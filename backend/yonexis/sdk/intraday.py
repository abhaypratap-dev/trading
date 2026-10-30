"""Shared base for the professional intraday strategy library.

Every library strategy gets the same discipline, so results are comparable and the AI picker can rank them:
  * decisions only on closed 5-minute candles; entries as market (next open), stop (breakout) or limit (pullback);
  * an entry window (start .. entry_cutoff) and a daily trade cap; cooldown after an exit;
  * volatility sanity filters (ATR% band, minimum price) so dead or wild instruments are skipped;
  * risk from STRUCTURE (swing low, range edge, level) with an ATR floor/ceiling: if the structural stop is too far
    (more than max_risk_atr x ATR) the trade is skipped instead of taking an oversized loss;
  * target = rr x risk, or a structural target (VWAP, pivot, measured move) that must still pay at least min_rr;
  * optional ATR trailing stop and time exit; engine square-off at 15:15 always applies.
"""
from __future__ import annotations

import math
from datetime import time
from typing import Any

import pandas as pd

from .indicators import standard
from .strategy import BUY, SELL, Candle, Context, Signal, Strategy

BASE_PARAMS: dict[str, Any] = {
    "rr": 2.0,                 # reward:risk for the default target
    "min_rr": 1.2,             # a structural target must pay at least this
    "max_risk_atr": 2.5,       # skip if the structural stop is further than this many ATRs
    "min_risk_atr": 0.4,       # stops closer than this many ATRs are widened (noise)
    "trail_atr": 0.0,          # >0: ATR trailing stop distance (0 = fixed stop)
    "max_minutes": 0,          # >0: time exit after this many minutes
    "max_trades_per_day": 2,
    "start": "09:20",          # first candle (close time) a setup may trigger on
    "entry_cutoff": "14:30",
    "cooldown_bars": 3,        # candles to wait after an exit before a new entry
    "allow_short": True,
    "min_atr_pct": 0.12,       # skip instruments moving less than this % per 5-min candle (costs eat the edge)
    "max_atr_pct": 2.5,
    "min_price": 20.0,
}


def _t(s: str) -> time:
    h, m = map(int, s.split(":"))
    return time(h, m)


class IntradayStrategy(Strategy):
    """Subclass and implement setup(ctx, c, ind) -> Signal | list[Signal] | None."""

    family = "custom"
    chart_indicators = ["vwap", "ema9", "ema21"]
    params: dict[str, Any] = dict(BASE_PARAMS)

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        base = standard(df)
        extra = self.extra_indicators(df)
        out = base if extra is None else pd.concat([base, extra], axis=1)
        return out.loc[:, ~out.columns.duplicated(keep="last")]

    def extra_indicators(self, df: pd.DataFrame) -> pd.DataFrame | None:
        return None

    # ---------------------------------------------------------------- bookkeeping
    def on_new_day(self, st: dict) -> None:
        keep = {k: st[k] for k in list(st) if k.startswith("keep_")}
        st.clear()
        st.update(keep)
        st["trades"] = 0

    def on_entry(self, st: dict, signal) -> None:
        st["trades"] = st.get("trades", 0) + 1

    def on_candle(self, ctx: Context, c: Candle):
        st = ctx.state
        st["i"] = st.get("i", 0) + 1
        if st.get("was_in") and not ctx.in_position:
            st["last_exit_i"] = st["i"]
        st["was_in"] = ctx.in_position
        t = (c.ts + pd.Timedelta(minutes=5)).time()                   # candle CLOSE time
        if ctx.in_position or ctx.has_pending and not self.manages_pending:
            return None
        if not (_t(self.p["start"]) <= t <= _t(self.p["entry_cutoff"])):
            return None
        if st.get("trades", 0) >= self.p["max_trades_per_day"]:
            return None
        if st["i"] - st.get("last_exit_i", -10**9) < self.p["cooldown_bars"]:
            return None
        ind = ctx.ind
        a = ind["atr"]
        if not a == a or c.close < self.p["min_price"]:
            return None
        ap = a / c.close * 100
        if not (self.p["min_atr_pct"] <= ap <= self.p["max_atr_pct"]):
            return None
        return self.setup(ctx, c, ind)

    manages_pending = False      # True: setup() is called even while its own resting orders exist (to move/cancel them)

    def setup(self, ctx: Context, c: Candle, ind) -> Signal | list[Signal] | None:  # pragma: no cover - abstract
        raise NotImplementedError

    # ---------------------------------------------------------------- order builder
    def order(self, side: str, entry: float, stop: float, atr: float, reason: str, tags: dict | None = None,
              target: float | None = None, kind: str = "market", rr: float | None = None) -> Signal | None:
        """Build a risk-checked order. stop = structural stop; target None = rr x risk."""
        if side == SELL and not self.p["allow_short"]:
            return None
        d = 1 if side == BUY else -1
        risk = (entry - stop) * d
        if risk <= 0 or not math.isfinite(risk):
            return None
        if risk < self.p["min_risk_atr"] * atr:
            risk = self.p["min_risk_atr"] * atr
            stop = entry - d * risk
        if risk > self.p["max_risk_atr"] * atr:
            return None
        if target is not None:
            if (target - entry) * d < self.p["min_rr"] * risk:
                return None
        else:
            target = entry + d * (rr or self.p["rr"]) * risk
        trail = self.p["trail_atr"] * atr if self.p["trail_atr"] > 0 else None
        tags = {**(tags or {}), "family": self.family, "risk": round(risk, 4), "atr": round(atr, 4)}
        return Signal(side, round(stop, 2), round(target, 2) if target else None, reason, tags,
                      entry=kind, entry_price=round(entry, 2) if kind != "market" else None,
                      valid_until=self.p["entry_cutoff"] if kind != "market" else None,
                      trail=round(trail, 4) if trail else None,
                      max_minutes=int(self.p["max_minutes"]) or None)

    @staticmethod
    def bullish(c: Candle, min_body: float = 0.5) -> bool:
        r = c.high - c.low
        return c.close > c.open and r > 0 and (c.close - c.open) / r >= min_body

    @staticmethod
    def bearish(c: Candle, min_body: float = 0.5) -> bool:
        r = c.high - c.low
        return c.close < c.open and r > 0 and (c.open - c.close) / r >= min_body
