"""Data integrity: verify every call, paper trade and wallet balance against real market data, and repair.

Checks
  1. Calls (signals): future-dated or priced away from the real candle at their time -> marked simulated/invalid
     and hidden everywhere (the platform ran on simulated data before Upstox was connected).
  2. Paper trades: entry and exit prices must sit inside the real price range at their time, and their call must be
     real -> otherwise the trade is voided and its P&L reversed in the wallet ledger.
  3. Wallet: balance must equal the last reset plus every ledger movement since; each trade's ledger total must
     equal its net P&L.
  4. Candles: OHLC consistency of stored real candles.
"""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd

from yonexis import db
from yonexis.config import settings

TOL = 0.005   # 0.5% outside the candle's range is tolerated (slippage, tick rounding)


def price_matches(bars: pd.DataFrame, ts: pd.Timestamp, price: float, minutes: int = 5) -> tuple[bool, str]:
    if bars is None or bars.empty:
        return False, "no market data for that day"
    w = bars[(bars.index >= ts.floor("min") - pd.Timedelta(minutes=1)) & (bars.index < ts + pd.Timedelta(minutes=minutes + 1))]
    if w.empty:
        return False, f"no candle at {ts:%H:%M}"
    lo, hi = float(w["low"].min()), float(w["high"].max())
    if lo * (1 - TOL) <= price <= hi * (1 + TOL):
        return True, "ok"
    return False, f"price {price:.2f} but market traded {lo:.2f}-{hi:.2f} at {ts:%H:%M}"


_cache: dict[tuple, pd.DataFrame] = {}


def real_bars(sym: str, day: pd.Timestamp) -> pd.DataFrame:
    key = (sym, day.date())
    if key not in _cache:
        df = db.load_candles(sym, "1m", str(day.date()), str(day.date()), real_only=True)
        if df.empty:
            df = db.load_candles(sym, "5m", str(day.date()), str(day.date()), real_only=True)
        _cache[key] = df
    return _cache[key]


