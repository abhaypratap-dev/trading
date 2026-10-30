"""Vectorised, causal (no look-ahead) indicators for intraday NSE candles.

Every function takes a normalized OHLCV DataFrame (DatetimeIndex in IST, columns open/high/low/close/volume) and
returns a Series/DataFrame aligned to it. A value at candle t only uses candles <= t, so strategies can read
`ctx.ind` on candle t safely. Session-anchored indicators (VWAP, opening range, day high/low) restart every day;
previous-day indicators (pivots, CPR, prior high/low/close) only use completed earlier sessions.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

SESSION_OPEN = pd.Timedelta(hours=9, minutes=15)


def _day(df: pd.DataFrame) -> pd.Series:
    return pd.Series(df.index.normalize(), index=df.index)


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = up / dn.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.where(dn != 0, 100.0)


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift()
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    """Session VWAP (resets every day)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    v = df["volume"].clip(lower=0)
    g = _day(df)
    pv = (tp * v).groupby(g).cumsum()
    vv = v.groupby(g).cumsum()
    return (pv / vv.replace(0, np.nan)).fillna(tp)


def vwap_bands(df: pd.DataFrame, k: float = 2.0) -> pd.DataFrame:
    """VWAP +/- k standard deviations of typical price around VWAP (volume weighted, per session)."""
    tp = (df["high"] + df["low"] + df["close"]) / 3
    v = df["volume"].clip(lower=0)
    g = _day(df)
    vw = vwap(df)
    var = ((tp - vw) ** 2 * v).groupby(g).cumsum() / v.groupby(g).cumsum().replace(0, np.nan)
    sd = np.sqrt(var.fillna(0))
    return pd.DataFrame({"vwap": vw, "vwap_up": vw + k * sd, "vwap_dn": vw - k * sd}, index=df.index)


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    m = sma(close, n)
    sd = close.rolling(n, min_periods=n).std(ddof=0)
    return pd.DataFrame({"bb_mid": m, "bb_up": m + k * sd, "bb_dn": m - k * sd,
                         "bb_width_pct": (2 * k * sd) / m * 100}, index=close.index)


