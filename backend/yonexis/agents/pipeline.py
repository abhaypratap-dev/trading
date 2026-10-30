"""Agent pipeline: admin prompt -> rule card -> code -> review -> sandbox -> variants -> full evaluation -> critic + report.

LLM agents write and critique; every number comes from the deterministic engine via the sandbox.
"""
from __future__ import annotations

import json
import re
from typing import Callable

import pandas as pd

from yonexis.sandbox import check as static_check
from yonexis.sandbox import run as sandbox_run

from . import llm, prompts

Progress = Callable[[str, dict], None]


def _noop(stage: str, info: dict) -> None:
    pass


def spec_from_prompt(prompt: str, context: str = "") -> dict:
    user = f"Admin's idea:\n{prompt}\n\nContext (watchlist, existing strategies, earlier answers):\n{context or 'none'}"
    return llm.extract_json(llm.ask(prompts.SPEC_AGENT, user, temperature=0.1))


def code_from_spec(spec: dict) -> str:
    return llm.extract_code(llm.ask(prompts.CODER_AGENT, "Rule card:\n" + json.dumps(spec, indent=2), temperature=0.1, max_tokens=6000))


def convert_script(source: str, language: str = "auto", notes: str = "") -> str:
    user = f"Language: {language}\nAdmin notes: {notes or 'none'}\n\nOriginal script:\n```\n{source}\n```"
    return llm.extract_code(llm.ask(prompts.CONVERTER_AGENT, user, temperature=0.0, max_tokens=6000))


def review(code: str, spec: dict | None) -> dict:
    sc = static_check(code)
    try:
        r = llm.extract_json(llm.ask(prompts.REVIEWER_AGENT,
                                     f"Rule card:\n{json.dumps(spec or {}, indent=2)}\n\nCode:\n```python\n{code}\n```", temperature=0.0))
    except llm.LLMError as e:
        r = {"approved": None, "issues": [], "error": str(e)}
    r["static_check"] = sc
    r["approved"] = bool(sc["ok"] and r.get("approved") is not False and r.get("lookahead_risk") != "high")
    return r


def fix(code: str, problems: list[str]) -> str:
    user = "Problems to fix:\n- " + "\n- ".join(problems) + f"\n\nCurrent code:\n```python\n{code}\n```"
    return llm.extract_code(llm.ask(prompts.FIXER_AGENT, user, temperature=0.0, max_tokens=6000))


def propose_param_space(spec: dict, params: dict) -> dict:
    user = f"Rule card:\n{json.dumps(spec, indent=2)}\n\nStrategy params (defaults):\n{json.dumps(params, indent=2)}"
    try:
        out = llm.extract_json(llm.ask(prompts.OPTIMIZER_AGENT, user, temperature=0.2))
        space = {k: v for k, v in out.get("param_space", {}).items() if k in params and isinstance(v, list) and v}
    except llm.LLMError:
        space, out = {}, {}
    # Hard cap: at most 12 combinations, so multiple-testing stays under control.
    total = 1
    for k in list(space):
        if total * len(space[k]) > 12:
            space.pop(k)
        else:
            total *= len(space[k])
    return {"param_space": space, "rationale": out.get("rationale", "")}


def critic(evaluation: dict) -> str:
    try:
        return llm.ask(prompts.CRITIC_AGENT, json.dumps(_slim(evaluation), default=str), temperature=0.3)
    except llm.LLMError as e:
        return f"(critic unavailable: {e})"


def report(evaluation: dict) -> str:
    try:
        return llm.ask(prompts.REPORT_AGENT, json.dumps(_slim(evaluation), default=str), temperature=0.2)
    except llm.LLMError as e:
        return f"(report unavailable: {e})"


def _slim(ev: dict) -> dict:
    ev = dict(ev)
    if ev.get("walk_forward"):
        ev["walk_forward"] = {k: v for k, v in ev["walk_forward"].items() if k != "folds"}
    if ev.get("sensitivity"):
        ev["sensitivity"] = {k: v for k, v in ev["sensitivity"].items() if k != "rows"}
    return ev


