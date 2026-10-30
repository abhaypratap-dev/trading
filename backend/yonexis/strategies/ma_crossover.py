"""Moving-average crossover (EMA fast/slow) with trend and chop filters.

  * signal: EMA9 crosses above EMA21 (BUY) / below (SELL) on this candle;
  * filters: price on the same side of VWAP; EMA50 sloping the same way over the last 3 candles; ADX >= min_adx
    (crossovers in a flat market are whipsaws);
  * entry next candle open; stop below the lowest low of the last 5 candles (above the highest high for shorts)
    with the ATR floor/ceiling; exit by ATR trailing stop (the trend decides when to leave) or rr target.
"""
from __future__ import annotations

import pandas as pd

from yonexis.sdk import BUY, SELL
from yonexis.sdk.intraday import BASE_PARAMS, IntradayStrategy


class MACrossover(IntradayStrategy):
    name = "ma_crossover"
    family = "trend"
    description = "EMA 9/21 crossover in the VWAP direction with EMA50 slope and ADX filters; ATR trailing exit."
    chart_indicators = ["ema9", "ema21", "ema50", "vwap"]
    params = {**BASE_PARAMS, "min_adx": 20.0, "trail_atr": 2.0, "rr": 3.0, "start": "09:45"}

    def extra_indicators(self, df):
        e50 = df["close"].ewm(span=50, adjust=False, min_periods=50).mean()
        return pd.DataFrame({"ema50_slope": e50 - e50.shift(3)}, index=df.index)

    def setup(self, ctx, c, ind):
        prev = ctx.ind_at(1)
        a = ind["atr"]
        if ind["adx"] < self.p["min_adx"]:
            return None
        if prev["ema9"] <= prev["ema21"] and ind["ema9"] > ind["ema21"] and c.close > ind["vwap"] and ind["ema50_slope"] > 0:
            return self.order(BUY, c.close, ind["low5"] - 0.1 * a, a, "EMA 9 crossed above 21, above VWAP, EMA50 rising")
        if prev["ema9"] >= prev["ema21"] and ind["ema9"] < ind["ema21"] and c.close < ind["vwap"] and ind["ema50_slope"] < 0:
            return self.order(SELL, c.close, ind["high5"] + 0.1 * a, a, "EMA 9 crossed below 21, below VWAP, EMA50 falling")
        return None
