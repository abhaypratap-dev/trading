"""Breakout of key support/resistance with volume.

Levels: previous day's high/low (the most watched intraday levels) and the intraday swing high/low of the last
`swing_bars` candles. Long (short mirror):
  * the candle opens at/below the level and CLOSES above it by at least 0.1 ATR (no wick-only pokes);
  * relative volume >= min_rvol and body >= 50% of the range (conviction);
  * not chasing: close no more than max_ext_atr ATRs above the level;
  * the level was respected before (price stayed below it for the previous `swing_bars` candles).
Stop: back inside the range = level - 0.5 ATR (or the breakout candle's low if tighter). Target rr.
"""
from __future__ import annotations

import pandas as pd

from yonexis.sdk import BUY, SELL
from yonexis.sdk.intraday import BASE_PARAMS, IntradayStrategy


class BreakoutSR(IntradayStrategy):
    name = "breakout_sr"
    family = "breakout"
    description = "Close beyond previous-day high/low or the intraday swing level on high relative volume; stop back inside the range."
    chart_indicators = ["prev_day", "vwap"]
    params = {**BASE_PARAMS, "swing_bars": 12, "min_rvol": 1.5, "max_ext_atr": 1.2, "rr": 2.0, "start": "09:45"}

    def extra_indicators(self, df):
        n = int(self.p["swing_bars"])
        return pd.DataFrame({"swing_hi": df["high"].shift().rolling(n).max(), "swing_lo": df["low"].shift().rolling(n).min()},
                            index=df.index)

    def setup(self, ctx, c, ind):
        a = ind["atr"]
        if ind["rvol"] < self.p["min_rvol"]:
            return None
        for name, lvl in (("previous-day high", ind["prev_high"]), ("swing high", ind["swing_hi"])):
            if lvl == lvl and c.open <= lvl and c.close >= lvl + 0.1 * a and c.close - lvl <= self.p["max_ext_atr"] * a and self.bullish(c):
                return self.order(BUY, c.close, max(lvl - 0.5 * a, c.low - 0.1 * a), a,
                                  f"Breakout above {name} {lvl:.2f} on {ind['rvol']:.1f}x volume", {"level": round(lvl, 2), "level_kind": name})
        for name, lvl in (("previous-day low", ind["prev_low"]), ("swing low", ind["swing_lo"])):
            if lvl == lvl and c.open >= lvl and c.close <= lvl - 0.1 * a and lvl - c.close <= self.p["max_ext_atr"] * a and self.bearish(c):
                return self.order(SELL, c.close, min(lvl + 0.5 * a, c.high + 0.1 * a), a,
                                  f"Breakdown below {name} {lvl:.2f} on {ind['rvol']:.1f}x volume", {"level": round(lvl, 2), "level_kind": name})
        return None
