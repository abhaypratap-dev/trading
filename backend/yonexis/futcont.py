"""Continuous futures history (needs Upstox Plus: expired-instruments API).

A futures symbol here (NIFTY-FUT, RELIANCE-FUT) always means the NEAR-MONTH contract. Its stored history used to be
the current contract's whole life, including months when it was the far month and hardly traded. This rebuilds it the
way traders read futures history: for every past expiry, the candles of the contract that was the near month in that
window (day after the previous expiry -> its own expiry), joined one after another, followed by the current
near-month contract from the day after the last expiry until today.

Prices are left as traded (no back-adjustment), so a jump at a roll is the real difference between two contracts.
That suits intraday strategies (every trade opens and closes inside one session, never across a roll).
"""
from __future__ import annotations

from datetime import datetime

import pandas as pd
from sqlalchemy import and_, delete

from yonexis import db


def plan_check() -> dict:
    """Does this Upstox login have access to expired contracts (Upstox Plus)?"""
    from yonexis.data.connectors.upstox import INDEX_KEYS, UpstoxConnector, tokens
    if not tokens.token:
        return {"ok": False, "plus": None, "message": "not logged in to Upstox today - log in on the Data tab first"}
    try:
        c = UpstoxConnector()
        ex = c.expiries(INDEX_KEYS["NIFTY"])
        if not ex:
            return {"ok": True, "plus": None, "message": "Upstox answered but listed no expired NIFTY contracts"}
        fut = c.expired_futures(INDEX_KEYS["NIFTY"], ex[-1])
        return {"ok": True, "plus": True, "expiries": len(ex), "oldest": ex[0], "latest": ex[-1],
                "sample": (fut[0].get("trading_symbol") if fut else None),
                "message": f"Upstox Plus is active: expired contracts are available ({len(ex)} NIFTY expiries, {ex[0]} to {ex[-1]})"}
    except Exception as e:
        msg = str(e)
        plus = False if any(k in msg.lower() for k in ("plus", "subscription", "not authorized", "permission", "403")) else None
        return {"ok": False, "plus": plus, "message": f"expired-contract data is not available: {msg[:300]}"}


def _expired_key(contract: dict, expiry: str) -> str:
    k = contract.get("instrument_key") or contract.get("expired_instrument_key") or ""
    if k.count("|") >= 2:
        return k
    return f"{k}|{pd.Timestamp(expiry).strftime('%d-%m-%Y')}"


def _pick_future(contracts: list[dict]) -> dict | None:
    futs = [c for c in contracts if str(c.get("instrument_type", "FUT")).upper() in ("FUT", "FUTIDX", "FUTSTK", "")] or contracts
    return futs[0] if futs else None


_EXP_CACHE: dict = {}
_KEY_CACHE: dict = {}


def monthly_expiries(conn, under: str) -> list[str]:
    """Past FUTURES expiries of an underlying: the last expiry of every month (the expiries list of an index also
    has weekly option expiries; futures are monthly). Cached for the day."""
    ck = (under, str(pd.Timestamp.now().date()))
    if ck not in _EXP_CACHE:
        last: dict = {}
        for e in conn.expiries(under):
            last[e[:7]] = max(last.get(e[:7], e), e)
        _EXP_CACHE[ck] = sorted(last.values())
    return _EXP_CACHE[ck]


def fetch_range(conn, symbol: str, start: str, end: str, interval: str) -> pd.DataFrame:
    """Near-month candles of a future for any date range, joined across rolls: each past window from the contract
    that was the near month then (expired contract), the rest from the current contract."""
    from yonexis.data.connectors.upstox import instruments
    sym = symbol.upper()
    s, e = pd.Timestamp(start).normalize(), pd.Timestamp(end).normalize()
    under = instruments.underlying_key(sym)
    exps = monthly_expiries(conn, under)
    frames, prev = [], None
    for x in exps:
        xe = pd.Timestamp(x)
        lo = s if prev is None else max(s, prev + pd.Timedelta(days=1))
        prev = xe
        hi = min(xe, e)
        if xe < s or lo > hi:
            continue
        kk = (under, x)
        if kk not in _KEY_CACHE:
            c = _pick_future(conn.expired_futures(under, x))
            _KEY_CACHE[kk] = _expired_key(c, x) if c else None
        if _KEY_CACHE[kk]:
            df = conn.expired_candles(_KEY_CACHE[kk], str(lo.date()), str(hi.date()), interval)
            if df is not None and not df.empty:
                frames.append(df)
        if xe >= e:
            break
    rest = s if prev is None else max(s, prev + pd.Timedelta(days=1))
    if rest <= e:                                     # after the last expired contract: the current one
        df = conn._fetch_history_raw(sym, str(rest.date()), str(e.date()), interval)
        if df is not None and not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames)
    df.index = pd.to_datetime(df.index)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = "ts"
    return df


