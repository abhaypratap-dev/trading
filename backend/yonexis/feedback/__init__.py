"""Admin feedback: store actions + remarks, score AI vs admin vs combined, mine patterns."""
from __future__ import annotations

from collections import defaultdict

from yonexis import db


def record(signal_id: int, action: str, remark: str = "", new_sl: float | None = None, new_target: float | None = None) -> int:
    if action not in ("agree", "reject", "modify", "skip"):
        raise ValueError("action must be agree, reject, modify or skip")
    sig = db.fetch(db.signals, db.signals.c.id == signal_id)
    if not sig:
        raise KeyError(f"signal {signal_id} not found")
    fid = db.insert_row(db.feedback, signal_id=signal_id, action=action, remark=remark, new_sl=new_sl,
                        new_target=new_target, features=sig[0]["features"], reasons=None)
    db.log("admin", "feedback", {"signal_id": signal_id, "action": action, "remark": remark})
    return fid


def rows_with_outcomes() -> list[dict]:
    fb = db.fetch(db.feedback, order=db.feedback.c.id)
    sig = {s["id"]: s for s in db.fetch(db.signals, db.real_signals())}
    out = []
    for f in fb:
        s = sig.get(f["signal_id"])
        if not s:
            continue
        out.append({**f, "symbol": s["symbol"], "side": s["side"], "strategy": s["strategy"], "ai_score": s["ai_score"],
                    "outcome_pnl": s["outcome_pnl"], "outcome": s["outcome"], "signal_ts": s["ts"],
                    "features": db.uj(f["features"], {}), "reasons": db.uj(f["reasons"], [])})
    return out


def scoreboard() -> dict:
    """AI alone vs admin alone vs combined, on calls that have an outcome."""
    signals = [s for s in db.fetch(db.signals, db.real_signals()) if s["outcome_pnl"] is not None]
    latest_fb = {}
    for f in db.fetch(db.feedback, order=db.feedback.c.id):
        latest_fb[f["signal_id"]] = f
    ai_pnl = sum(s["outcome_pnl"] for s in signals)
    with_fb = [s for s in signals if s["id"] in latest_fb]
    admin_taken = [s for s in with_fb if latest_fb[s["id"]]["action"] in ("agree", "modify")]
    admin_pnl = sum(s["outcome_pnl"] for s in admin_taken)
    ai_on_fb = sum(s["outcome_pnl"] for s in with_fb)
    right = sum(1 for s in with_fb if (latest_fb[s["id"]]["action"] in ("agree", "modify")) == (s["outcome_pnl"] > 0))
    combined = [s for s in signals if (s["score"] or 0) >= 0.5 and (s["id"] not in latest_fb or latest_fb[s["id"]]["action"] != "reject")]

    by_tag: dict[str, dict] = defaultdict(lambda: {"count": 0, "rejected": 0, "admin_right": 0})
    for s in with_fb:
        f = latest_fb[s["id"]]
        took = f["action"] in ("agree", "modify")
        for t in db.uj(f["reasons"], []) or []:
            d = by_tag[t]
            d["count"] += 1
            d["rejected"] += 0 if took else 1
            d["admin_right"] += int(took == (s["outcome_pnl"] > 0))
    for d in by_tag.values():
        d["admin_right_pct"] = round(d["admin_right"] / d["count"] * 100, 1) if d["count"] else 0.0

    return {
        "calls_with_outcome": len(signals), "calls_with_feedback": len(with_fb),
        "ai_alone_pnl": round(ai_pnl, 2),
        "ai_on_reviewed_calls_pnl": round(ai_on_fb, 2),
        "admin_filtered_pnl": round(admin_pnl, 2),
        "combined_pnl": round(sum(s["outcome_pnl"] for s in combined), 2),
        "admin_decision_accuracy_pct": round(right / len(with_fb) * 100, 1) if with_fb else None,
        "admin_adds_value": (admin_pnl > ai_on_fb) if with_fb else None,
        "by_tag": dict(by_tag),
    }
