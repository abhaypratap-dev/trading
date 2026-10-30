"""Order flow from the Upstox full feed (5-level market depth on the standard plan).

Per tick we see: last trade price/qty, cumulative day volume (vtt), the top-5 bid/ask ladder, and the exchange's
total pending buy/sell quantity (tbq/tsq). From that, per symbol and per minute:
  * depth imbalance  = (sum of 5 bid qty - sum of 5 ask qty) / (sum of both), -1..+1 (who is waiting to trade);
  * tbq/tsq          = total pending buy vs sell quantity on the whole book;
  * aggressor volume = traded volume classified as BUY when the trade printed at/above the best ask (buyers
                       lifting offers), SELL at/below the best bid, otherwise by the tick rule;
  * delta            = buy volume - sell volume (cumulative delta is the running sum over the day);
  * big prints       = single volume increments >= big_mult x the symbol's recent average increment;
  * spread           = best ask - best bid in basis points.
Minute rows are written to the `orderflow` table so the AI can learn from them once enough history exists.
"""
from __future__ import annotations

import threading
from collections import deque

import pandas as pd

BIG_MULT = 8.0


class OrderFlow:
    def __init__(self):
        self._lock = threading.Lock()
        self.book: dict[str, dict] = {}            # latest snapshot per symbol (ladder, tbq/tsq, ltp)
        self.cur: dict[str, dict] = {}             # current minute accumulator per symbol
        self.done: list[dict] = []                 # finished minutes waiting to be written
        self.recent: dict[str, deque] = {}         # last 120 finished minutes per symbol (for the UI)
        self._last: dict[str, dict] = {}           # previous ltp / vtt / avg increment

    def on_full(self, sym: str, ts: pd.Timestamp, ltp: float | None, vtt: float | None, depth: list[dict],
                tbq: float | None, tsq: float | None) -> None:
        bids = [(float(q.get("bidP") or 0), float(q.get("bidQ") or 0)) for q in depth if q.get("bidP")]
        asks = [(float(q.get("askP") or 0), float(q.get("askQ") or 0)) for q in depth if q.get("askP")]
        bq, aq = sum(q for _, q in bids), sum(q for _, q in asks)
        best_bid = bids[0][0] if bids else None
        best_ask = asks[0][0] if asks else None
        m = ts.floor("min")
        with self._lock:
            prev = self._last.get(sym, {})
            inc = 0.0
            if vtt is not None and prev.get("vtt") is not None and vtt >= prev["vtt"]:
                inc = float(vtt) - prev["vtt"]
            side = 0
            if inc > 0 and ltp is not None:
                pa, pb = prev.get("ask"), prev.get("bid")
                if pa and ltp >= pa:
                    side = 1
                elif pb and ltp <= pb:
                    side = -1
                elif prev.get("ltp") is not None:
                    side = 1 if ltp > prev["ltp"] else -1 if ltp < prev["ltp"] else prev.get("side", 0)
            avg = prev.get("avg_inc") or inc or 1.0
            big = inc > 0 and inc >= BIG_MULT * avg
            self._last[sym] = {"vtt": vtt if vtt is not None else prev.get("vtt"), "ltp": ltp if ltp is not None else prev.get("ltp"),
                               "bid": best_bid or prev.get("bid"), "ask": best_ask or prev.get("ask"), "side": side or prev.get("side", 0),
                               "avg_inc": (0.97 * avg + 0.03 * inc) if inc > 0 else avg}
            c = self.cur.get(sym)
            if c is None or c["ts"] != m:
                if c is not None:
                    self._finish(sym, c)
                c = self.cur[sym] = {"ts": m, "symbol": sym, "buy_vol": 0.0, "sell_vol": 0.0, "big_buys": 0, "big_sells": 0}
            if side > 0:
                c["buy_vol"] += inc
                c["big_buys"] += int(big)
            elif side < 0:
                c["sell_vol"] += inc
                c["big_sells"] += int(big)
            c.update({"bid_qty": bq, "ask_qty": aq, "imbalance": (bq - aq) / (bq + aq) if bq + aq > 0 else 0.0,
                      "tbq": tbq, "tsq": tsq,
                      "spread_bps": (best_ask - best_bid) / best_bid * 1e4 if best_bid and best_ask else None})
            self.book[sym] = {"ts": str(ts), "ltp": ltp, "bids": bids, "asks": asks, "tbq": tbq, "tsq": tsq,
                              "imbalance": round(c["imbalance"], 3)}

    def _finish(self, sym: str, c: dict) -> None:
        c = {**c, "delta": c["buy_vol"] - c["sell_vol"]}
        self.done.append(c)
        self.recent.setdefault(sym, deque(maxlen=120)).append(c)

    def drain(self) -> list[dict]:
        """Finished minutes since the last call (for the database)."""
        with self._lock:
            out, self.done = self.done, []
        return out

    def snapshot(self, sym: str) -> dict:
        with self._lock:
            rows = list(self.recent.get(sym, []))
            cur = self.cur.get(sym)
            book = self.book.get(sym)
        if cur:
            rows = rows + [{**cur, "delta": cur["buy_vol"] - cur["sell_vol"], "forming": True}]
        cum, out = 0.0, []
        day = None
        for r in rows:
            if day != r["ts"].normalize():
                day, cum = r["ts"].normalize(), 0.0
            cum += r["delta"]
            out.append({**r, "ts": str(r["ts"]), "cum_delta": cum})
        last = out[-15:]
        bv, sv = sum(r["buy_vol"] for r in last), sum(r["sell_vol"] for r in last)
        return {"book": book, "minutes": out, "summary": {
            "imbalance": book["imbalance"] if book else None,
            "tbq_tsq": round(book["tbq"] / book["tsq"], 2) if book and book.get("tbq") and book.get("tsq") else None,
            "buy_pct_15m": round(bv / (bv + sv) * 100, 1) if bv + sv > 0 else None,
            "cum_delta": round(out[-1]["cum_delta"], 0) if out else None,
            "big_buys_15m": sum(r["big_buys"] for r in last), "big_sells_15m": sum(r["big_sells"] for r in last)}}


flow = OrderFlow()
