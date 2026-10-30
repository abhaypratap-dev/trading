"""Parity check: compare our backtest trades with the admin's original backtest, trade by trade."""
from __future__ import annotations

import pandas as pd


def compare(ours: pd.DataFrame, theirs: pd.DataFrame, time_tolerance_min: int = 10, price_tolerance_pct: float = 0.5) -> dict:
    """ours: engine trades. theirs: parsed admin trades (symbol, side, entry_ts, entry, [exit_ts, exit, net_pnl])."""
    a = ours.copy()
    b = theirs.copy()
    a["entry_ts"] = pd.to_datetime(a["entry_ts"])
    b["entry_ts"] = pd.to_datetime(b["entry_ts"])
    a["_used"] = False
    matched, shifted, missing = [], [], []

    for i, t in b.iterrows():
        # Candidates: same side, same symbol (unless the export had none), same trading day.
        cand = a[(~a["_used"]) & (a["side"] == t["side"]) &
                 ((a["symbol"] == t["symbol"]) | (t["symbol"] == "UNKNOWN")) &
                 (a["entry_ts"].dt.date == t["entry_ts"].date())]
        if cand.empty:
            missing.append(_row(t))
            continue
        j = (cand["entry_ts"] - t["entry_ts"]).abs().idxmin()
        a.loc[j, "_used"] = True
        o = a.loc[j]
        dt = abs((o["entry_ts"] - t["entry_ts"]).total_seconds()) / 60
        px_diff = abs(o["entry"] - t["entry"]) / t["entry"] * 100 if pd.notna(t.get("entry")) and t.get("entry") else 0.0
        rec = {"admin": _row(t), "ours": _row(o), "time_diff_min": round(dt, 1), "entry_diff_pct": round(px_diff, 3)}
        (matched if dt <= time_tolerance_min and px_diff <= price_tolerance_pct else shifted).append(rec)

    extra = [_row(r) for _, r in a[~a["_used"]].iterrows()]
    n_admin = len(b)
    report = {
        "admin_trades": n_admin, "our_trades": len(ours),
        "matched": len(matched), "shifted": len(shifted), "missing": len(missing), "extra": len(extra),
        "match_rate_pct": round(len(matched) / n_admin * 100, 2) if n_admin else 0.0,
        "examples": {"shifted": shifted[:20], "missing": missing[:20], "extra": extra[:20]},
    }
    if "net_pnl" in b and b["net_pnl"].notna().any():
        report["admin_net_pnl"] = round(float(b["net_pnl"].sum()), 2)
        report["our_net_pnl"] = round(float(ours["net_pnl"].sum()), 2)
    report["verdict"] = ("PARITY" if report["match_rate_pct"] >= 95 and report["extra"] <= 0.05 * max(n_admin, 1)
                         else "CLOSE" if report["match_rate_pct"] >= 80 else "MISMATCH")
    report["likely_causes"] = _causes(report)
    return report


def _row(r) -> dict:
    keep = ("symbol", "side", "entry_ts", "entry", "exit_ts", "exit", "net_pnl")
    return {k: (str(r[k]) if k.endswith("_ts") else r[k]) for k in keep if k in r and pd.notna(r[k])}


def _causes(rep: dict) -> list[str]:
    out = []
    if rep["shifted"] > 0.1 * max(rep["admin_trades"], 1):
        out.append("Many trades shifted in time: check fill rule (signal candle close vs next candle open) and timezone.")
    if rep["missing"] > 0.1 * max(rep["admin_trades"], 1):
        out.append("Trades missing on our side: a filter or zone rule may be stricter, or data has gaps on those days.")
    if rep["extra"] > 0.1 * max(rep["admin_trades"], 1):
        out.append("Extra trades on our side: the original may use an extra filter, or a different session start.")
    if not out:
        out.append("No systematic difference found.")
    return out