def build_strategy(spec: dict, data: dict[str, pd.DataFrame], code: str | None = None, max_fix_rounds: int = 3,
                   baseline_net_pnl: float | None = None, n_trials_so_far: int = 1, progress: Progress = _noop,
                   config: dict | None = None) -> dict:
    """Full build loop. Returns code, reviews, quick backtest, full evaluation, critic and report."""
    log: list[dict] = []

    def step(stage: str, **info):
        log.append({"stage": stage, **{k: v for k, v in info.items() if k != "code"}})
        progress(stage, info)

    if code is None:
        step("coding")
        code = code_from_spec(spec)

    rv: dict = {}
    for round_ in range(max_fix_rounds + 1):
        rv = review(code, spec)
        step("review", round=round_, approved=rv["approved"], issues=len(rv.get("issues", [])),
             static_errors=rv["static_check"]["errors"])
        problems = list(rv["static_check"]["errors"])
        problems += [f"{i.get('problem')} -> {i.get('fix')}" for i in rv.get("issues", []) if i.get("severity") in ("high", "medium")]
        if not problems:
            # Dry-run in the sandbox to catch runtime errors.
            dry = sandbox_run(code, {k: v.tail(2000) for k, v in list(data.items())[:1]}, {"kind": "quick"}, timeout=120)
            if dry.get("ok"):
                break
            problems = [f"runtime error in sandbox: {dry.get('error')}\n{dry.get('trace', '')[-1500:]}"]
            step("dry_run_failed", error=dry.get("error"))
        if round_ == max_fix_rounds:
            return {"ok": False, "stage": "review", "code": code, "review": rv, "problems": problems, "log": log}
        step("fixing", round=round_, problems=problems[:5])
        code = fix(code, problems)

    step("quick_backtest")
    quick = sandbox_run(code, data, {"kind": "quick", "config": config or {}})
    if not quick.get("ok"):
        return {"ok": False, "stage": "quick_backtest", "code": code, "review": rv, "error": quick.get("error"), "log": log}
    strat_info = quick["result"]["strategy"]

    step("optimizer")
    ps = propose_param_space(spec, strat_info["params"])

    step("full_evaluation", param_space=ps["param_space"])
    full = sandbox_run(code, data, {"kind": "full", "param_space": ps["param_space"] or None, "config": config or {},
                                    "baseline_net_pnl": baseline_net_pnl, "n_trials_so_far": n_trials_so_far},
                       timeout=1800)
    if not full.get("ok"):
        return {"ok": False, "stage": "full_evaluation", "code": code, "review": rv, "error": full.get("error"), "log": log}
    ev = full["result"]

    step("critic")
    crit = critic(ev)
    step("report")
    rep = report(ev)
    step("done", verdict=ev.get("verdict"))
    quick_r = quick["result"]
    return {
        "ok": True, "name": strat_info["name"], "code": code, "spec": spec, "review": rv,
        "quick": {"metrics": quick_r["metrics"], "trades": quick_r.get("trades", [])[:500]},
        "param_space": ps, "evaluation": ev, "critic": crit, "report": rep, "log": log,
    }


# --------------------------------------------------------------------- feedback analyst

KEYWORD_TAGS = {
    r"result|earning|quarter": "results_soon", r"news|announce": "news", r"budget|policy|rbi|fed|event": "event_risk",
    r"resist": "resistance_nearby", r"support": "support_nearby", r"low vol|weak vol|no vol|volume weak|volume fake": "weak_volume",
    r"high vol|strong vol|volume good": "strong_volume", r"fake|trap|false break": "fake_breakout",
    r"sector.*(weak|down)": "sector_weak", r"sector.*(strong|up)": "sector_strong", r"nifty.*(weak|down)|market.*(weak|down)": "market_weak",
    r"nifty.*(strong|up)|market.*(strong|up)": "market_strong", r"overextend|stretched|too far": "overextended",
    r"gap": "gap_risk", r"illiquid|liquidity": "low_liquidity", r"operator": "operator_activity",
    r"sl .*tight|stop.*tight": "sl_too_tight", r"sl .*wide|stop.*wide": "sl_too_wide",
    r"good setup|clean|perfect": "good_setup", r"trend against|counter trend": "trend_against",
}


def tag_remarks(items: list[dict]) -> dict[int, list[str]]:
    """items: [{'id':..,'remark':..}]. Uses the LLM, falls back to keywords."""
    items = [i for i in items if (i.get("remark") or "").strip()]
    if not items:
        return {}
    try:
        out = llm.extract_json(llm.ask(prompts.FEEDBACK_AGENT, json.dumps(items), temperature=0.0))
        return {int(x["id"]): list(x.get("tags", [])) for x in out.get("items", [])}
    except Exception:
        res = {}
        for i in items:
            r = i["remark"].lower()
            res[int(i["id"])] = sorted({t for pat, t in KEYWORD_TAGS.items() if re.search(pat, r)}) or ["other"]
        return res


def suggest_from_feedback(stats: dict) -> list[dict]:
    try:
        return llm.extract_json(llm.ask(prompts.PATTERN_AGENT, json.dumps(stats, default=str), temperature=0.2)).get("suggestions", [])
    except Exception:
        out = []
        for tag, s in (stats.get("by_tag") or {}).items():
            if s.get("rejected", 0) >= 5 and s.get("admin_right_pct", 0) >= 70:
                out.append({"title": f"Add a filter for '{tag}'",
                            "evidence": f"Admin rejected {s['rejected']} calls tagged {tag} and was right {s['admin_right_pct']}% of the time.",
                            "prompt_for_builder": f"Skip signals when the situation matches '{tag.replace('_', ' ')}'."})
        return out[:3]
