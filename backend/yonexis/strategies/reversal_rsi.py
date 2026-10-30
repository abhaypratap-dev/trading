"""Reversal (mean-reversion at exhaustion).

Idea: after an over-extended move, momentum exhausts and price snaps back toward fair value (VWAP).
Long setup (short is the mirror):
  1. Exhaustion: RSI(14) was below `rsi_low` on this or the previous candle AND price stretched below the lower
     VWAP band (VWAP - 2 sigma) or the lower Bollinger band.
  2. Not a trend day against us: ADX below `max_adx`, or the candle shows clear rejection.
  3. Confirmation: a bullish candle (body >= 50% of range) that closes above the previous candle's high,
     with RSI turning up.
Stop: below the lowest low of the last 3 candles minus 0.2 ATR. Target: VWAP (must still pay min_rr), else rr.
"""
from __future__ import annotations

from yonexis.sdk import BUY, SELL
from yonexis.sdk.intraday import BASE_PARAMS, IntradayStrategy


class ReversalRSI(IntradayStrategy):
    name = "reversal_rsi"
    family = "reversal"
    description = "Fade exhaustion: RSI extreme + stretch beyond VWAP/Bollinger bands, enter on a confirming reversal candle, target VWAP."
    chart_indicators = ["vwap", "bollinger", "rsi"]
    params = {**BASE_PARAMS, "rsi_low": 25.0, "rsi_high": 75.0, "max_adx": 35.0, "rr": 1.8, "start": "09:35"}

    def setup(self, ctx, c, ind):
        prev = ctx.ind_at(1)
        a, r, rp = ind["atr"], ind["rsi"], prev["rsi"]
        if ind["adx"] > self.p["max_adx"]:
            return None
        stretched_dn = c.low <= min(ind["vwap_dn"], ind["bb_dn"]) or ind["pbar_low"] <= prev["bb_dn"]
        if min(r, rp) < self.p["rsi_low"] and stretched_dn and self.bullish(c) and c.close > ind["pbar_high"] and r > rp:
            return self.order(BUY, c.close, ind["low3"] - 0.2 * a, a, f"Oversold reversal: RSI {rp:.0f}->{r:.0f}, back above prior high",
                              {"rsi": round(r, 1)}, target=ind["vwap"] if ind["vwap"] > c.close else None)
        stretched_up = c.high >= max(ind["vwap_up"], ind["bb_up"]) or ind["pbar_high"] >= prev["bb_up"]
        if max(r, rp) > self.p["rsi_high"] and stretched_up and self.bearish(c) and c.close < ind["pbar_low"] and r < rp:
            return self.order(SELL, c.close, ind["high3"] + 0.2 * a, a, f"Overbought reversal: RSI {rp:.0f}->{r:.0f}, back below prior low",
                              {"rsi": round(r, 1)}, target=ind["vwap"] if ind["vwap"] < c.close else None)
        return None
