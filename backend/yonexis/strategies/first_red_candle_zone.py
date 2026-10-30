"""First Red Candle Zone (the YONEXIS dashboard strategy).

The first red candle of the session (from 09:15 on the chosen timeframe)
defines a zone [low, high].
  entry_mode="touch" (default): as soon as price TOUCHES the zone high -> BUY, touches the zone low -> SELL
                                (resting stop orders, one cancels the other; fills at the level, or at the open on a gap)
  entry_mode="close":           a candle CLOSE above the zone high -> BUY, below the zone low -> SELL
                                (filled at the next candle's open)
Stop loss sits on the opposite side of the zone (plus a buffer); target is a
multiple of risk. One trade per day by default.

Relay (touch mode, reverse=True): once in a trade, touching the OPPOSITE edge of the zone closes it and opens the
opposite side at that level (a SELL taken at the zone low that rises back to the zone high is closed and a BUY is
opened there; its stop is the zone low, where it would flip back). Only a full traverse of the zone flips the
position - wiggles around the same edge do nothing. A target hit ends the relay for the day. No new flips after
entry_cutoff (the position is then simply stopped) and at most max_flips flips per day.
"""
from __future__ import annotations

from datetime import time

from yonexis.sdk import BUY, SELL, Candle, Context, Signal, Strategy


