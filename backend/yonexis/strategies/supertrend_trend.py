"""Supertrend flip with ADX confirmation - an extra trend follower widely used on NSE intraday.

  * BUY when Supertrend(10, 3) flips to up on this candle and ADX >= min_adx and price is above VWAP;
    SELL on a flip down below VWAP;
  * stop: the Supertrend line (the natural invalidation level), with the ATR floor/ceiling;
  * exit: ATR trailing stop (2.5 ATR) or rr target.
"""
from __future__ import annotations

from yonexis.sdk import BUY, SELL
from yonexis.sdk.indicators import supertrend
from yonexis.sdk.intraday import BASE_PARAMS, IntradayStrategy


class SupertrendTrend(IntradayStrategy):
    name = "supertrend_trend"
    family = "trend"
    description = "Supertrend(10,3) flip in the VWAP direction with ADX >= 20; stop on the Supertrend line, ATR trailing exit."
    chart_indicators = ["supertrend", "vwap"]
    params = {**BASE_PARAMS, "min_adx": 20.0, "trail_atr": 2.5, "rr": 3.0, "start": "09:45", "max_risk_atr": 3.0}

    def extra_indicators(self, df):
        return supertrend(df)

    def setup(self, ctx, c, ind):
        prev = ctx.ind_at(1)
        a = ind["atr"]
        if ind["adx"] < self.p["min_adx"]:
            return None
        if prev["st_dir"] == -1 and ind["st_dir"] == 1 and c.close > ind["vwap"]:
            return self.order(BUY, c.close, ind["supertrend"], a, f"Supertrend flipped up, ADX {ind['adx']:.0f}")
        if prev["st_dir"] == 1 and ind["st_dir"] == -1 and c.close < ind["vwap"]:
            return self.order(SELL, c.close, ind["supertrend"], a, f"Supertrend flipped down, ADX {ind['adx']:.0f}")
        return None
