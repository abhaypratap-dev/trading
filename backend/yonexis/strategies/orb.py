"""Opening Range Breakout (ORB).

Range = high/low of the first `or_minutes` (15 or 30) of the session. After it completes:
  * skip the day if the range is too narrow (< min_or_pct, noise) or too wide (> max_or_pct, risk too big);
  * BUY when a candle CLOSES above the range high with relative volume >= min_rvol (volume-confirmed, avoids the
    classic false ORB), SELL below the range low; entries only until `last_entry`;
  * stop: the middle of the range (or the opposite side with stop_mode="opposite"); target rr x risk, plus an
    ATR trailing stop once in profit.
One trade per day.
"""
from __future__ import annotations

from yonexis.sdk import BUY, SELL
from yonexis.sdk.indicators import opening_range
from yonexis.sdk.intraday import BASE_PARAMS, IntradayStrategy


class OpeningRangeBreakout(IntradayStrategy):
    name = "orb"
    family = "orb"
    description = "Opening Range Breakout: first 15/30 minutes set the range; volume-confirmed close beyond it; stop at range middle."
    chart_indicators = ["opening_range", "vwap"]
    params = {**BASE_PARAMS, "or_minutes": 15, "min_or_pct": 0.25, "max_or_pct": 1.8, "min_rvol": 1.3, "stop_mode": "mid",
              "last_entry": "11:30", "max_trades_per_day": 1, "rr": 2.0, "trail_atr": 2.0, "start": "09:30"}

    def extra_indicators(self, df):
        return opening_range(df, int(self.p["or_minutes"]))

    def setup(self, ctx, c, ind):
        hi, lo, a = ind["or_high"], ind["or_low"], ind["atr"]
        if hi != hi or c.ts.strftime("%H:%M") > self.p["last_entry"]:
            return None
        w = (hi - lo) / c.close * 100
        if not (self.p["min_or_pct"] <= w <= self.p["max_or_pct"]) or ind["rvol"] < self.p["min_rvol"]:
            return None
        mid = (hi + lo) / 2
        if c.close > hi and c.open <= hi * 1.002:
            return self.order(BUY, c.close, mid if self.p["stop_mode"] == "mid" else lo, a,
                              f"ORB up: closed above {self.p['or_minutes']}-min high {hi:.2f} on {ind['rvol']:.1f}x volume",
                              {"or_high": round(hi, 2), "or_low": round(lo, 2)})
        if c.close < lo and c.open >= lo * 0.998:
            return self.order(SELL, c.close, mid if self.p["stop_mode"] == "mid" else hi, a,
                              f"ORB down: closed below {self.p['or_minutes']}-min low {lo:.2f} on {ind['rvol']:.1f}x volume",
                              {"or_high": round(hi, 2), "or_low": round(lo, 2)})
        return None
