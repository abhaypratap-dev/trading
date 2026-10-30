"""Point-in-time features for a signal. Only candles at or before the signal time are used.

The same function builds features for training, live scoring and the admin
feedback snapshot, so there is no training/serving skew.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

FEATURE_NAMES = [
    "side_long", "minutes_from_open", "zone_pct", "zone_atr_ratio", "vol_ratio", "dist_vwap_pct",
    "gap_pct", "prev_day_ret_pct", "day_ret_so_far_pct", "atr_pct", "rsi14", "ret_1h_pct",
    "close_pos_in_day_range", "dow",
]


def _rsi(close: pd.Series, n: int = 14) -> float:
    d = close.diff().dropna().tail(n)
    if d.empty:
        return 50.0
    up, dn = d.clip(lower=0).mean(), (-d.clip(upper=0)).mean()
    if dn == 0:
        return 100.0
    return float(100 - 100 / (1 + up / dn))


def signal_features(candles: pd.DataFrame, ts, side: str, tags: dict | None = None,
                    market: dict | None = None) -> dict:
    """candles: normalized OHLCV for ONE symbol. ts: signal candle timestamp."""
    ts = pd.Timestamp(ts)
    h = candles[(candles.index <= ts) & (candles.index >= ts - pd.Timedelta(days=20))]   # recent window is all features need
    if h.empty:
        return {k: np.nan for k in FEATURE_NAMES}
    day0 = ts.normalize()
    today = h[h.index >= day0]
    prev = h[h.index < day0]
    c = h.iloc[-1]
    tags = tags or {}

    tr = pd.concat([h["high"] - h["low"], (h["high"] - h["close"].shift()).abs(), (h["low"] - h["close"].shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.tail(14).mean()) if len(tr) else np.nan
    zl, zh = tags.get("zone_low"), tags.get("zone_high")
    zone = (zh - zl) if zl is not None and zh is not None else np.nan

    tp = (today["high"] + today["low"] + today["close"]) / 3
    vwap = float((tp * today["volume"]).sum() / today["volume"].sum()) if today["volume"].sum() > 0 else float(c["close"])
    earlier_vol = today["volume"].iloc[:-1]
    vol_ratio = float(c["volume"] / earlier_vol.mean()) if len(earlier_vol) and earlier_vol.mean() > 0 else 1.0

    prev_close = float(prev["close"].iloc[-1]) if len(prev) else np.nan
    prev_days = prev.groupby(prev.index.normalize())["close"].last() if len(prev) else pd.Series(dtype=float)
    prev_day_ret = float(prev_days.pct_change().iloc[-1] * 100) if len(prev_days) > 1 else 0.0
    day_open = float(today["open"].iloc[0])
    rng = float(today["high"].max() - today["low"].min())
    hour_ago = h[h.index <= ts - pd.Timedelta(hours=1)]

    f = {
        "side_long": 1.0 if side == "BUY" else 0.0,
        "minutes_from_open": (ts - (day0 + pd.Timedelta(hours=9, minutes=15))).total_seconds() / 60,
        "zone_pct": zone / c["close"] * 100 if zone == zone else np.nan,
        "zone_atr_ratio": zone / atr if atr and zone == zone else np.nan,
        "vol_ratio": vol_ratio,
        "dist_vwap_pct": (c["close"] - vwap) / vwap * 100,
        "gap_pct": (day_open - prev_close) / prev_close * 100 if prev_close == prev_close else 0.0,
        "prev_day_ret_pct": prev_day_ret,
        "day_ret_so_far_pct": (c["close"] - day_open) / day_open * 100,
        "atr_pct": atr / c["close"] * 100 if atr else np.nan,
        "rsi14": _rsi(h["close"]),
        "ret_1h_pct": (c["close"] / hour_ago["close"].iloc[-1] - 1) * 100 if len(hour_ago) else 0.0,
        "close_pos_in_day_range": (c["close"] - today["low"].min()) / rng if rng > 0 else 0.5,
        "dow": float(ts.dayofweek),
    }
    for k, v in (market or {}).items():      # e.g. nifty_ret_pct, india_vix
        f[f"mkt_{k}"] = v
    return {k: (float(v) if v is not None else np.nan) for k, v in f.items()}
