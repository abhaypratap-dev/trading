"""Paper trading: every call is filled against live prices and recorded in the database. Nothing goes to a broker.

Fill model (deliberately a little pessimistic):
  * Entry at the live last-traded price when the call is made, plus slippage.
  * Stop: BUY exits at min(stop, LTP), SELL at max(stop, LTP), plus slippage, so gaps through the stop hurt.
    The stop is checked before the target when both are touched in the same update.
  * Target: filled at the target price (a resting limit order).
  * Square-off at 15:15 at the live price, plus slippage.
  * Full Indian intraday costs (backtest/costs.py) are deducted from the demo wallet on exit.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta

from sqlalchemy import insert, select, update

from yonexis import db
from yonexis.backtest.costs import CostModel
from yonexis.config import settings

log = logging.getLogger("yonexis.paper")


class InsufficientFunds(RuntimeError):
    pass


class Wallet:
    """Demo money for paper trading. One row; every change is written to the ledger."""

    def __init__(self, default_balance: float | None = None):
        self.default = float(default_balance or settings.PAPER_STARTING_BALANCE)
        self._lock = threading.Lock()

    def _row(self, cx) -> dict:
        r = cx.execute(select(db.wallet).where(db.wallet.c.id == 1)).mappings().first()
        if r is None:
            cx.execute(insert(db.wallet).values(id=1, balance=self.default, starting_balance=self.default))
            cx.execute(insert(db.wallet_ledger).values(ts=datetime.now(), kind="reset", amount=self.default, balance_after=self.default,
                                                       note="initial demo money"))
            r = cx.execute(select(db.wallet).where(db.wallet.c.id == 1)).mappings().first()
        return dict(r)

    def get(self) -> dict:
        with db.engine.begin() as cx:
            return self._row(cx)

    def blocked(self) -> float:
        rows = db.fetch(db.paper_trades, db.paper_trades.c.status == "open")
        return round(sum(r["margin"] or 0 for r in rows), 2)

    def available(self) -> float:
        return round(self.get()["balance"] - self.blocked(), 2)

    def _move(self, amount: float, kind: str, ref: str = "", note: str = "", ts: datetime | None = None) -> float:
        with self._lock, db.engine.begin() as cx:
            w = self._row(cx)
            bal = round(w["balance"] + amount, 2)
            cx.execute(update(db.wallet).where(db.wallet.c.id == 1).values(balance=bal, updated_at=datetime.utcnow()))
            cx.execute(insert(db.wallet_ledger).values(ts=ts or datetime.now(), kind=kind, amount=round(amount, 2),
                                                       balance_after=bal, ref=ref, note=note))
            return bal

    def deposit(self, amount: float, note: str = "") -> float:
        if amount <= 0:
            raise ValueError("amount must be positive")
        return self._move(amount, "deposit", note=note or "demo top-up")

    def reset(self, amount: float | None = None) -> float:
        if db.fetch(db.paper_trades, db.paper_trades.c.status == "open"):
            raise ValueError("close open paper positions before resetting the wallet")
        amount = float(amount or self.default)
        with self._lock, db.engine.begin() as cx:
            self._row(cx)
            cx.execute(update(db.wallet).where(db.wallet.c.id == 1).values(balance=amount, starting_balance=amount,
                                                                           updated_at=datetime.utcnow()))
            cx.execute(insert(db.wallet_ledger).values(ts=datetime.now(), kind="reset", amount=amount, balance_after=amount, note="wallet reset"))
        return amount

    def book(self, net_pnl: float, trade_id: int, note: str, ts: datetime | None = None) -> float:
        return self._move(net_pnl, "trade", ref=f"paper_trade:{trade_id}", note=note, ts=ts)


def margins() -> tuple[float, float]:
    """(stock intraday leverage, futures margin %) - editable in Paper Wallet, defaults from .env."""
    m = db.get_setting("paper_margin", {}) or {}
    lev = float(m.get("equity_leverage") or settings.PAPER_LEVERAGE)
    fut = float(m.get("fut_margin_pct") or settings.FUT_MARGIN_PCT)
    return max(1.0, min(lev, 10.0)), max(1.0, min(fut, 100.0))


def margin_needed(symbol: str, qty: float, price: float) -> float:
    lev, fut = margins()
    if str(symbol).upper().endswith("-FUT"):
        return round(qty * price * fut / 100, 2)
    return round(qty * price / lev, 2)


class PaperBroker:
    name = "paper"

    def __init__(self, costs: CostModel | None = None, slippage_bps: float | None = None, leverage: float | None = None):
        self.costs = costs or CostModel()
        self.slippage_bps = settings.PAPER_SLIPPAGE_BPS if slippage_bps is None else slippage_bps
        self._leverage = leverage
        self.wallet = Wallet()
        self._lock = threading.Lock()
        self.restore_risk()

    # ------------------------------------------------------------------ helpers
    def _slip(self, px: float, side: str, entering: bool) -> float:
        up = (side == "BUY") == entering
        k = self.slippage_bps / 10_000
        return round(px * (1 + k if up else 1 - k), 2)

    def open_trades(self) -> list[dict]:
        return db.fetch(db.paper_trades, db.paper_trades.c.status == "open", order=db.paper_trades.c.id)

    def restore_risk(self) -> None:
        """After a restart, tell the risk engine about open paper positions and today's realised P&L."""
        try:
            from yonexis.risk import risk
            for t in self.open_trades():
                risk.state.open_positions[t["symbol"]] = {"qty": t["qty"], "value": t["qty"] * t["entry_price"],
                                                          "side": t["side"], "strategy": t["strategy"]}
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            done = [t for t in db.fetch(db.paper_trades, db.paper_trades.c.status == "closed")
                    if t["exit_ts"] and t["exit_ts"] >= today]
            risk.state.day = datetime.now().strftime("%Y-%m-%d")
            risk.state.realized_pnl = round(sum(t["net_pnl"] or 0 for t in done), 2)
        except Exception as e:  # pragma: no cover
            log.warning("could not restore risk state: %s", e)

    # ------------------------------------------------------------------ orders
    @property
    def leverage(self) -> float:
        return max(1.0, float(self._leverage)) if self._leverage else margins()[0]

    def margin_for(self, symbol: str, qty: int, price: float) -> float:
        if self._leverage and not symbol.upper().endswith("-FUT"):
            return round(qty * price / self.leverage, 2)
        return margin_needed(symbol, qty, price)

    def _costs(self, symbol: str) -> CostModel:
        return self.costs.for_futures() if symbol.upper().endswith("-FUT") else self.costs

    def place(self, symbol: str, side: str, qty: int, price: float, stop_loss: float, target: float | None, tag: str,
              signal_id: int | None = None, strategy: str | None = None, ts: datetime | None = None,
              trail: float | None = None, max_minutes: int | None = None) -> dict:
        with self._lock:
            entry = self._slip(float(price), side, entering=True)
            avail = self.wallet.available()
            if symbol.upper().endswith("-FUT"):        # futures: whole lots only, margin must be available
                if self.margin_for(symbol, int(qty), entry) > avail:
                    raise InsufficientFunds(f"futures margin Rs {self.margin_for(symbol, int(qty), entry):,.0f} > available Rs {avail:,.0f}")
            else:
                max_qty = int(avail * self.leverage // entry)
                if max_qty <= 0:
                    raise InsufficientFunds(f"demo wallet has Rs {avail:,.0f} available")
                qty = min(int(qty), max_qty)
            if (side == "BUY" and entry <= stop_loss) or (side == "SELL" and entry >= stop_loss):
                raise ValueError(f"price {entry} already beyond the stop {stop_loss}")
            if target and ((side == "BUY" and entry >= target) or (side == "SELL" and entry <= target)):
                raise ValueError(f"price {entry} already past the target {target}")
            margin = self.margin_for(symbol, qty, entry)
            tid = db.insert_row(db.paper_trades, signal_id=signal_id, strategy=strategy, symbol=symbol, side=side, qty=qty,
                                margin=margin, ref_price=float(price), entry_price=entry, entry_ts=ts or datetime.now(),
                                stop_loss=float(stop_loss), target=float(target) if target else None, status="open",
                                last_price=float(price), tag=tag, init_stop=float(stop_loss),
                                trail=float(trail) if trail else None, max_minutes=int(max_minutes) if max_minutes else None,
                                best=entry)
        db.log("paper", "open", {"trade_id": tid, "symbol": symbol, "side": side, "qty": qty, "entry": entry})
        return {"order_id": f"PAPER-{tid}", "trade_id": tid, "symbol": symbol, "side": side, "qty": qty, "price": entry,
                "stop_loss": stop_loss, "target": target, "margin": margin, "status": "FILLED (paper)", "broker": "paper"}

    def close(self, trade_id: int, price: float, reason: str, ts: datetime | None = None, slip: bool = True) -> dict | None:
        with self._lock:
            rows = db.fetch(db.paper_trades, (db.paper_trades.c.id == trade_id) & (db.paper_trades.c.status == "open"))
            if not rows:
                return None
            t = rows[0]
            px = self._slip(price, t["side"], entering=False) if slip else round(price, 2)
            d = 1 if t["side"] == "BUY" else -1
            gross = round((px - t["entry_price"]) * t["qty"] * d, 2)
            buy_v = t["entry_price"] * t["qty"] if d == 1 else px * t["qty"]
            sell_v = px * t["qty"] if d == 1 else t["entry_price"] * t["qty"]
            cost = round(self._costs(t["symbol"]).round_trip(buy_v, sell_v), 2)
            net = round(gross - cost, 2)
            db.update_row(db.paper_trades, trade_id, status="closed", exit_price=px, exit_ts=ts or datetime.now(),
                          exit_reason=reason, gross_pnl=gross, costs=cost, net_pnl=net, last_price=price)
            bal = self.wallet.book(net, trade_id, f"{t['side']} {t['qty']} {t['symbol']} {reason}", ts=ts)
        if t["signal_id"]:
            db.update_row(db.signals, t["signal_id"], outcome_pnl=net, outcome=reason, status="closed")
        try:
            from yonexis.risk import risk
            risk.on_exit(t["symbol"], net)
        except Exception:
            pass
        db.log("paper", "close", {"trade_id": trade_id, "symbol": t["symbol"], "exit": px, "reason": reason, "net_pnl": net})
        return {"trade_id": trade_id, "symbol": t["symbol"], "exit": reason, "price": px, "net_pnl": net, "balance": bal}

    def manage(self, quotes: dict[str, dict], square_off: bool, ts: datetime | None = None) -> list[dict]:
        """quotes: {symbol: {"ltp": .., "high": .., "low": ..}} (high/low = range since the last update)."""
        out = []
        for t in self.open_trades():
            q = quotes.get(t["symbol"]) or {}
            ltp = q.get("ltp")
            if ltp is None:
                continue
            hi, lo = q.get("high", ltp), q.get("low", ltp)
            sl, tgt = t["stop_loss"], t["target"]
            r = None
            now = ts or datetime.now()
            if t.get("max_minutes") and t["entry_ts"] and now >= t["entry_ts"] + timedelta(minutes=int(t["max_minutes"])):
                r = self.close(t["id"], ltp, "time_exit", ts)
                out.append(r) if r else None
                continue
            trailed = bool(t.get("trail")) and t.get("init_stop") is not None and abs(sl - t["init_stop"]) > 1e-9
            if t["side"] == "BUY":
                if lo <= sl:
                    r = self.close(t["id"], min(sl, ltp), "trail_stop" if trailed else "stop_loss", ts)
                elif tgt is not None and hi >= tgt:
                    r = self.close(t["id"], tgt, "target", ts, slip=False)
            else:
                if hi >= sl:
                    r = self.close(t["id"], max(sl, ltp), "trail_stop" if trailed else "stop_loss", ts)
                elif tgt is not None and lo <= tgt:
                    r = self.close(t["id"], tgt, "target", ts, slip=False)
            if r is None and square_off:
                r = self.close(t["id"], ltp, "square_off", ts)
            if r is None:
                upd = {"last_price": float(ltp)}
                if t.get("trail"):                  # trailing stop follows the best price, never loosens
                    if t["side"] == "BUY":
                        best = max(float(t.get("best") or t["entry_price"]), hi)
                        upd.update(best=best, stop_loss=max(sl, round(best - float(t["trail"]), 2)))
                    else:
                        best = min(float(t.get("best") or t["entry_price"]), lo)
                        upd.update(best=best, stop_loss=min(sl, round(best + float(t["trail"]), 2)))
                db.update_row(db.paper_trades, t["id"], **upd)
            else:
                out.append(r)
        return out

    def square_off(self, symbol: str, price: float | None = None) -> list[dict]:
        out = []
        for t in self.open_trades():
            if t["symbol"] == symbol:
                out.append(self.close(t["id"], price or t["last_price"] or t["entry_price"], "manual"))
        return out

    def summary(self, quotes: dict[str, dict] | None = None) -> dict:
        w = self.wallet.get()
        opens = self.open_trades()
        mtm = 0.0
        for t in opens:
            ltp = ((quotes or {}).get(t["symbol"]) or {}).get("ltp") or t["last_price"] or t["entry_price"]
            t["ltp"] = ltp
            t["mtm"] = round((ltp - t["entry_price"]) * t["qty"] * (1 if t["side"] == "BUY" else -1), 2)
            mtm += t["mtm"]
        closed = db.fetch(db.paper_trades, db.paper_trades.c.status == "closed")
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        todays = [t for t in closed if t["exit_ts"] and t["exit_ts"] >= today]
        wins = [t for t in closed if (t["net_pnl"] or 0) > 0]
        return {
            "balance": round(w["balance"], 2), "starting_balance": round(w["starting_balance"], 2),
            "blocked_margin": self.wallet.blocked(), "available": self.wallet.available(),
            "open_mtm": round(mtm, 2), "equity": round(w["balance"] + mtm, 2),
            "total_pnl": round(w["balance"] - w["starting_balance"] + mtm, 2),
            "today_realized": round(sum(t["net_pnl"] or 0 for t in todays), 2), "today_trades": len(todays),
            "closed_trades": len(closed), "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else None,
            "total_costs": round(sum(t["costs"] or 0 for t in closed), 2),
            "open_positions": opens, "leverage": self.leverage, "fut_margin_pct": margins()[1], "slippage_bps": self.slippage_bps,
            "real_orders": "LOCKED" if not settings.live_orders_allowed else "ENABLED",
        }


def void_out_of_hours_trades() -> list[int]:
    """One-time correction: paper trades opened outside 09:15-15:30 (stale calls replayed at login) are voided.
    Open ones are closed at their entry price; closed ones get their P&L reversed in the wallet ledger."""
    from datetime import time as dtime
    w = Wallet()
    voided = []
    for t in db.fetch(db.paper_trades):
        et = t["entry_ts"]
        if not et or t["exit_reason"] == "void":
            continue
        if dtime(9, 15) <= et.time() < dtime(15, 30) and et.weekday() < 5:
            continue
        if t["status"] == "closed" and t["net_pnl"]:
            w.book(-t["net_pnl"], t["id"], f"void correction: {t['side']} {t['symbol']} opened {et:%H:%M} outside market hours")
        db.update_row(db.paper_trades, t["id"], status="closed", exit_reason="void", exit_price=t["entry_price"],
                      exit_ts=t["exit_ts"] or datetime.now(), gross_pnl=0.0, costs=0.0, net_pnl=0.0)
        if t["signal_id"]:
            db.update_row(db.signals, t["signal_id"], status="void: replayed old call", outcome_pnl=None, outcome=None)
        voided.append(t["id"])
    if voided:
        db.log("system", "paper_void", {"trade_ids": voided, "reason": "opened outside market hours (stale call)"})
    return voided


def void_trade(t: dict, reason: str, signal_status: str = "void") -> None:
    """Cancel a paper trade as if it never happened: reverse booked P&L, release the position."""
    if t["exit_reason"] == "void":
        return
    if t["status"] == "closed" and t["net_pnl"]:
        Wallet().book(-t["net_pnl"], t["id"], f"void correction: {t['side']} {t['symbol']} - {reason}"[:300])
    db.update_row(db.paper_trades, t["id"], status="closed", exit_reason="void", exit_price=t["entry_price"],
                  exit_ts=t["exit_ts"] or datetime.now(), gross_pnl=0.0, costs=0.0, net_pnl=0.0)
    if t["signal_id"]:
        db.update_row(db.signals, t["signal_id"], status=f"{signal_status}: {reason}"[:64], outcome_pnl=None, outcome=None)
    try:
        from yonexis.risk import risk
        risk.state.open_positions.pop(t["symbol"], None)
    except Exception:
        pass


def clear_history(amount: float | None = None, include_calls: bool = False) -> dict:
    """Paper Wallet 'Clear trade history': delete every paper trade (open ones too) and every ledger line, then start
    the demo wallet again at `amount`. With include_calls, the calls (signals) and their feedback go as well."""
    from sqlalchemy import delete

    n_trades = len(db.fetch(db.paper_trades))
    n_calls = len(db.fetch(db.signals)) if include_calls else 0
    amount = float(amount or settings.PAPER_STARTING_BALANCE)
    with db.engine.begin() as cx:
        cx.execute(delete(db.paper_trades))
        cx.execute(delete(db.wallet_ledger))
        if include_calls:
            cx.execute(delete(db.feedback))
            cx.execute(delete(db.signals))
    w = Wallet()
    with db.engine.begin() as cx:
        w._row(cx)
        cx.execute(update(db.wallet).where(db.wallet.c.id == 1).values(balance=amount, starting_balance=amount,
                                                                       updated_at=datetime.utcnow()))
        cx.execute(insert(db.wallet_ledger).values(ts=datetime.now(), kind="reset", amount=amount, balance_after=amount,
                                                   note="trade history cleared"))
    try:
        from yonexis.risk import risk
        risk.state.open_positions.clear()
        risk.state.realized_pnl = 0.0
    except Exception:
        pass
    db.log("admin", "paper_history_cleared", {"trades": n_trades, "calls": n_calls, "amount": amount})
    return {"removed_trades": n_trades, "removed_calls": n_calls, "balance": amount}
