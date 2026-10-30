"""Gap and Go.

A significant opening gap that the market keeps pushing in the same direction:
  * gap of at least min_gap_pct vs the previous close, but not an exhaustion gap (> max_gap_pct);
  * the first 5-minute candle confirms the direction (green for a gap up, red for a gap down), closes in the
    direction of the gap beyond the previous day's high/low, on relative volume >= min_rvol;
  * entry: resting STOP order at the first candle's high (gap up) / low (gap down) + 0.05 ATR, valid until
    `last_entry` -> fills the moment the continuation starts;
  * stop: the first candle's low (gap up) / high, or VWAP if tighter; ATR trailing stop rides the move.
One trade per day.
"""
from __future__ import annotations

from yonexis.sdk import BUY, SELL
from yonexis.sdk.intraday import BASE_PARAMS, IntradayStrategy


class GapAndGo(IntradayStrategy):
    name = "gap_and_go"
    family = "gap"
    description = "Gap and Go: opening gap >= 1% confirmed by the first candle and volume; stop order at the first candle's extreme."
    chart_indicators = ["prev_day", "vwap"]
    params = {**BASE_PARAMS, "min_gap_pct": 1.0, "max_gap_pct": 5.0, "min_rvol": 1.5, "last_entry": "10:30",
              "max_trades_per_day": 1, "trail_atr": 1.5, "rr": 2.5, "start": "09:20", "max_risk_atr": 3.0}

    def setup(self, ctx, c, ind):
        if ind["mins"] != 0:                       # decide on the first candle of the session only
            return None
        g, a = ind["gap_pct"], ind["atr"]
        if g != g or not (self.p["min_gap_pct"] <= abs(g) <= self.p["max_gap_pct"]) or ind["rvol"] < self.p["min_rvol"]:
            return None
        if g > 0 and c.close > c.open and c.close > ind["prev_high"]:
            o = self.order(BUY, c.high + 0.05 * a, max(c.low, min(ind["vwap"], c.low + 0.6 * (c.high - c.low))) - 0.05 * a, a,
                           f"Gap up {g:.1f}% holding: buy above first candle high {c.high:.2f}", {"gap_pct": round(g, 2)}, kind="stop")
        elif g < 0 and c.close < c.open and c.close < ind["prev_low"]:
            o = self.order(SELL, c.low - 0.05 * a, min(c.high, max(ind["vwap"], c.high - 0.6 * (c.high - c.low))) + 0.05 * a, a,
                           f"Gap down {g:.1f}% holding: sell below first candle low {c.low:.2f}", {"gap_pct": round(g, 2)}, kind="stop")
        else:
            return None
        if o is not None:
            o.valid_until = self.p["last_entry"]
        return o
