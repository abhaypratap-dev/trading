"""End-to-end self test. Writes data/system_check.json so results can be read from the project folder."""
from __future__ import annotations

import json
import time
import traceback
from datetime import datetime, timedelta
from typing import Callable

import pandas as pd
from sqlalchemy import text

from yonexis import db
from yonexis.config import settings

SAMPLE_IDEA = ("Buy when a 5-minute candle closes above the high of the first red candle of the day, sell short when it closes "
               "below that candle's low. Stop loss at the other side of the zone, target 2 times the risk, one trade per day, "
               "no new trades after 2:30 pm.")


def expected_last_session(now: datetime) -> pd.Timestamp:
    """Most recent weekday session that should have candles (holidays not known: a mismatch is a warning)."""
    d = pd.Timestamp(now).normalize()
    if pd.Timestamp(now).time() < datetime.strptime("09:20", "%H:%M").time() or d.dayofweek >= 5:
        d -= pd.Timedelta(days=1)
    while d.dayofweek >= 5:
        d -= pd.Timedelta(days=1)
    return d


def run(deep: bool = False, progress: Callable[[str, dict], None] = lambda s, i: None) -> dict:
    results: list[dict] = []
    progress("starting", {"total": 9 + (1 if deep else 0)})

    def check(name: str, fn, warn_only: bool = False):
        progress(name, {"done": len(results)})
        t0 = time.time()
        try:
            ok, detail = fn()
            status = "pass" if ok else ("warn" if warn_only else "fail")
        except Exception as e:
            status, detail = ("warn" if warn_only else "fail"), f"{type(e).__name__}: {e}\n{traceback.format_exc()[-600:]}"
        results.append({"check": name, "status": status, "detail": detail, "seconds": round(time.time() - t0, 1)})
        progress("partial", {"results": list(results)})

    # ---------------------------------------------------------------- platform
    def c_db():
        with db.engine.connect() as cx:
            cx.execute(text("SELECT 1"))
        counts = {t: len(db.fetch(getattr(db, t), limit=100000)) for t in ("strategies", "signals", "paper_trades", "backtests")}
        return True, {"dialect": db.engine.dialect.name, **counts}
    check("database", c_db)

    def c_tz():
        ist = datetime.utcnow() + timedelta(hours=5, minutes=30)
        drift = abs((datetime.now() - ist).total_seconds())
        return drift < 120, {"local_time": datetime.now().isoformat(timespec="seconds"), "ist": ist.isoformat(timespec="seconds")}
    check("timezone is IST", c_tz)

    def c_lock():
        from yonexis.broker import OrdersLocked, PaperBroker
        from yonexis.data.connectors.upstox import UpstoxConnector
        from yonexis.live import engine as live
        try:
            UpstoxConnector(token="x").place_order("RELIANCE", "BUY", 1)
            return False, "an order call was NOT blocked"
        except OrdersLocked:
            pass
        broker = type(live.engine.broker).__name__ if live.engine else "n/a"
        return (not settings.live_orders_allowed and (live.engine is None or isinstance(live.engine.broker, PaperBroker)),
                {"real_orders": "LOCKED" if not settings.live_orders_allowed else "ENABLED", "engine_broker": broker})
    check("real orders locked (paper only)", c_lock)

    def c_wallet():
        from yonexis.broker import PaperBroker
        s = PaperBroker().summary()
        return s["balance"] > 0, {k: s[k] for k in ("balance", "available", "equity", "closed_trades", "win_rate")}
    check("paper wallet", c_wallet)

    # ---------------------------------------------------------------- AI model
    def c_llm():
        from yonexis.agents import llm
        h = llm.health()
        if not h.get("ok") or not h.get("model_available"):
            return False, h
        t0 = time.time()
        reply = llm.ask("You are a test.", "Reply with exactly the word OK.", max_tokens=5)
        return "ok" in reply.lower(), {"model": settings.LLM_MODEL, "reply": reply.strip()[:40], "latency_s": round(time.time() - t0, 1)}
    check("local AI model", c_llm)

    # ---------------------------------------------------------------- sandbox + backtest engine
    from yonexis.data.connectors.simulated import simulate
    from yonexis.sandbox import run as sandbox_run
    from yonexis.strategies.loader import latest_versions

    code = latest_versions()["first_red_candle_zone"]["code"]

    def c_sandbox():
        r = sandbox_run(code, {"SIM": simulate("SIM", "2024-01-01", "2024-02-29", seed=1)}, {"kind": "quick"}, timeout=180)
        if not r.get("ok"):
            return False, r.get("error")
        from yonexis.sandbox.runner import status as sbx_status
        m = r["result"]["metrics"]
        st = sbx_status()
        info = {"trades": m.get("trades"), "net_pnl": m.get("net_pnl"), "ran_in": st["mode"]}
        if st["note"]:
            info["note"] = st["note"] + " (check: docker compose logs sandbox)"
        return m.get("trades", 0) > 0, info
    check("sandbox runs strategy code", c_sandbox)

    def c_eval():
        data = {s: simulate(s, "2024-01-01", "2024-09-30", seed=i) for i, s in enumerate(["SIMA", "SIMB"])}
        r = sandbox_run(code, data, {"kind": "full", "param_space": {"rr": [1.5, 2.0]}, "wf_train_days": 60, "wf_test_days": 30}, timeout=900)
        if not r.get("ok"):
            return False, r.get("error")
        ev = r["result"]
        return "checks" in ev, {"verdict": ev["verdict"], "score": ev["score"],
                                "note": "simulated random data: FAIL is the expected verdict here"}
    check("full evaluation pipeline", c_eval)

    # ---------------------------------------------------------------- Upstox + live data
    from yonexis.data.connectors import upstox as ux

    def c_upstox():
        if not (settings.UPSTOX_API_KEY and settings.UPSTOX_API_SECRET):
            return False, "UPSTOX_API_KEY / UPSTOX_API_SECRET missing in .env"
        if not ux.tokens.token:
            return False, "not logged in today (Upstox logins expire at 3:30 AM) - Data tab > Connect Upstox"
        c = ux.UpstoxConnector()
        prof = c.profile()
        end = datetime.now().date()
        df = c.fetch_history("RELIANCE", str(end - timedelta(days=8)), str(end), "5m")
        last = df.index[-1] if not df.empty else None
        exp = expected_last_session(datetime.now())
        fresh = last is not None and last.normalize() >= exp
        return fresh, {"user": prof.get("user_id"), "candles": len(df), "last_candle": str(last), "expected_session": str(exp.date()),
                       "note": "" if fresh else "last candle older than the expected session (holiday? or data issue)"}
    check("Upstox login + candles are current", c_upstox, warn_only=True)

    def c_live():
        from yonexis.live import engine as live
        e = live.engine
        if not e:
            return False, "live engine not running"
        info = {"connector": e.connector_name, "running": e.running, "error": e.last_error,
                "feed": e.feed.info() if e.feed is not None else None, "symbols_with_data": len(e.status)}
        lasts = {s: r.get("ts") for s, r in e.status.items()}
        if lasts:
            newest = max(pd.Timestamp(v) for v in lasts.values() if v)
            info["newest_candle"] = str(newest)
            info["stale"] = newest.normalize() < expected_last_session(datetime.now())
        ok = e.running and not info.get("stale", False) and (e.feed is None or e.feed.info()["status"] == "connected" or
                                                              not ux.tokens.token)
        return ok, info
    check("live engine and feed", c_live, warn_only=True)

    # ---------------------------------------------------------------- AI builder end to end (slow)
    if deep:
        from yonexis.agents import pipeline

        def c_ai():
            t0 = time.time()
            spec = pipeline.spec_from_prompt(SAMPLE_IDEA)
            t_spec = round(time.time() - t0, 1)
            data = {s: simulate(s, "2024-01-01", "2024-12-31", seed=i + 5) for i, s in enumerate(["SIMA", "SIMB"])}
            stages = []
            out = pipeline.build_strategy(spec, data, progress=lambda st, info: stages.append(st), max_fix_rounds=3)
            detail = {"spec_seconds": t_spec, "rule_card_name": spec.get("name"), "questions": spec.get("questions", [])[:3],
                      "stages": stages, "ok": out.get("ok"), "stopped_at": None if out.get("ok") else out.get("stage"),
                      "problems": (out.get("problems") or out.get("error") or "")}
            if out.get("ok"):
                detail.update({"verdict": out["evaluation"]["verdict"], "score": out["evaluation"]["score"],
                               "review_approved": out["review"]["approved"], "code_lines": out["code"].count("\n")})
            return bool(out.get("ok")), detail
        check("AI builder: idea -> rule card -> code -> review -> backtest", c_ai)

    summary = {s: sum(1 for r in results if r["status"] == s) for s in ("pass", "warn", "fail")}
    report = {"at": datetime.now().isoformat(timespec="seconds"), "deep": deep, "summary": summary, "results": results}
    try:
        (settings.DATA_DIR / "system_check.json").write_text(json.dumps(report, indent=2, default=str))
    except Exception:
        pass
    db.log("admin", "system_check", summary)
    return report
