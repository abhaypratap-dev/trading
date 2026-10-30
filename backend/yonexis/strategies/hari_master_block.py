"""Hari Master Block - volume order blocks (port of the admin's TradingView indicator "Hari Master Block").

Indicator logic, exactly as in the Pine script (length = "Volume Pivot Length", default 5):
  upper/lower   = highest high / lowest low of the last `length` candles
  os (trend)    = 0 when the candle `length` bars ago made a high above `upper`, 1 when it made a low below `lower`,
                  otherwise unchanged
  volume pivot  = the candle `length` bars ago has more volume than the `length` candles on each side of it
  Bullish OB    = volume pivot while os == 1  ->  block from that candle's low to its (high+low)/2
  Bearish OB    = volume pivot while os == 0  ->  block from that candle's (high+low)/2 to its high
  Mitigation    = a bullish block is removed once the lowest low (or lowest close, mitigation="Close") of the last
                  `length` candles falls below its bottom; a bearish block once the highest high/close rises above its top

Trading rules added on top (the indicator itself only draws blocks):
  BUY  when price comes back DOWN to the newest live bullish block's top (resting limit order at the top);
       stop below the block bottom (minus sl_buffer_pct), target = rr x risk.
  SELL when price comes back UP to the newest live bearish block's bottom; stop above the block top.
  Orders follow the newest block: when a new block forms or the block is mitigated, the order moves/cancels.
  Each block is traded at most once. Intraday: no entries after entry_cutoff, square-off 15:15 (engine).
  Blocks from earlier sessions stay live until mitigated (carry_blocks=True), as on a TradingView chart.
"""
from __future__ import annotations

from collections import deque
from datetime import time

from yonexis.sdk import BUY, SELL, Candle, Context, Signal, Strategy


class HariMasterBlock(Strategy):
    name = "hari_master_block"
    description = ("Volume order blocks (Hari Master Block indicator): BUY when price returns to the newest bullish "
                   "block, SELL when it returns to the newest bearish block; stop beyond the block, target rr x risk.")
    params = {
        "length": 5,                 # Volume Pivot Length
        "mitigation": "Wick",        # "Wick" or "Close"
        "rr": 2.0,                   # target = rr * risk
        "sl_buffer_pct": 0.05,       # extra % beyond the block edge for the stop
        "max_trades_per_day": 2,
        "entry_cutoff": "14:30",     # no new entries after this time
        "allow_short": True,
        "carry_blocks": True,        # keep live blocks from previous sessions
        "min_block_pct": 0.05,       # skip blocks thinner than this % of price
        "max_block_pct": 2.0,        # skip blocks wider than this % of price
    }

    # ---------------------------------------------------------------- state across days
    def on_new_day(self, st: dict) -> None:
        keep = {k: st[k] for k in ("bars", "os", "bull", "bear", "used", "n") if k in st} if self.p["carry_blocks"] else {}
        st.clear()
        st.update(keep)
        st["trades"] = 0

    def on_entry(self, st: dict, signal) -> None:
        st["trades"] = st.get("trades", 0) + 1
        bid = (signal.tags or {}).get("block_id")
        if bid:
            st.setdefault("used", set()).add(bid)

    # ---------------------------------------------------------------- indicator
    def _update_blocks(self, st: dict, c: Candle) -> None:
        L = int(self.p["length"])
        bars = st.setdefault("bars", deque(maxlen=2 * L + 1))
        bars.append((c.ts, c.open, c.high, c.low, c.close, c.volume))
        st["n"] = st.get("n", 0) + 1
        bull, bear = st.setdefault("bull", []), st.setdefault("bear", [])
        if len(bars) < L + 1:
            return
        last = list(bars)[-L:]                       # current candle and the L-1 before it
        upper, lower = max(b[2] for b in last), min(b[3] for b in last)
        ref = bars[-1 - L]                           # the candle `length` bars ago
        if ref[2] > upper:
            st["os"] = 0
        elif ref[3] < lower:
            st["os"] = 1
        os_ = st.get("os", 0)
        if len(bars) == 2 * L + 1:                   # volume pivot high on the middle candle
            v = [b[5] for b in bars]
            mid = v[L]
            if mid > max(v[:L]) and mid > max(v[L + 1:]):
                hl2 = (ref[2] + ref[3]) / 2
                bid = str(ref[0])
                if os_ == 1:
                    bull.insert(0, {"id": "bull-" + bid, "top": hl2, "btm": ref[3], "ts": bid})
                else:
                    bear.insert(0, {"id": "bear-" + bid, "top": ref[2], "btm": hl2, "ts": bid})
        if self.p["mitigation"] == "Close":
            t_bull, t_bear = min(b[4] for b in last), max(b[4] for b in last)
        else:
            t_bull, t_bear = lower, upper
        st["bull"] = [b for b in bull if not t_bull < b["btm"]][:20]
        st["bear"] = [b for b in bear if not t_bear > b["top"]][:20]

    def _ok(self, b: dict, px: float) -> bool:
        w = (b["top"] - b["btm"]) / px * 100
        return self.p["min_block_pct"] <= w <= self.p["max_block_pct"]

    # ---------------------------------------------------------------- trading
    def on_candle(self, ctx: Context, c: Candle):
        st = ctx.state
        self._update_blocks(st, c)
        hh, mm = map(int, self.p["entry_cutoff"].split(":"))
        if c.ts.time() < self.session_start or c.ts.time() >= time(hh, mm) or ctx.in_position:
            return None
        if st.get("trades", 0) >= self.p["max_trades_per_day"]:
            return None
        used = st.setdefault("used", set())
        buf = c.close * self.p["sl_buffer_pct"] / 100
        orders = []
        bull = next((b for b in st.get("bull", []) if b["id"] not in used), None)
        if bull and c.close > bull["top"] and self._ok(bull, c.close):
            px = bull["top"]
            sl = round(bull["btm"] - buf, 2)
            orders.append(Signal(BUY, sl, round(px + self.p["rr"] * (px - sl), 2),
                                 f"Back to bullish block {bull['btm']:.2f}-{bull['top']:.2f} (volume pivot {bull['ts'][5:16]})",
                                 {"block_id": bull["id"], "block_low": bull["btm"], "block_high": bull["top"]},
                                 entry="limit", entry_price=round(px, 2), valid_until=self.p["entry_cutoff"]))
        bear = next((b for b in st.get("bear", []) if b["id"] not in used), None)
        if self.p["allow_short"] and bear and c.close < bear["btm"] and self._ok(bear, c.close):
            px = bear["btm"]
            sl = round(bear["top"] + buf, 2)
            orders.append(Signal(SELL, sl, round(px - self.p["rr"] * (sl - px), 2),
                                 f"Back to bearish block {bear['btm']:.2f}-{bear['top']:.2f} (volume pivot {bear['ts'][5:16]})",
                                 {"block_id": bear["id"], "block_low": bear["btm"], "block_high": bear["top"]},
                                 entry="limit", entry_price=round(px, 2), valid_until=self.p["entry_cutoff"]))
        key = tuple(o.tags["block_id"] for o in orders)
        if key == st.get("armed") and ctx.has_pending:
            return None                                  # same blocks already armed
        st["armed"] = key
        oco = "ob-" + "|".join(key) if key else None
        for o in orders:
            o.oco = oco
        if orders:
            orders[0].replace_pending = True             # orders follow the newest blocks
            return orders
        if ctx.has_pending:                              # blocks gone/mitigated: cancel resting orders
            return [Signal(BUY, 0.0, None, "cancel", {}, entry="limit", entry_price=-1.0, replace_pending=True,
                           valid_until="00:00")]
        return None
