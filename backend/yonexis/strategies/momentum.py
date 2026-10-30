"""Momentum (trend + participation).

Trade only instruments that are trending strongly WITH volume, ride the move with a trailing stop.
Long (short mirror):
  * EMA9 > EMA21 > EMA50 and price above VWAP (all timeframes of the day agree);
  * ADX >= min_adx and rising, +DI > -DI;
  * relative volume of the candle >= min_rvol (same time-of-day average of the last 10 sessions);
  * strong candle: closes in the top 30% of its range, MACD histogram positive and rising;
  * not over-extended: close within max_ext_atr ATRs of EMA9.
Stop: 1.5 ATR or below EMA21, whichever is closer to price but >= min risk. Exit: ATR trailing stop
(squares off as soon as momentum fades) or rr target.
"""
from __future__ import annotations

from yonexis.sdk import BUY, SELL
from yonexis.sdk.intraday import BASE_PARAMS, IntradayStrategy


class Momentum(IntradayStrategy):
    name = "momentum"
    family = "momentum"
    description = "Ride strong trends with volume: EMA stack + VWAP side + rising ADX + high relative volume, ATR trailing stop."
    chart_indicators = ["ema9", "ema21", "ema50", "vwap", "adx"]
    params = {**BASE_PARAMS, "min_adx": 22.0, "min_rvol": 1.5, "max_ext_atr": 1.5, "trail_atr": 1.5, "rr": 3.0, "start": "09:45"}

    def setup(self, ctx, c, ind):
        prev = ctx.ind_at(1)
        a, rng = ind["atr"], c.high - c.low
        if rng <= 0 or ind["adx"] < self.p["min_adx"] or ind["adx"] <= prev["adx"] or ind["rvol"] < self.p["min_rvol"]:
            return None
        pos = (c.close - c.low) / rng
        if (ind["ema9"] > ind["ema21"] > ind["ema50"] and c.close > ind["vwap"] and ind["pdi"] > ind["mdi"] and pos >= 0.7
                and ind["macd_hist"] > 0 and ind["macd_hist"] > prev["macd_hist"] and c.close - ind["ema9"] <= self.p["max_ext_atr"] * a):
            stop = max(c.close - 1.5 * a, ind["ema21"] - 0.2 * a)
            return self.order(BUY, c.close, stop, a, f"Momentum up: ADX {ind['adx']:.0f}, RVOL {ind['rvol']:.1f}x, above VWAP",
                              {"adx": round(ind["adx"], 1), "rvol": round(ind["rvol"], 2)})
        if (ind["ema9"] < ind["ema21"] < ind["ema50"] and c.close < ind["vwap"] and ind["mdi"] > ind["pdi"] and pos <= 0.3
                and ind["macd_hist"] < 0 and ind["macd_hist"] < prev["macd_hist"] and ind["ema9"] - c.close <= self.p["max_ext_atr"] * a):
            stop = min(c.close + 1.5 * a, ind["ema21"] + 0.2 * a)
            return self.order(SELL, c.close, stop, a, f"Momentum down: ADX {ind['adx']:.0f}, RVOL {ind['rvol']:.1f}x, below VWAP",
                              {"adx": round(ind["adx"], 1), "rvol": round(ind["rvol"], 2)})
        return None
