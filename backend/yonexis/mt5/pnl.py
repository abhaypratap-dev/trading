"""Today's P&L per automated symbol, for the MT5 tab.

calls: what the automation's calls made today: the same zone rules replayed on today's Upstox 5-minute candles,
       open lots valued at the live Upstox price, after costs, whether or not MT5 filled the orders.
mt5:   what actually happened in the MT5 account: today's closed YONEXIS deals plus open YONEXIS positions.
"""
from __future__ import annotations

import time

import pandas as pd

_CACHE: dict = {"at": 0.0, "key": None, "data": None}


def day_pnl(engine, mapping: dict, auto_cfg: dict, state: dict, magic: int) -> dict:
    key = (tuple(auto_cfg.get("symbols") or []), str(auto_cfg.get("params")), magic)
    if _CACHE["data"] is not None and _CACHE["key"] == key and time.time() - _CACHE["at"] < 5:
        return _CACHE["data"]
    from yonexis.zone_lab import run_day
    params = dict(auto_cfg.get("params") or {})
    today = pd.Timestamp.now().normalize()
    rows = {}
    for sym in auto_cfg.get("symbols") or []:
        r = {"calls": None, "calls_trades": 0, "calls_open": 0, "mt5": None}
        mp = mapping.get(sym) or {}
        try:
            if engine is not None:
                df = engine.candles(sym)
                if df is not None and not df.empty:
                    d = df[(df.index >= today) & (df.index < today + pd.Timedelta(days=1))]
                    if len(d):
                        try:
                            from yonexis.data.connectors.upstox import instruments
                            lot = instruments.lot_size(sym) if sym.endswith("-FUT") else 1
                        except Exception:
                            lot = 1
                        lot = lot * float(mp.get("volume") or 1)
                        res = run_day(sym, d, params, lot)
                        ltp = (engine.quote(sym) or {}).get("ltp")
                        net = 0.0
                        for t in res["trades"]:
                            if t["exit_reason"] == "open":
                                px = float(ltp) if ltp else float(t.get("ltp") or t["entry"])
                                net += (px - t["entry"]) * t["qty"] * (1 if t["side"] == "BUY" else -1)
                                r["calls_open"] += 1
                            else:
                                net += t["net_pnl"]
                        r["calls"] = round(net, 2)
                        r["calls_trades"] = len(res["trades"])
        except Exception:
            pass
        name = (mp.get("mt5") or "").upper()
        if name:
            closed = sum(float(x.get("profit") or 0) + float(x.get("commission") or 0) + float(x.get("swap") or 0)
                         for x in (state.get("deals") or [])
                         if int(x.get("magic") or 0) == magic and (x.get("symbol") or "").upper() == name)
            opened = sum(float(x.get("profit") or 0) for x in (state.get("positions") or [])
                         if int(x.get("magic") or 0) == magic and (x.get("symbol") or "").upper() == name)
            r["mt5"] = round(closed + opened, 2)
        rows[sym] = r
    out = {"symbols": rows,
           "total_calls": round(sum(v["calls"] or 0 for v in rows.values()), 2),
           "total_mt5": round(sum(v["mt5"] or 0 for v in rows.values()), 2)}
    _CACHE.update(at=time.time(), key=key, data=out)
    return out