def run(repair: bool = True) -> dict:
    from yonexis.broker.paper import void_trade
    _cache.clear()
    now = pd.Timestamp.now()
    rep: dict = {"at": datetime.now().isoformat(timespec="seconds"), "repair": repair, "calls": {}, "trades": {}, "wallet": {},
                 "candles": {}, "issues": []}

    # ------------------------------------------------------------------ 1. calls
    sigs = db.fetch(db.signals)
    bad_sig: set[int] = set()
    counts = {"checked": 0, "verified": 0, "simulated": 0, "invalid": 0, "unverifiable": 0}
    for s in sigs:
        if s["source"] in ("simulated", "invalid"):
            bad_sig.add(s["id"])
            counts[s["source"]] += 1
            continue
        counts["checked"] += 1
        ts = pd.Timestamp(s["ts"])
        verdict = None
        if ts > now + pd.Timedelta(minutes=2):
            verdict, why = "simulated", f"dated in the future ({ts:%d %b %H:%M})"
        else:
            ok, why = price_matches(real_bars(s["symbol"], ts.normalize()), ts, float(s["price"] or 0))
            if ok:
                counts["verified"] += 1
                if repair and s["source"] is None:
                    db.update_row(db.signals, s["id"], source="live")
                continue
            if "no market data" in why or "no candle" in why:
                counts["unverifiable"] += 1
                continue
            verdict = "invalid"
        counts[verdict] += 1
        bad_sig.add(s["id"])
        if len(rep["issues"]) < 60:
            rep["issues"].append({"kind": "call", "id": s["id"], "symbol": s["symbol"], "ts": str(ts), "price": s["price"], "problem": why})
        if repair:
            db.update_row(db.signals, s["id"], source=verdict)
    rep["calls"] = counts

    # ------------------------------------------------------------------ 2. paper trades
    tc = {"checked": 0, "ok": 0, "voided": 0, "reversed_pnl": 0.0}
    for t in db.fetch(db.paper_trades):
        if t["exit_reason"] == "void":
            continue
        tc["checked"] += 1
        problems = []
        if t["signal_id"] in bad_sig:
            problems.append("its call was not real market data")
        if t["entry_ts"]:
            ok, why = price_matches(real_bars(t["symbol"], pd.Timestamp(t["entry_ts"]).normalize()), pd.Timestamp(t["entry_ts"]),
                                    float(t["entry_price"]), 1)
            if not ok and "no " not in why:
                problems.append("entry " + why)
        if t["status"] == "closed" and t["exit_ts"] and t["exit_price"]:
            ok, why = price_matches(real_bars(t["symbol"], pd.Timestamp(t["exit_ts"]).normalize()), pd.Timestamp(t["exit_ts"]),
                                    float(t["exit_price"]), 1)
            if not ok and "no " not in why:
                problems.append("exit " + why)
        if not problems:
            tc["ok"] += 1
            continue
        tc["voided"] += 1
        tc["reversed_pnl"] = round(tc["reversed_pnl"] - (t["net_pnl"] or 0), 2)
        rep["issues"].append({"kind": "paper_trade", "id": t["id"], "symbol": t["symbol"], "side": t["side"],
                              "entry": t["entry_price"], "exit": t["exit_price"], "net_pnl": t["net_pnl"], "problem": "; ".join(problems)})
        if repair:
            void_trade(t, "price check failed", "invalid")
    rep["trades"] = tc

    # ------------------------------------------------------------------ 3. wallet
    ledger = db.fetch(db.wallet_ledger, order=db.wallet_ledger.c.id)
    w = db.fetch(db.wallet, db.wallet.c.id == 1)
    if w and ledger:
        last_reset = max(i for i, l in enumerate(ledger) if l["kind"] == "reset") if any(l["kind"] == "reset" for l in ledger) else 0
        expected = round(ledger[last_reset]["amount"] + sum(l["amount"] for l in ledger[last_reset + 1:]), 2)
        stored = round(w[0]["balance"], 2)
        per_trade: dict[int, float] = {}
        for l in ledger[last_reset + 1:]:
            if l["kind"] == "trade" and str(l["ref"] or "").startswith("paper_trade:"):
                tid = int(l["ref"].split(":")[1])
                per_trade[tid] = round(per_trade.get(tid, 0) + l["amount"], 2)
        trades = {t["id"]: t for t in db.fetch(db.paper_trades)}
        mism = [(tid, amt, round(trades[tid]["net_pnl"] or 0, 2)) for tid, amt in per_trade.items()
                if tid in trades and abs(amt - (trades[tid]["net_pnl"] or 0)) > 0.05]
        rep["wallet"] = {"stored_balance": stored, "ledger_balance": expected, "balanced": abs(stored - expected) < 0.05,
                         "trade_ledger_mismatches": len(mism)}
        for tid, amt, net in mism[:20]:
            rep["issues"].append({"kind": "ledger", "trade_id": tid, "ledger_total": amt, "trade_net": net})
        if repair and abs(stored - expected) >= 0.05:
            db.update_row(db.wallet, 1, balance=expected)
            rep["wallet"]["repaired"] = True

    # ------------------------------------------------------------------ 4. candles
    from sqlalchemy import text
    with db.engine.connect() as cx:
        badohlc = cx.execute(text("SELECT COUNT(*) FROM candles WHERE source <> 'simulated' AND "
                                  "(high < low OR high < open OR high < close OR low > open OR low > close)")).scalar()
        total = cx.execute(text("SELECT COUNT(*) FROM candles WHERE source <> 'simulated'")).scalar()
    rep["candles"] = {"real_candles": int(total or 0), "inconsistent_ohlc": int(badohlc or 0)}

    rep["ok"] = (counts["simulated"] + counts["invalid"] == 0 or repair) and tc["voided"] == 0 and rep["wallet"].get("balanced", True)
    try:
        (settings.DATA_DIR / "integrity.json").write_text(json.dumps(rep, indent=1, default=str))
    except Exception:
        pass
    if repair and (tc["voided"] or counts["simulated"] or counts["invalid"]):
        db.log("system", "integrity_repair", {"calls": counts, "trades": tc, "wallet": rep["wallet"]})
    return rep