def macd(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    signal = line.ewm(span=sig, adjust=False, min_periods=sig).mean()
    return pd.DataFrame({"macd": line, "macd_signal": signal, "macd_hist": line - signal}, index=close.index)


def adx(df: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    up = df["high"].diff()
    dn = -df["low"].diff()
    plus = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr = true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    pdi = 100 * plus.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr
    mdi = 100 * minus.ewm(alpha=1 / n, adjust=False, min_periods=n).mean() / tr
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return pd.DataFrame({"adx": dx.ewm(alpha=1 / n, adjust=False, min_periods=n).mean(), "pdi": pdi, "mdi": mdi}, index=df.index)


def supertrend(df: pd.DataFrame, n: int = 10, mult: float = 3.0) -> pd.DataFrame:
    a = atr(df, n)
    hl2 = (df["high"] + df["low"]) / 2
    ub, lb = (hl2 + mult * a).to_numpy(), (hl2 - mult * a).to_numpy()
    c = df["close"].to_numpy()
    st, dirn = np.full(len(df), np.nan), np.zeros(len(df))
    fu, fl = ub.copy(), lb.copy()
    for i in range(len(df)):
        if np.isnan(ub[i]):
            continue
        if i > 0 and not np.isnan(fu[i - 1]):
            fu[i] = ub[i] if (ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
            fl[i] = lb[i] if (lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
            prev = dirn[i - 1] or 1
            dirn[i] = 1 if (prev == -1 and c[i] > fu[i - 1]) else -1 if (prev == 1 and c[i] < fl[i - 1]) else prev
        else:
            dirn[i] = 1
        st[i] = fl[i] if dirn[i] == 1 else fu[i]
    return pd.DataFrame({"supertrend": st, "st_dir": dirn}, index=df.index)


def rvol(df: pd.DataFrame, lookback_days: int = 10) -> pd.Series:
    """Relative volume: this candle's volume / average volume of the same time-of-day over the last N sessions."""
    tod = df.index.strftime("%H:%M")
    v = df["volume"]
    avg = v.groupby(tod).transform(lambda s: s.shift().rolling(lookback_days, min_periods=3).mean())
    return (v / avg.replace(0, np.nan)).fillna(1.0)


def session(df: pd.DataFrame) -> pd.DataFrame:
    """Per-session running values: day open, running high/low, minutes from open, previous day's H/L/C."""
    g = _day(df)
    out = pd.DataFrame(index=df.index)
    out["day_open"] = df["open"].groupby(g).transform("first")
    out["day_high"] = df["high"].groupby(g).cummax()
    out["day_low"] = df["low"].groupby(g).cummin()
    out["mins"] = (df.index - df.index.normalize() - SESSION_OPEN).total_seconds() / 60
    out["bar_no"] = df.groupby(g).cumcount()
    daily = df.groupby(g).agg(h=("high", "max"), l=("low", "min"), c=("close", "last"), o=("open", "first"))
    prev = daily.shift()
    out["prev_high"] = g.map(prev["h"])
    out["prev_low"] = g.map(prev["l"])
    out["prev_close"] = g.map(prev["c"])
    out["gap_pct"] = (out["day_open"] - out["prev_close"]) / out["prev_close"] * 100
    return out


def pivots(df: pd.DataFrame) -> pd.DataFrame:
    """Classic floor pivots and CPR (central pivot range) from the previous session."""
    s = session(df)
    h, l, c = s["prev_high"], s["prev_low"], s["prev_close"]
    p = (h + l + c) / 3
    bc = (h + l) / 2
    tc = 2 * p - bc
    return pd.DataFrame({"pp": p, "r1": 2 * p - l, "s1": 2 * p - h, "r2": p + (h - l), "s2": p - (h - l),
                         "r3": h + 2 * (p - l), "s3": l - 2 * (h - p),
                         "cpr_top": np.maximum(tc, bc), "cpr_bot": np.minimum(tc, bc),
                         "cpr_width_pct": (tc - bc).abs() / p * 100}, index=df.index)


def opening_range(df: pd.DataFrame, minutes: int = 15) -> pd.DataFrame:
    """High/low of the first `minutes` of the session; NaN until the range is complete (no look-ahead)."""
    s = session(df)
    g = _day(df)
    bar = (df.index.to_series().diff().dropna().mode().iloc[0] if len(df) > 1 else pd.Timedelta(minutes=5))
    in_or = s["mins"] < minutes
    hi = df["high"].where(in_or).groupby(g).transform("max")
    lo = df["low"].where(in_or).groupby(g).transform("min")
    done = (s["mins"] + bar.total_seconds() / 60) > minutes            # candle closes after the range ended
    return pd.DataFrame({"or_high": hi.where(done), "or_low": lo.where(done)}, index=df.index)


_STD_CACHE: dict = {}


def standard(df: pd.DataFrame) -> pd.DataFrame:
    """Everything most strategies need, computed once per symbol (cached: many strategies on the same candles
    share one computation, e.g. the AI picker running every strategy live)."""
    if len(df) == 0:
        return _standard(df)
    key = (len(df), df.index[0], df.index[-1], float(df["close"].iloc[-1]), float(df["volume"].iloc[-1]),
           float(df["close"].sum()))
    hit = _STD_CACHE.get(key)
    if hit is not None:
        return hit
    out = _standard(df)
    if len(_STD_CACHE) > 128:
        _STD_CACHE.clear()
    _STD_CACHE[key] = out
    return out


def _standard(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    out = pd.concat([
        pd.DataFrame({"ema9": ema(c, 9), "ema20": ema(c, 20), "ema21": ema(c, 21), "ema50": ema(c, 50),
                      "rsi": rsi(c, 14), "rsi2": rsi(c, 2), "atr": atr(df, 14), "rvol": rvol(df)}, index=df.index),
        vwap_bands(df), bollinger(c), macd(c), adx(df), session(df),
    ], axis=1)
    out["atr_pct"] = out["atr"] / c * 100
    out["pbar_high"], out["pbar_low"] = df["high"].shift(), df["low"].shift()        # previous candle
    out["pbar_open"], out["pbar_close"] = df["open"].shift(), df["close"].shift()
    out["low3"], out["high3"] = df["low"].rolling(3).min(), df["high"].rolling(3).max()
    out["low5"], out["high5"] = df["low"].rolling(5).min(), df["high"].rolling(5).max()
    return out


# Names the chart can draw: name -> (function returning DataFrame/Series, overlay on price?)
CHART = {
    "ema9": (lambda d: ema(d["close"], 9), True), "ema20": (lambda d: ema(d["close"], 20), True),
    "ema21": (lambda d: ema(d["close"], 21), True), "ema50": (lambda d: ema(d["close"], 50), True),
    "vwap": (lambda d: vwap_bands(d)[["vwap", "vwap_up", "vwap_dn"]], True),
    "bollinger": (lambda d: bollinger(d["close"])[["bb_up", "bb_mid", "bb_dn"]], True),
    "supertrend": (lambda d: supertrend(d)[["supertrend"]], True),
    "pivots": (lambda d: pivots(d)[["pp", "r1", "s1", "r2", "s2"]], True),
    "cpr": (lambda d: pivots(d)[["cpr_top", "cpr_bot"]], True),
    "prev_day": (lambda d: session(d)[["prev_high", "prev_low", "prev_close"]], True),
    "opening_range": (lambda d: opening_range(d, 15), True),
    "rsi": (lambda d: rsi(d["close"], 14), False), "macd": (lambda d: macd(d["close"]), False),
    "adx": (lambda d: adx(d), False), "rvol": (lambda d: rvol(d), False),
}