def build(symbol: str, start: str, intervals: list[str], conn, progress=lambda *a: None, roll_days: int = 0) -> dict:
    from yonexis.data.connectors.upstox import instruments
    sym = symbol.upper()
    under = instruments.underlying_key(sym)
    exps, fut_of = [], {}
    for e in [e for e in conn.expiries(under) if e >= start]:
        # the expiries list also has weekly OPTION expiries (indices): keep only dates with a futures contract
        progress(f"{sym}: looking up the futures contract for expiry {e}", {})
        try:
            c = _pick_future(conn.expired_futures(under, e))
        except Exception:
            c = None
        if c:
            exps.append(e)
            fut_of[e] = c
    try:
        cur = instruments.contract(sym)
    except Exception:
        cur = None
    segments, prev = [], pd.Timestamp(start) - pd.Timedelta(days=1)
    for e in exps:                                    # expired near-month contracts, oldest first
        lo = prev + pd.Timedelta(days=1)
        hi = pd.Timestamp(e) - pd.Timedelta(days=roll_days)
        if hi >= lo:
            c = fut_of[e]
            segments.append({"expiry": e, "from": str(lo.date()), "to": str(hi.date()), "kind": "expired",
                             "key": _expired_key(c, e), "trading_symbol": c.get("trading_symbol")})
        prev = pd.Timestamp(e) - pd.Timedelta(days=roll_days)
    if cur:
        segments.append({"expiry": cur["expiry"], "from": str((prev + pd.Timedelta(days=1)).date()),
                         "to": str(pd.Timestamp.now().date()), "kind": "current", "key": cur["instrument_key"],
                         "trading_symbol": cur["trading_symbol"]})
    out = {"symbol": sym, "underlying": under, "segments": [], "candles": {}, "errors": []}
    for iv in intervals:
        frames = []
        for k, sg in enumerate(segments):
            progress(f"{sym} {iv}: contract expiring {sg['expiry']} for {sg['from']} -> {sg['to']} ({k + 1}/{len(segments)})", {})
            try:
                if sg["kind"] == "expired":
                    if "key" not in sg:
                        c = _pick_future(conn.expired_futures(under, sg["expiry"]))
                        if not c:
                            raise RuntimeError("no futures contract listed for this expiry")
                        sg["key"] = _expired_key(c, sg["expiry"])
                        sg["trading_symbol"] = c.get("trading_symbol")
                    df = conn.expired_candles(sg["key"], sg["from"], sg["to"], iv)
                else:
                    df = (conn._fetch_history_raw if hasattr(conn, "_fetch_history_raw") else conn.fetch_history)(sym, sg["from"], sg["to"], iv)
                if df is not None and not df.empty:
                    df.index = pd.to_datetime(df.index)
                    df = df[(df.index >= pd.Timestamp(sg["from"])) & (df.index < pd.Timestamp(sg["to"]) + pd.Timedelta(days=1))]
                    frames.append(df)
                    sg.setdefault("candles", {})[iv] = int(len(df))
            except Exception as e:
                out["errors"].append({"interval": iv, "expiry": sg["expiry"], "error": str(e)[:200]})
        if not frames:
            continue
        cont = pd.concat(frames)
        cont = cont[~cont.index.duplicated(keep="last")].sort_index()
        cont = cont[cont.index.normalize() < pd.Timestamp.now().normalize()]      # never store a forming session
        first, last = cont.index.min(), cont.index.max()
        with db.engine.begin() as cx:                 # replace the stored series in that window
            cx.execute(delete(db.candles).where(and_(db.candles.c.symbol == sym, db.candles.c.interval == iv,
                                                     db.candles.c.ts >= first.to_pydatetime(), db.candles.c.ts <= last.to_pydatetime())))
        out["candles"][iv] = db.save_candles(sym, cont, iv, "upstox_cont")
    out["segments"] = [{k: v for k, v in sg.items()} for sg in segments]
    meta = db.get_setting("fut_continuous") or {}
    meta[sym] = {"built": datetime.now().isoformat(timespec="seconds"), "start": start, "intervals": intervals,
                 "segments": out["segments"], "errors": out["errors"][:20], "roll_days": roll_days}
    db.set_setting("fut_continuous", meta)
    return out


def build_many(symbols: list[str], start: str, intervals: list[str], progress=lambda *a: None, roll_days: int = 0) -> dict:
    from yonexis.data.connectors.upstox import UpstoxConnector, tokens
    if not tokens.token:
        raise RuntimeError("log in to Upstox first (Data tab)")
    pc = plan_check()
    if not pc.get("ok"):
        raise RuntimeError(pc["message"])
    conn = UpstoxConnector()
    res = []
    for i, s in enumerate(symbols):
        progress(f"building continuous history for {s} ({i + 1}/{len(symbols)})", {"done": i, "total": len(symbols)})
        try:
            res.append(build(s, start, intervals, conn, progress, roll_days))
        except Exception as e:
            res.append({"symbol": s, "segments": [], "candles": {}, "errors": [{"error": str(e)[:200]}]})
    db.log("admin", "futures_continuous", {"symbols": len(symbols), "start": start, "intervals": intervals})
    return {"plan": pc, "results": res, "start": start, "intervals": intervals}
