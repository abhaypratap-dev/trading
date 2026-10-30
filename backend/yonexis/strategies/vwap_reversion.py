"""VWAP band reversion (range-day mean reversion) - an extra the list did not have.

Institutions benchmark to VWAP; on balanced (non-trending) days price oscillates around it.
  * range day: ADX below max_adx (no runaway trend);
  * SHORT when a candle pokes above VWAP + 2 sigma and closes back inside the band as a bearish candle;
    LONG mirror at VWAP - 2 sigma;
  * target VWAP (must pay min_rr), stop beyond the candle extreme + 0.2 ATR, time exit 60 minutes.
"""
from __future__ import annotations

from yonexis.sdk import BUY, SELL
from yonexis.sdk.intraday import BASE_PARAMS, IntradayStrategy


class VWAPReversion(IntradayStrategy):
    name = "vwap_reversion"
    family = "mean_reversion"
    description = "Range days: fade closes back inside the VWAP +/-2 sigma bands, target VWAP, 60-minute time exit."
    chart_indicators = ["vwap"]
    params = {**BASE_PARAMS, "max_adx": 20.0, "max_minutes": 60, "min_rr": 1.0, "start": "10:00", "entry_cutoff": "14:15"}

    def setup(self, ctx, c, ind):
        a = ind["atr"]
        if ind["adx"] > self.p["max_adx"]:
            return None
        if c.high > ind["vwap_up"] and c.close < ind["vwap_up"] and self.bearish(c, 0.4):
            return self.order(SELL, c.close, c.high + 0.2 * a, a, "Rejected at VWAP +2 sigma on a range day", target=ind["vwap"])
        if c.low < ind["vwap_dn"] and c.close > ind["vwap_dn"] and self.bullish(c, 0.4):
            return self.order(BUY, c.close, c.low - 0.2 * a, a, "Rejected at VWAP -2 sigma on a range day", target=ind["vwap"])
        return None
