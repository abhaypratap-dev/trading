"""Pullback in an established trend (buy the dip / sell the rally).

  * trend: EMA20 > EMA50, price above VWAP, ADX >= min_adx (shorts mirror);
  * pullback: within the last 3 candles price came back to EMA20 or VWAP (low within 0.2 ATR of it) while RSI(14)
    cooled into the 38-55 zone, without closing below EMA50 (trend intact);
  * trigger: a bullish candle that closes above the previous candle's high (the dip is being bought);
  * stop: below the pullback low - 0.2 ATR; target: the day's high so far if it pays min_rr, else rr.
"""
from __future__ import annotations

from yonexis.sdk import BUY, SELL
from yonexis.sdk.intraday import BASE_PARAMS, IntradayStrategy


class Pullback(IntradayStrategy):
    name = "pullback"
    family = "pullback"
    description = "Trend pullback: in an EMA20>EMA50 trend above VWAP, buy when a dip to EMA20/VWAP is bought back (RSI 38-55)."
    chart_indicators = ["ema20", "ema50", "vwap", "rsi"]
    params = {**BASE_PARAMS, "min_adx": 18.0, "rr": 2.0, "start": "09:45"}

    def setup(self, ctx, c, ind):
        a = ind["atr"]
        if ind["adx"] < self.p["min_adx"] or not (38 <= min(ind["rsi"], ctx.ind_at(1)["rsi"], ctx.ind_at(2)["rsi"]) <= 55
                                                   or 45 <= max(ind["rsi"], ctx.ind_at(1)["rsi"], ctx.ind_at(2)["rsi"]) <= 62):
            return None
        if ind["ema20"] > ind["ema50"] and c.close > ind["vwap"] and c.close > ind["ema50"]:
            touched = min(abs(ind["low3"] - ind["ema20"]), abs(ind["low3"] - ind["vwap"])) <= 0.2 * a
            if touched and self.bullish(c, 0.4) and c.close > ind["pbar_high"] and ind["low3"] > ind["ema50"] - 0.2 * a:
                tgt = ind["day_high"] if ind["day_high"] > c.close else None
                return self.order(BUY, c.close, ind["low3"] - 0.2 * a, a, "Pullback to EMA20/VWAP bought back in an uptrend", target=tgt)
        if ind["ema20"] < ind["ema50"] and c.close < ind["vwap"] and c.close < ind["ema50"]:
            touched = min(abs(ind["high3"] - ind["ema20"]), abs(ind["high3"] - ind["vwap"])) <= 0.2 * a
            if touched and self.bearish(c, 0.4) and c.close < ind["pbar_low"] and ind["high3"] < ind["ema50"] + 0.2 * a:
                tgt = ind["day_low"] if ind["day_low"] < c.close else None
                return self.order(SELL, c.close, ind["high3"] + 0.2 * a, a, "Rally to EMA20/VWAP sold in a downtrend", target=tgt)
        return None
