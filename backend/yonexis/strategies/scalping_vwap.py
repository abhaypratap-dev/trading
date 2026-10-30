"""Scalping (quick, cost-aware).

Many small, fast trades in the direction of the intraday bias, exiting quickly. On 5-minute decisions with
1-minute execution this is a "micro pullback" scalp:
  * bias: price on one side of VWAP and EMA9 vs EMA21 agrees;
  * the previous candle dipped to EMA9 (low within 0.15 ATR of it) and this candle closes back above the
    previous high (resumption);
  * cost filter: the target move must be at least `min_edge_x` times the round-trip cost (brokerage+STT+slippage
    approximated by cost_pct), otherwise the scalp cannot pay for itself.
Stop 0.6 ATR, target 1.0 x risk, time exit after `max_minutes`. Up to 4 trades a day.
"""
from __future__ import annotations

from yonexis.sdk import BUY, SELL
from yonexis.sdk.intraday import BASE_PARAMS, IntradayStrategy


class ScalpingVWAP(IntradayStrategy):
    name = "scalping_vwap"
    family = "scalping"
    description = "Fast micro-pullback scalps to EMA9 in the VWAP direction; tight stop, 1R target, 20-minute time exit, cost filter."
    chart_indicators = ["ema9", "ema21", "vwap"]
    params = {**BASE_PARAMS, "rr": 1.0, "min_rr": 0.9, "max_minutes": 20, "max_trades_per_day": 4, "cooldown_bars": 1,
              "stop_atr": 0.6, "min_risk_atr": 0.3, "cost_pct": 0.06, "min_edge_x": 3.0, "start": "09:30", "entry_cutoff": "14:45"}

    def setup(self, ctx, c, ind):
        prev = ctx.ind_at(1)
        a = ind["atr"]
        p = {"low": ind["pbar_low"], "high": ind["pbar_high"]}
        edge_ok = (self.p["stop_atr"] * a * self.p["rr"]) / c.close * 100 >= self.p["min_edge_x"] * self.p["cost_pct"]
        if not edge_ok:
            return None
        if c.close > ind["vwap"] and ind["ema9"] > ind["ema21"] and abs(p["low"] - prev["ema9"]) <= 0.15 * a and c.close > p["high"] and c.close > c.open:
            return self.order(BUY, c.close, c.close - self.p["stop_atr"] * a, a, "Scalp long: dip to EMA9 bought back above VWAP")
        if c.close < ind["vwap"] and ind["ema9"] < ind["ema21"] and abs(p["high"] - prev["ema9"]) <= 0.15 * a and c.close < p["low"] and c.close < c.open:
            return self.order(SELL, c.close, c.close + self.p["stop_atr"] * a, a, "Scalp short: pop to EMA9 sold back below VWAP")
        return None
