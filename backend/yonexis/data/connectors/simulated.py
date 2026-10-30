"""Synthetic NSE-session candles for testing when the market is closed or no data is loaded.

Never use simulated data to judge a strategy: it has no real edge in it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .base import Connector


def session_index(day: pd.Timestamp, minutes: int = 5) -> pd.DatetimeIndex:
    start = day.normalize() + pd.Timedelta(hours=9, minutes=15)
    end = day.normalize() + pd.Timedelta(hours=15, minutes=30)
    return pd.date_range(start, end - pd.Timedelta(minutes=minutes), freq=f"{minutes}min")


def simulate(symbol: str, start: str, end: str, minutes: int = 5, base_price: float | None = None,
             daily_vol_pct: float = 1.4, seed: int | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(seed if seed is not None else abs(hash(symbol)) % (2 ** 32))
    days = pd.bdate_range(start, end)
    price = base_price or float(rng.uniform(200, 4000))
    rows = []
    n = len(session_index(days[0], minutes)) if len(days) else 0
    step_vol = daily_vol_pct / 100 / np.sqrt(max(n, 1))
    for d in days:
        idx = session_index(d, minutes)
        gap = rng.normal(0, daily_vol_pct / 100 * 0.35)
        price *= 1 + gap
        drift = rng.normal(0, daily_vol_pct / 100 * 0.6) / n   # some days trend
        u = np.linspace(-1, 1, n)
        vol_curve = 1.0 + 0.9 * u ** 2                          # U-shaped intraday volatility
        vol_mult = rng.uniform(0.7, 1.6)
        for k, ts in enumerate(idx):
            o = price
            ret = drift + rng.normal(0, step_vol * vol_curve[k] * vol_mult)
            c = o * (1 + ret)
            wick = abs(rng.normal(0, step_vol * 0.6 * vol_curve[k])) * o
            h = max(o, c) + wick * rng.uniform(0, 1)
            lo = min(o, c) - wick * rng.uniform(0, 1)
            vol = rng.lognormal(10, 0.5) * vol_curve[k]
            rows.append((ts, round(o, 2), round(h, 2), round(lo, 2), round(c, 2), round(vol)))
            price = c
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).set_index("ts")
    df.attrs["symbol"] = symbol
    return df


class SimulatedConnector(Connector):
    name = "simulated"
    live_capable = True

    def fetch_history(self, symbol: str, start: str, end: str, interval: str = "5m") -> pd.DataFrame:
        return simulate(symbol, start, end, int(interval.rstrip("m")))