class FirstRedCandleZone(Strategy):
    name = "first_red_candle_zone"
    description = "First red candle of the session defines a zone; price touching the zone high = BUY, zone low = SELL (or wait for a close beyond it with entry_mode='close')."
    params = {
        "entry_mode": "touch",       # "touch" = enter the moment the level is hit; "close" = wait for a candle close beyond it
        "sl_mode": "opposite",       # "opposite" = other edge of zone, "mid" = zone middle
        "sl_buffer_pct": 0.05,       # extra % beyond the SL level
        "rr": 2.0,                   # target = rr * risk
        "max_trades_per_day": 1,
        "min_zone_pct": 0.05,        # skip zones narrower than this % of price
        "max_zone_pct": 1.5,         # skip zones wider than this % of price
        "entry_cutoff": "14:30",     # no new entries after this time
        "allow_short": True,
        "min_volume_ratio": 0.0,     # breakout candle volume / avg volume of today's earlier candles ("close" mode only)
        "reverse": True,             # relay: stopped at the opposite zone edge -> open the opposite side there ("touch" mode)
        "max_flips": 4,              # max reversals per stock per day
    }

    def on_entry(self, state: dict, signal) -> None:
        if self.p["entry_mode"] == "touch":
            state["trades"] = state.get("trades", 0) + 1

    def _relay(self, flip: int) -> bool:
        return (self.p["entry_mode"] == "touch" and self.p.get("reverse") and self.p["sl_mode"] == "opposite"
                and flip < int(self.p.get("max_flips", 0)))

    def _levels(self, side: str, lo: float, hi: float, px: float, flip: int = 0):
        mid = (lo + hi) / 2
        buf = 0.0 if self._relay(flip) else px * self.p["sl_buffer_pct"] / 100   # relay: stop exactly at the flip level
        if side == BUY:
            sl = (lo if self.p["sl_mode"] == "opposite" else mid) - buf
            return round(sl, 2), round(px + self.p["rr"] * (px - sl), 2)
        sl = (hi if self.p["sl_mode"] == "opposite" else mid) + buf
        return round(sl, 2), round(px - self.p["rr"] * (sl - px), 2)

    def _arm(self, ctx: Context, c: Candle):
        """Touch mode: resting BUY stop at the zone high and SELL stop at the zone low (one cancels the other)."""
        s = ctx.state
        if not s.get("valid") or ctx.in_position or ctx.has_pending or s.get("trades", 0) >= self.p["max_trades_per_day"]:
            return None
        hh, mm = map(int, self.p["entry_cutoff"].split(":"))
        if c.ts.time() >= time(hh, mm):
            return None
        lo, hi = s["zone"]
        tags = {"zone_low": lo, "zone_high": hi, "zone_ts": s["zone_ts"], "flip": 0}
        oco = f"zone-{s['zone_ts']}-{s.get('trades', 0)}"
        out = []
        sl, tgt = self._levels(BUY, lo, hi, hi)
        out.append(Signal(BUY, sl, tgt, f"Touched zone high {hi:.2f} (zone {lo:.2f}-{hi:.2f})", dict(tags),
                          entry="stop", entry_price=hi, oco=oco, valid_until=self.p["entry_cutoff"]))
        if self.p["allow_short"]:
            sl, tgt = self._levels(SELL, lo, hi, lo)
            out.append(Signal(SELL, sl, tgt, f"Touched zone low {lo:.2f} (zone {lo:.2f}-{hi:.2f})", dict(tags),
                              entry="stop", entry_price=lo, oco=oco, valid_until=self.p["entry_cutoff"]))
        return out

    def reversal(self, tags: dict, stopped_side: str, ts):
        flip = int(tags.get("flip", 0))
        if not self._relay(flip) or "zone_low" not in tags:
            return None
        hh, mm = map(int, self.p["entry_cutoff"].split(":"))
        if ts.time() >= time(hh, mm):
            return None
        lo, hi = tags["zone_low"], tags["zone_high"]
        side = SELL if stopped_side == BUY else BUY
        if side == SELL and not self.p["allow_short"]:
            return None
        px = hi if side == BUY else lo
        sl, tgt = self._levels(side, lo, hi, px, flip + 1)
        what = "rose back to zone high" if side == BUY else "fell back to zone low"
        return Signal(side, sl, tgt, f"Relay #{flip + 1}: {what} {px:.2f} - closed the {stopped_side} and reversed",
                      {**tags, "flip": flip + 1}, entry="stop", entry_price=px, oco=tags.get("oco"))

    def on_candle(self, ctx: Context, c: Candle) -> Signal | None:
        s = ctx.state
        t = c.ts.time()
        if t < self.session_start:
            return None

        if "zone" not in s:
            if c.close < c.open:  # first red candle
                s["zone"] = (c.low, c.high)
                s["zone_ts"] = str(c.ts)
                s["trades"] = 0
                zone_pct = (c.high - c.low) / c.close * 100
                s["valid"] = self.p["min_zone_pct"] <= zone_pct <= self.p["max_zone_pct"]
                if self.p["entry_mode"] == "touch":
                    return self._arm(ctx, c)
            return None

        if self.p["entry_mode"] == "touch":
            return self._arm(ctx, c)

        if not s.get("valid") or ctx.in_position:
            return None
        if s["trades"] >= self.p["max_trades_per_day"]:
            return None
        hh, mm = map(int, self.p["entry_cutoff"].split(":"))
        if t > time(hh, mm):
            return None

        lo, hi = s["zone"]
        side = None
        if c.close > hi:
            side = BUY
        elif c.close < lo and self.p["allow_short"]:
            side = SELL
        if side is None:
            return None

        if self.p["min_volume_ratio"] > 0:
            earlier = ctx.today["volume"].iloc[:-1]
            if len(earlier) and earlier.mean() > 0 and c.volume / earlier.mean() < self.p["min_volume_ratio"]:
                return None

        mid = (lo + hi) / 2
        buf = c.close * self.p["sl_buffer_pct"] / 100
        if side == BUY:
            sl = (lo if self.p["sl_mode"] == "opposite" else mid) - buf
            risk = c.close - sl
            tgt = c.close + self.p["rr"] * risk
        else:
            sl = (hi if self.p["sl_mode"] == "opposite" else mid) + buf
            risk = sl - c.close
            tgt = c.close - self.p["rr"] * risk
        if risk <= 0:
            return None

        s["trades"] += 1
        return Signal(
            side=side,
            stop_loss=round(sl, 2),
            target=round(tgt, 2),
            reason=f"Close {c.close:.2f} {'above' if side == BUY else 'below'} zone {lo:.2f}-{hi:.2f}",
            tags={"zone_low": lo, "zone_high": hi, "zone_ts": s["zone_ts"]},
        )
