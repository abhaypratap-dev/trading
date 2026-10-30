"""Bull flag / bear flag (continuation pattern).

Bull flag (bear flag is the mirror):
  * POLE: within the last `pole_bars` candles before the flag, price rose at least pole_atr x ATR, mostly with
    green candles and relative volume >= min_rvol (a real impulse, not drift);
  * FLAG: the following `flag_min`..`flag_max` candles drift sideways/down, retracing no more than max_retrace of
    the pole, staying above EMA21, on LOWER volume than the pole (sellers are not committed);
  * ENTRY: resting buy STOP just above the flag high -> fills the moment the breakout starts;
  * STOP: below the flag low (minus 0.1 ATR). TARGET: measured move = pole height projected from the breakout,
    which must pay at least min_rr; ATR trail optional.
"""
from __future__ import annotations

from yonexis.sdk import BUY, SELL
from yonexis.sdk.intraday import BASE_PARAMS, IntradayStrategy


class FlagPattern(IntradayStrategy):
    name = "flag_pattern"
    family = "pattern"
    description = "Bull/bear flag: strong high-volume pole, low-volume shallow consolidation, buy-stop above the flag, measured-move target."
    chart_indicators = ["ema9", "ema21", "vwap"]
    params = {**BASE_PARAMS, "pole_bars": 6, "pole_atr": 2.5, "flag_min": 3, "flag_max": 8, "max_retrace": 0.5,
              "min_rvol": 1.3, "rr": 2.0, "start": "09:50"}

    def setup(self, ctx, c, ind):
        pb, fmin, fmax = int(self.p["pole_bars"]), int(self.p["flag_min"]), int(self.p["flag_max"])
        n = int(ind["bar_no"]) + 1                          # today's candles so far
        if n < pb + fmin:
            return None
        a = ind["atr"]
        day = ctx.history.iloc[-min(n, pb + fmax):]          # only the window a flag can live in (positional, cheap)
        o, hi, lo, cl, vo = (day[k].to_numpy() for k in ("open", "high", "low", "close", "volume"))
        for fl in range(fmin, min(fmax, len(day) - pb) + 1):
            f_hi, f_lo, f_v = hi[-fl:].max(), lo[-fl:].min(), vo[-fl:].mean()
            ps = slice(len(day) - fl - pb, len(day) - fl)
            p_hi, p_lo, p_v = hi[ps].max(), lo[ps].min(), vo[ps].mean()
            move = p_hi - p_lo
            if move < self.p["pole_atr"] * a or f_v >= 0.8 * p_v:
                continue
            up = (cl[ps] > o[ps]).mean() >= 0.6 and cl[ps][-1] > o[ps][0]
            dn = (cl[ps] < o[ps]).mean() >= 0.6 and cl[ps][-1] < o[ps][0]
            if up and (p_hi - f_lo) / move <= self.p["max_retrace"] and f_hi <= p_hi * 1.001 and f_lo > ind["ema21"]:
                return self._arm(BUY, f_hi + 0.05 * a, f_lo - 0.1 * a, f_hi + move, a, fl, move)
            if dn and self.p["allow_short"] and (f_hi - p_lo) / move <= self.p["max_retrace"] and f_lo >= p_lo * 0.999 and f_hi < ind["ema21"]:
                return self._arm(SELL, f_lo - 0.05 * a, f_hi + 0.1 * a, f_lo - move, a, fl, move)
        return None

    def _arm(self, side, entry, stop, target, a, fl, move):
        what = "Bull" if side == BUY else "Bear"
        o = self.order(side, entry, stop, a, f"{what} flag: {fl}-candle flag after a {move:.2f} pole; stop order at flag {'high' if side == BUY else 'low'}",
                       {"flag_bars": fl, "pole": round(float(move), 2)}, target=target, kind="stop")
        return o
