"""What-if test: put today's missed first calls into the paper wallet at the price of the call, then replay the day.

Each stock's FIRST call of the day that was not traded (blocked / not traded) is opened as a paper position at the
call price and time. Today's 1-minute candles since then decide what would have happened: stop loss or target hit
(stop checked first in each minute), otherwise it stays open and follows the live price.
Trades are tagged "backfill" so phase-4 statistics (paper vs backtest) ignore them.
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from yonexis import db
from yonexis.config import settings


def _minute_bars(engine, sym: str, day: pd.Timestamp) -> pd.DataFrame:
    if engine is not None and getattr(engine, "feed", None) is not None:
        df = engine.feed.session_1m(sym)
        f = engine.feed.builder.candles(sym, 1, closed_only=False)
        if not f.empty:
            df = pd.concat([df, f]) if not df.empty else f
            df = df[~df.index.duplicated(keep="last")].sort_index()
        if not df.empty:
            return df[df.index >= day]
    df = db.load_candles(sym, "1m", str(day.date()), str(day.date()), real_only=True)
    if df.empty and engine is not None:                      # simulated feed: use the engine's own candles
        c = engine.cache.get(sym)
        if c is not None and not c.empty:
            df = c[c.index >= day]
    return df


def run(engine, broker, dry_run: bool = False) -> dict:
    from yonexis.risk import risk
    day = pd.Timestamp.now().normalize()
    if engine is not None and engine.sim:
        day = engine.sim.current_day
    minutes = int(settings.LIVE_INTERVAL.rstrip("m")) if settings.LIVE_INTERVAL.endswith("m") else 5
    sigs = db.fetch(db.signals, (db.signals.c.ts >= day.to_pydatetime()) & (db.signals.c.ts < (day + pd.Timedelta(days=1)).to_pydatetime())
                    & (db.signals.c.id > 0 if (engine is not None and engine.sim) else db.real_signals()), order=db.signals.c.ts)
    first: dict[tuple, dict] = {}
    for s in sigs:
        first.setdefault((s["strategy"], s["symbol"]), s)
    open_syms = {t["symbol"] for t in db.fetch(db.paper_trades, db.paper_trades.c.status == "open")}
    rows = []
    for (strategy, sym), s in first.items():
        st = str(s["status"] or "")
        r = {"symbol": sym, "strategy": strategy, "side": s["side"], "call_time": str(s["ts"])[11:16], "call_price": s["price"],
             "stop_loss": s["stop_loss"], "target": s["target"], "original_status": st}
        if not (st.startswith("blocked") or st.startswith("not traded")):
            r["result"] = "skipped: this call was already traded" if st in ("taken", "closed") else f"skipped: {st}"
            rows.append(r)
            continue
        if sym in open_syms:
            r["result"] = "skipped: already holding a paper position in this stock"
            rows.append(r)
            continue
        if len(open_syms) >= risk.limits.max_open_positions:
            r["result"] = f"skipped: max open positions ({risk.limits.max_open_positions}) reached"
            rows.append(r)
            continue
        bars_all = _minute_bars(engine, sym, day)
        from yonexis.integrity import price_matches
        ok, why = price_matches(bars_all, pd.Timestamp(s["ts"]), float(s["price"]), minutes)
        if not ok and not (engine is not None and engine.sim):
            r["result"] = f"skipped: call price does not match market data ({why})"
            rows.append(r)
            continue
        tags = db.uj(s["tags"], {}) or {}
        start = pd.Timestamp(s["ts"]) + (pd.Timedelta(0) if tags.get("trigger") == "touch" else pd.Timedelta(minutes=minutes))
        bars = _minute_bars(engine, sym, day)
        bars = bars[bars.index >= start.floor("min")]
        qty = int(min(risk.limits.capital_per_trade, risk.limits.max_capital_per_symbol) // max(s["price"], 0.01))
        if dry_run:
            r["result"] = "would open"
            rows.append(r)
            continue
        try:
            o = broker.place(sym, s["side"], qty, float(s["price"]), s["stop_loss"], s["target"], f"backfill:{strategy}-{s['id']}",
                             signal_id=s["id"], strategy=strategy, ts=start.to_pydatetime())
        except Exception as e:
            r["result"] = f"not opened: {e}"
            rows.append(r)
            continue
        db.update_row(db.signals, s["id"], status="taken (backfill)")
        risk.state.open_positions[sym] = {"qty": o["qty"], "value": o["qty"] * o["price"], "side": s["side"], "strategy": strategy}
        open_syms.add(sym)
        r.update({"qty": o["qty"], "entry": o["price"]})
        # replay the day minute by minute (stop checked first, like the live engine)
        done = None
        for ts, b in bars.iterrows():
            if s["side"] == "BUY":
                if b.low <= s["stop_loss"]:
                    done = broker.close(o["trade_id"], min(s["stop_loss"], b.open), "stop_loss", ts.to_pydatetime())
                elif s["target"] and b.high >= s["target"]:
                    done = broker.close(o["trade_id"], s["target"], "target", ts.to_pydatetime(), slip=False)
            else:
                if b.high >= s["stop_loss"]:
                    done = broker.close(o["trade_id"], max(s["stop_loss"], b.open), "stop_loss", ts.to_pydatetime())
                elif s["target"] and b.low <= s["target"]:
                    done = broker.close(o["trade_id"], s["target"], "target", ts.to_pydatetime(), slip=False)
            if done:
                r.update({"result": f"closed at {done['exit']} {ts:%H:%M}", "exit_price": done["price"], "net_pnl": done["net_pnl"]})
                open_syms.discard(sym)
                break
        if not done:
            ltp = None
            if engine is not None:
                ltp = (engine.quote(sym) or {}).get("ltp")
            ltp = ltp or (float(bars["close"].iloc[-1]) if not bars.empty else o["price"])
            db.update_row(db.paper_trades, o["trade_id"], last_price=float(ltp))
            mtm = round((ltp - o["price"]) * o["qty"] * (1 if s["side"] == "BUY" else -1), 2)
            r.update({"result": "open now", "ltp": ltp, "mtm": mtm})
        r["minutes_replayed"] = len(bars)
        rows.append(r)
    db.log("admin", "paper_backfill", {"day": str(day.date()), "opened": sum(1 for x in rows if "entry" in x)})
    opened = [x for x in rows if "entry" in x]
    return {"day": str(day.date()), "opened": len(opened), "still_open": sum(1 for x in opened if x["result"] == "open now"),
            "closed": sum(1 for x in opened if x["result"].startswith("closed")),
            "realised": round(sum(x.get("net_pnl", 0) for x in opened), 2), "open_mtm": round(sum(x.get("mtm", 0) for x in opened), 2),
            "rows": rows, "at": datetime.now().isoformat(timespec="seconds")}
