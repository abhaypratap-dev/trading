"""Broker adapters. Paper trading always, unless real orders are explicitly unlocked (see config.live_orders_allowed)."""
from __future__ import annotations

from yonexis.config import settings


from .paper import InsufficientFunds, PaperBroker, Wallet  # noqa: E402,F401


class OrdersLocked(RuntimeError):
    pass


def assert_live_orders_allowed() -> None:
    """Last line of defence: called inside every function that could send a real order."""
    if not settings.live_orders_allowed:
        raise OrdersLocked("real orders are locked: this install is paper-trading only (demo wallet)")


class KiteBroker:
    """Entry as MARKET (brokers convert to price-protected orders under SEBI rules) + an SL-M stop.
    Target is placed as a LIMIT order; whichever fills first should cancel the other (handled by the OMS poller)."""
    name = "kite"

    def __init__(self):
        from yonexis.data.connectors.kite import KiteConnector
        self.k = KiteConnector(settings.KITE_API_KEY, settings.KITE_ACCESS_TOKEN)

    def place(self, symbol: str, side: str, qty: int, price: float, stop_loss: float, target: float | None, tag: str) -> dict:
        exit_side = "SELL" if side == "BUY" else "BUY"
        entry = self.k.place_order(symbol, side, qty, "MARKET", tag=tag)
        sl = self.k.place_order(symbol, exit_side, qty, "SL-M", trigger_price=round(stop_loss, 1), tag=tag)
        tgt = self.k.place_order(symbol, exit_side, qty, "LIMIT", price=round(target, 1), tag=tag) if target else None
        return {"order_id": entry, "sl_order_id": sl, "target_order_id": tgt, "symbol": symbol, "side": side, "qty": qty,
                "status": "PLACED"}

    def square_off(self, symbol: str) -> dict:  # pragma: no cover - live only
        raise NotImplementedError("use Kite's positions API / console to square off")


class UpstoxBroker:
    """Upstox live orders with a small order manager.

    Upstox has no OCO, so a resting stop AND a resting target could both fill and flip the position.
    We place MARKET entry (market protection on) + an SL-M stop at the broker, and the engine closes
    at target itself: when LTP reaches the target it cancels the stop and exits at MARKET.
    The same happens at the square-off time.
    """
    name = "upstox"

    def __init__(self):
        from yonexis.data.connectors.upstox import UpstoxConnector
        self.u = UpstoxConnector()
        self.open: dict[str, dict] = {}
        self._last_book = 0.0

    def place(self, symbol: str, side: str, qty: int, price: float, stop_loss: float, target: float | None, tag: str) -> dict:
        exit_side = "SELL" if side == "BUY" else "BUY"
        entry = self.u.place_order(symbol, side, qty, "MARKET", tag=tag)
        try:
            sl = self.u.place_order(symbol, exit_side, qty, "SL-M", trigger_price=round(stop_loss, 2), tag=tag)
        except Exception as e:
            # Never leave a position without a stop: exit immediately.
            self.u.place_order(symbol, exit_side, qty, "MARKET", tag=tag)
            raise RuntimeError(f"stop-loss order rejected ({e}); position exited at market") from e
        self.open[symbol] = {"side": side, "exit_side": exit_side, "qty": qty, "entry_ids": entry, "sl_ids": sl,
                             "target": target, "tag": tag}
        return {"order_id": entry, "sl_order_id": sl, "target": target, "symbol": symbol, "side": side, "qty": qty,
                "status": "PLACED", "broker": "upstox"}

    def _exit(self, symbol: str, why: str) -> dict:
        p = self.open.pop(symbol)
        for oid in p["sl_ids"]:
            try:
                self.u.cancel_order(oid)
            except Exception:
                pass
        ids = self.u.place_order(symbol, p["exit_side"], p["qty"], "MARKET", tag=p["tag"])
        return {"symbol": symbol, "exit": why, "order_ids": ids}

    def manage(self, quotes: dict[str, dict], square_off: bool) -> list[dict]:
        """Call every few seconds with {symbol: {"ltp": ...}}. Returns actions taken."""
        import time as _t
        actions = []
        if self.open and _t.time() - self._last_book > 20:      # sync with the broker's order book
            self._last_book = _t.time()
            try:
                book = {o.get("order_id"): o for o in self.u.orders()}
                for sym in list(self.open):
                    p = self.open[sym]
                    st = [str(book.get(i, {}).get("status", "")).lower() for i in p["sl_ids"]]
                    en = [str(book.get(i, {}).get("status", "")).lower() for i in p["entry_ids"]]
                    if any(x == "complete" for x in st):
                        self.open.pop(sym)
                        actions.append({"symbol": sym, "exit": "stop_loss_filled"})
                    elif en and all(x in ("rejected", "cancelled") for x in en):
                        for oid in p["sl_ids"]:
                            try:
                                self.u.cancel_order(oid)
                            except Exception:
                                pass
                        self.open.pop(sym)
                        actions.append({"symbol": sym, "exit": "entry_rejected"})
            except Exception as e:
                actions.append({"error": f"order book sync failed: {e}"})
        for sym in list(self.open):
            p = self.open[sym]
            ltp = (quotes.get(sym) or {}).get("ltp")
            if square_off:
                actions.append(self._exit(sym, "square_off"))
            elif ltp is not None and p["target"] is not None and (
                    (p["side"] == "BUY" and ltp >= p["target"]) or (p["side"] == "SELL" and ltp <= p["target"])):
                actions.append(self._exit(sym, "target"))
        return actions

    def square_off(self, symbol: str) -> dict:
        return self._exit(symbol, "manual") if symbol in self.open else {"symbol": symbol, "status": "not open"}


def get_broker():
    """Paper unless real orders were deliberately unlocked (TRADING_MODE=live AND LIVE_ORDERS_UNLOCK phrase)."""
    if settings.live_orders_allowed and settings.BROKER == "upstox":
        return UpstoxBroker()
    if settings.live_orders_allowed and settings.BROKER == "kite":
        return KiteBroker()
    return PaperBroker()
