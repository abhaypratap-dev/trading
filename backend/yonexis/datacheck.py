"""Data check: is the stored candle history complete and consistent?

For each symbol + interval in a date range it checks, day by day against the NSE trading calendar:
  * missing trading days (no candles at all);
  * incomplete days (fewer candles than the 09:15-15:30 session should have) and which times are missing;
  * zero-volume flat candles (open = high = low = close, volume 0): the instrument did not trade in that slot.
    Typical for a futures contract while it was still the far month - refetching cannot fix these;
  * broken rows (high below open/close, low above open/close, non-positive prices) and extreme jumps (> 12%).

The trading calendar is learnt from the data itself: a weekday is a trading day when the NIFTY index (or most stored
symbols) has candles that day, so exchange holidays are not reported as missing.
Repair refetches only the missing / incomplete days from Upstox, in as few requests as possible, then checks again.
"""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from yonexis import db

SESSION_MIN = 375                     # 09:15 -> 15:30


def _mins(interval: str) -> int:
    return {"1d": 0}.get(interval) if interval == "1d" else int(interval.rstrip("m"))


def expected_slots(interval: str) -> list[str]:
    m = _mins(interval)
    if not m:
        return []
    start = pd.Timestamp("2000-01-01 09:15")
    return [(start + pd.Timedelta(minutes=k * m)).strftime("%H:%M") for k in range(SESSION_MIN // m)]


def trading_days(start: str, end: str, symbols: list[str]) -> set:
    """Weekdays with trading, learnt from NIFTY (or the majority of the given symbols)."""
    ref = db.load_candles("NIFTY", "5m", start, end, real_only=True)
    if ref is not None and not ref.empty:
        days = set(pd.to_datetime(ref.index).normalize())
        if days:
            return days
    counts: dict = {}
    for s in symbols[:40]:
        df = db.load_candles(s, "5m", start, end, real_only=True)
        for d in (set(pd.to_datetime(df.index).normalize()) if df is not None and not df.empty else set()):
            counts[d] = counts.get(d, 0) + 1
    need = max(1, len(symbols[:40]) // 2)
    return {d for d, n in counts.items() if n >= need}


def check_one(sym: str, interval: str, start: str, end: str, tdays: set) -> dict:
    df = db.load_candles(sym, interval, start, end, real_only=True)
    out = {"symbol": sym, "interval": interval, "candles": 0, "first": None, "last": None, "expected_days": 0,
           "days": 0, "missing_days": [], "incomplete": [], "flat_zero_vol": 0, "flat_days": [], "broken": 0, "jumps": [],
           "status": "ok"}
    if df is None or df.empty:
        out["status"] = "no data"
        return out
    df.index = pd.to_datetime(df.index)
    out.update({"candles": int(len(df)), "first": str(df.index.min()), "last": str(df.index.max())})
    first_day, last_day = df.index.min().normalize(), df.index.max().normalize()
    exp_days = sorted(d for d in tdays if first_day <= d <= last_day)      # only within the instrument's own life
    have_days = set(df.index.normalize())
    out["expected_days"], out["days"] = len(exp_days), len(have_days & set(exp_days))
    out["missing_days"] = [str(d.date()) for d in exp_days if d not in have_days]
    slots = expected_slots(interval)
    if slots:
        hm = df.index.strftime("%H:%M")
        for d, g in df.groupby(df.index.normalize()):
            if d not in tdays:
                continue
            got = set(g.index.strftime("%H:%M"))
            miss = [t for t in slots if t not in got]
            if miss and len(miss) < len(slots):
                out["incomplete"].append({"day": str(d.date()), "missing": len(miss), "of": len(slots),
                                          "times": miss[:12] + (["…"] if len(miss) > 12 else [])})
        flat = (df["open"] == df["high"]) & (df["high"] == df["low"]) & (df["low"] == df["close"]) & (df["volume"].fillna(0) <= 0)
        out["flat_zero_vol"] = int(flat.sum())
        if flat.any():
            fd = flat.groupby(df.index.normalize()).sum()
            out["flat_days"] = [{"day": str(d.date()), "candles": int(n)} for d, n in fd[fd >= max(3, len(slots) // 10)].items()][:60]
    bad = (df["high"] < df[["open", "close"]].max(axis=1) - 1e-9) | (df["low"] > df[["open", "close"]].min(axis=1) + 1e-9) | \
          (df[["open", "high", "low", "close"]] <= 0).any(axis=1)
    out["broken"] = int(bad.sum())
    r = df["close"].pct_change().abs()
    j = r[r > 0.12]
    out["jumps"] = [{"ts": str(t), "pct": round(float(v) * 100, 1)} for t, v in j.items()][:20]
    gaps = len(out["missing_days"]) + len(out["incomplete"])
    if out["broken"]:
        out["status"] = "broken rows"
    elif gaps:
        out["status"] = "gaps"
    elif out["flat_days"]:
        out["status"] = "thin trading"
    return out


def verify(symbols: list[str], intervals: list[str], start: str, end: str | None, progress=lambda *a: None) -> dict:
    end = end or str(pd.Timestamp.now().date())
    tdays = trading_days(start, end, symbols)
    today = pd.Timestamp.now().normalize()
    tdays = {d for d in tdays if d < today}               # today's session is still running: not judged
    rows, k, n = [], 0, len(symbols) * len(intervals)
    for s in symbols:
        for iv in intervals:
            progress(f"checking {s} {iv} ({k + 1}/{n})", {"done": k, "total": n})
            rows.append(check_one(s, iv, start, end, tdays))
            k += 1
    tot = {"checked": len(rows), "ok": sum(r["status"] == "ok" for r in rows),
           "with_gaps": sum(r["status"] == "gaps" for r in rows), "thin": sum(r["status"] == "thin trading" for r in rows),
           "no_data": sum(r["status"] == "no data" for r in rows), "broken": sum(r["status"] == "broken rows" for r in rows),
           "missing_days": sum(len(r["missing_days"]) for r in rows),
           "incomplete_days": sum(len(r["incomplete"]) for r in rows),
           "missing_candles": sum(sum(i["missing"] for i in r["incomplete"]) for r in rows),
           "flat_candles": sum(r["flat_zero_vol"] for r in rows), "trading_days": len(tdays)}
    out = {"at": datetime.now().isoformat(timespec="seconds"), "start": start, "end": end, "intervals": intervals,
           "symbols": symbols, "totals": tot, "rows": rows}
    db.set_setting("data_check_last", {k_: out[k_] for k_ in ("at", "start", "end", "intervals", "symbols", "totals")})
    return out


def _ranges(days: list[str]) -> list[tuple[str, str]]:
    """Group days into contiguous fetch ranges (gaps of up to 4 calendar days are merged: weekends/holidays)."""
    ds = sorted(pd.Timestamp(d) for d in days)
    out = []
    for d in ds:
        if out and (d - out[-1][1]).days <= 4:
            out[-1][1] = d
        else:
            out.append([d, d])
    return [(str(a.date()), str(b.date())) for a, b in out]


def repair(rows: list[dict], start: str, end: str, progress=lambda *a: None) -> dict:
    """Refetch only the missing and incomplete days, then check again."""
    from yonexis.data.connectors.upstox import UpstoxConnector, tokens
    if not tokens.token:
        raise RuntimeError("log in to Upstox first (Data tab) - refetching needs it")
    conn = UpstoxConnector()
    todo = [r for r in rows if r.get("missing_days") or r.get("incomplete")]
    fixed, errors = [], []
    for k, r in enumerate(todo):
        days = list(r["missing_days"]) + [i["day"] for i in r["incomplete"]]
        rng = _ranges(days)
        progress(f"refetching {r['symbol']} {r['interval']}: {len(days)} days in {len(rng)} request(s) ({k + 1}/{len(todo)})",
                 {"done": k, "total": len(todo)})
        saved = 0
        for a, b in rng:
            try:
                df = conn.fetch_history(r["symbol"], a, b, r["interval"])
                if df is not None and not df.empty:
                    df.index = pd.to_datetime(df.index)
                    df = df[df.index.normalize() < pd.Timestamp.now().normalize()]   # never store a forming session
                    saved += db.save_candles(r["symbol"], df, r["interval"], "upstox")
            except Exception as e:
                errors.append({"symbol": r["symbol"], "interval": r["interval"], "range": f"{a}..{b}", "error": str(e)[:160]})
        fixed.append({"symbol": r["symbol"], "interval": r["interval"], "days": len(days), "saved": saved})
    progress("checking again", {})
    after = verify(sorted({r["symbol"] for r in rows}), sorted({r["interval"] for r in rows}), start, end)
    db.log("admin", "data_repair", {"series": len(todo), "saved": sum(f["saved"] for f in fixed), "errors": len(errors)})
    return {"refetched": fixed, "errors": errors, "after": after}


# ----------------------------------------------------------------- refined data (backtests)
REFINE_FLAT_SHARE = 0.10              # a day with >= 10% flat zero-volume candles (8 of 75 at 5m) is dropped


def day_problem(g: pd.DataFrame, interval: str, today=None) -> str | None:
    """Why one symbol's session is not fit for a backtest (None = fine): flat no-trade candles or missing candles."""
    slots = expected_slots(interval)
    if not slots or g is None or g.empty:
        return None
    idx = pd.to_datetime(g.index)
    flat = ((g["open"] == g["high"]) & (g["high"] == g["low"]) & (g["low"] == g["close"]) & (g["volume"].fillna(0) <= 0)).sum()
    need = int(np.ceil(len(slots) * REFINE_FLAT_SHARE))
    if flat >= need:
        return f"{int(flat)} flat no-trade candles"
    day = idx[0].normalize()
    today = today if today is not None else pd.Timestamp.now().normalize()
    if day < today:
        got = set(idx.strftime("%H:%M"))
        miss = sum(1 for t in slots if t not in got)
        if miss:
            return f"{miss} of {len(slots)} candles missing"
    return None


def refine(df: pd.DataFrame, interval: str, symbol: str = "") -> tuple[pd.DataFrame, list[dict]]:
    """Drop every session of this symbol that has flat no-trade candles or missing candles. Returns (kept, skipped)."""
    if df is None or df.empty or not expected_slots(interval):
        return df, []
    d = df.copy()
    d.index = pd.to_datetime(d.index)
    keep, skipped = [], []
    for day, g in d.groupby(d.index.normalize()):
        why = day_problem(g, interval)
        if why:
            skipped.append({"symbol": symbol, "day": str(day.date()), "reason": why})
        else:
            keep.append(g)
    return (pd.concat(keep) if keep else d.iloc[0:0]), skipped
