"""Pivot point trading (classic floor pivots + CPR from the previous session).

The width of the Central Pivot Range sets the day type:
  * NARROW CPR (cpr_width_pct <= narrow_cpr): a trending day is likely -> trade BREAKOUTS. Long when a candle
    closes above R1 having opened below it, while price is above the CPR; target R2, stop R1 - 0.4 ATR.
  * otherwise -> trade BOUNCES. Long when the candle's low tags S1 (within 0.15 ATR) and it closes back above S1
    as a bullish candle; target PP, stop S1 - 0.5 ATR. (Shorts mirror at R1/S1.)
"""
from __future__ import annotations

import pandas as pd

from yonexis.sdk import BUY, SELL
from yonexis.sdk.indicators import pivots
from yonexis.sdk.intraday import BASE_PARAMS, IntradayStrategy


class PivotPoints(IntradayStrategy):
    name = "pivot_points"
    family = "pivot"
    description = "Previous-day pivots and CPR: breakouts of R1/S1 on narrow-CPR (trend) days, bounces off S1/R1 back to PP otherwise."
    chart_indicators = ["pivots", "cpr"]
    params = {**BASE_PARAMS, "narrow_cpr": 0.25, "start": "09:30"}

    def extra_indicators(self, df):
        return pivots(df)

    def setup(self, ctx, c, ind):
        a = ind["atr"]
        if ind["pp"] != ind["pp"]:
            return None
        if ind["cpr_width_pct"] <= self.p["narrow_cpr"]:
            if c.open < ind["r1"] <= c.close and c.close > ind["cpr_top"] and self.bullish(c):
                return self.order(BUY, c.close, ind["r1"] - 0.4 * a, a, f"Narrow CPR day: broke R1 {ind['r1']:.2f}",
                                  {"mode": "breakout", "level": round(ind["r1"], 2)}, target=ind["r2"])
            if c.open > ind["s1"] >= c.close and c.close < ind["cpr_bot"] and self.bearish(c):
                return self.order(SELL, c.close, ind["s1"] + 0.4 * a, a, f"Narrow CPR day: broke S1 {ind['s1']:.2f}",
                                  {"mode": "breakout", "level": round(ind["s1"], 2)}, target=ind["s2"])
            return None
        if abs(c.low - ind["s1"]) <= 0.15 * a and c.close > ind["s1"] and self.bullish(c, 0.4):
            return self.order(BUY, c.close, ind["s1"] - 0.5 * a, a, f"Bounce off S1 {ind['s1']:.2f}",
                              {"mode": "bounce", "level": round(ind["s1"], 2)}, target=ind["pp"])
        if abs(c.high - ind["r1"]) <= 0.15 * a and c.close < ind["r1"] and self.bearish(c, 0.4):
            return self.order(SELL, c.close, ind["r1"] + 0.5 * a, a, f"Rejection at R1 {ind['r1']:.2f}",
                              {"mode": "bounce", "level": round(ind["r1"], 2)}, target=ind["pp"])
        return None
