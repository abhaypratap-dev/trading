"""Paper performance vs backtest expectation, promotion gates and the project phase checklist."""
from __future__ import annotations

import math
from datetime import datetime

from yonexis import db

MIN_SESSIONS = 20
MIN_TRADES_FOR_VERDICT = 10


def paper_stats(strategy: str) -> dict:
    t = [x for x in db.fetch(db.paper_trades, db.paper_trades.c.strategy == strategy)
         if x["status"] == "closed" and x["exit_reason"] != "void" and not str(x["tag"] or "").startswith("backfill")]
    if not t:
        return {"trades": 0, "sessions": 0}
    rets = [(x["net_pnl"] or 0) / ((x["entry_price"] or 1) * (x["qty"] or 1)) * 100 for x in t]
    wins = [x for x in t if (x["net_pnl"] or 0) > 0]
    days = sorted({x["entry_ts"].date() for x in t if x["entry_ts"]})
    mean = sum(rets) / len(rets)
    std = math.sqrt(sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)) if len(rets) > 1 else None
    return {"trades": len(t), "sessions": len(days), "first_day": str(days[0]), "last_day": str(days[-1]),
            "net_pnl": round(sum(x["net_pnl"] or 0 for x in t), 2), "win_rate": round(len(wins) / len(t) * 100, 1),
            "avg_ret_pct": round(mean, 4), "ret_pct_std": round(std, 4) if std is not None else None,
            "costs": round(sum(x["costs"] or 0 for x in t), 2)}


def latest_backtest(strategy: str) -> dict | None:
    """Most recent FULL evaluation on real (non-simulated) data."""
    for b in db.fetch(db.backtests, (db.backtests.c.strategy == strategy) & (db.backtests.c.kind == "full"),
                      order=db.backtests.c.id.desc(), limit=20):
        r = db.uj(b["result"], {})
        if (db.uj(b["params"], {}) or {}).get("_source", "db") == "simulated":
            continue
        dev = r.get("development") or {}
        return {"id": b["id"], "created_at": str(b["created_at"]), "verdict": r.get("verdict"), "score": r.get("score"),
                "trades": dev.get("trades"), "win_rate": dev.get("win_rate"), "avg_ret_pct": dev.get("avg_ret_pct"),
                "ret_pct_std": dev.get("ret_pct_std"), "net_pnl": dev.get("net_pnl"), "symbols": db.uj(b["symbols"], []),
                "period": [b["start"], b["end"]]}
    return None


def compare(strategy: str) -> dict:
    p, b = paper_stats(strategy), latest_backtest(strategy)
    out = {"strategy": strategy, "paper": p, "backtest": b, "checks": {}, "verdict": "collecting"}
    if not b:
        out["note"] = "Run a full evaluation of this strategy on real stored data (Backtest tab, Data = Stored data) to set the expectation."
        return out
    if p["trades"] >= MIN_TRADES_FOR_VERDICT and b.get("avg_ret_pct") is not None and b.get("ret_pct_std"):
        se = b["ret_pct_std"] / math.sqrt(p["trades"])
        lo, hi = b["avg_ret_pct"] - 2 * se, b["avg_ret_pct"] + 2 * se
        out["expected_avg_ret_band"] = [round(lo, 4), round(hi, 4)]
        out["checks"]["paper_avg_return_inside_backtest_band"] = p["avg_ret_pct"] >= lo
        out["checks"]["paper_win_rate_within_10pts"] = abs(p["win_rate"] - (b["win_rate"] or 0)) <= 10
    out["checks"]["enough_sessions"] = p["sessions"] >= MIN_SESSIONS
    out["checks"]["backtest_passed"] = b.get("verdict") == "PASS"
    if p["trades"] >= MIN_TRADES_FOR_VERDICT and out["checks"].get("paper_avg_return_inside_backtest_band") is False:
        out["verdict"] = "underperforming"
    elif all(out["checks"].values()) and len(out["checks"]) >= 4:
        out["verdict"] = "ready_for_next_stage"
    else:
        out["verdict"] = "collecting"
    out["sessions_left"] = max(0, MIN_SESSIONS - p["sessions"])
    return out


def auto_pause_underperformers() -> list[str]:
    """Nightly: strategies trading paper that fall below their backtest band are paused (status -> approved)."""
    from yonexis.strategies.loader import latest_versions
    paused = []
    for r in latest_versions().values():
        if r["status"] in ("paper", "live_small", "live") and compare(r["name"])["verdict"] == "underperforming":
            db.update_row(db.strategies, r["id"], status="approved")
            db.log("scheduler", "strategy_paused", {"name": r["name"], "reason": "paper results below the backtest band"})
            paused.append(r["name"])
    return paused


def phases() -> list[dict]:
    """Project roadmap gates computed from what is actually in the database."""
    import json
    from yonexis.config import settings
    from yonexis.strategies.loader import latest_versions
    chk = {}
    p = settings.DATA_DIR / "system_check.json"
    if p.exists():
        try:
            chk = json.loads(p.read_text())
        except Exception:
            chk = {}
    sysok = bool(chk) and chk.get("summary", {}).get("fail", 1) == 0
    bts = db.fetch(db.backtests)
    parity = [db.uj(b["result"], {}).get("verdict") for b in bts if b["kind"] == "parity"]
    strats = latest_versions()
    ai_pass = []
    for b in bts:
        if b["kind"] != "full":
            continue
        r = db.uj(b["result"], {})
        s = strats.get(b["strategy"])
        if s and s["origin"] == "ai" and r.get("verdict") == "PASS" and (r.get("checks") or {}).get("holdout_profitable"):
            ai_pass.append(b["strategy"])
    paper = [compare(r["name"]) for r in strats.values() if r["status"] in ("paper", "live_small", "live")]
    candles = db.candle_inventory()
    real_hist = [c for c in candles if c.get("source") not in ("simulated",) and c["interval"] == "5m" and c["n"] > 5000]
    return [
        {"phase": "1 · Platform", "done": sysok, "gate": "System check with no failures",
         "status": f"last check {chk.get('at', 'never')} · {chk.get('summary', {})}" if chk else "run the system check"},
        {"phase": "1b · Real history", "done": len(real_hist) >= 5, "gate": "≥5 watchlist stocks with ~1 year+ of 5m history stored",
         "status": f"{len(real_hist)} stocks with deep 5m history"},
        {"phase": "2 · Admin scripts", "done": "PARITY" in parity, "gate": "Admin's own script matches his backtest (parity)",
         "status": f"{len(parity)} parity runs, best: {max(parity, key=['MISMATCH','CLOSE','PARITY'].index) if parity else 'none'}"},
        {"phase": "3 · AI strategies", "done": bool(ai_pass), "gate": "AI-built strategy PASSES full evaluation incl. holdout",
         "status": f"{len(ai_pass)} passing: {', '.join(sorted(set(ai_pass))) or '-'}"},
        {"phase": "4 · Paper trading", "done": any(x["verdict"] == "ready_for_next_stage" for x in paper),
         "gate": f"{MIN_SESSIONS} paper sessions inside the backtest band",
         "status": "; ".join(f"{x['strategy']}: {x['paper'].get('sessions', 0)}/{MIN_SESSIONS} sessions, {x['verdict']}" for x in paper) or "no strategy in paper"},
        {"phase": "5 · Live", "done": False, "gate": "Your decision + unlock phrase + static IP", "status": "locked"},
    ]
