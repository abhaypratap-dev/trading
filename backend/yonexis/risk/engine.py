"""Risk engine: every order passes here. Nothing else may reach a broker."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dtime

from yonexis.config import settings


@dataclass
class RiskLimits:
    max_daily_loss: float = settings.MAX_DAILY_LOSS
    max_open_positions: int = settings.MAX_OPEN_POSITIONS
    capital_per_trade: float = settings.CAPITAL_PER_TRADE
    max_orders_per_sec: int = settings.MAX_ORDERS_PER_SEC
    no_entry_after: str = settings.NO_ENTRY_AFTER
    min_signal_score: float = settings.MIN_SIGNAL_SCORE
    max_capital_per_symbol: float = settings.CAPITAL_PER_TRADE * 1.5


@dataclass
class RiskState:
    kill_switch: bool = False
    kill_reason: str = ""
    day: str = ""
    realized_pnl: float = 0.0
    open_positions: dict = field(default_factory=dict)   # symbol -> {"qty":.., "value":.., "strategy":..}
    orders_ts: deque = field(default_factory=lambda: deque(maxlen=100))


class RiskEngine:
    def __init__(self, limits: RiskLimits | None = None):
        self.limits = limits or RiskLimits()
        self.state = RiskState()
        self._lock = threading.Lock()

    def _roll_day(self) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self.state.day != today:
            self.state.day, self.state.realized_pnl = today, 0.0
            self.state.open_positions.clear()

    def check_entry(self, symbol: str, side: str, price: float, score: float | None, strategy: str) -> tuple[bool, str, int]:
        """Returns (allowed, reason, qty)."""
        with self._lock:
            self._roll_day()
            L, S = self.limits, self.state
            if S.kill_switch:
                return False, f"kill switch on: {S.kill_reason}", 0
            if S.realized_pnl <= -abs(L.max_daily_loss):
                self.kill(f"daily loss limit {L.max_daily_loss} reached")
                return False, "daily loss limit reached", 0
            hh, mm = map(int, L.no_entry_after.split(":"))
            if datetime.now().time() > dtime(hh, mm) and not getattr(self, "ignore_clock", False):
                return False, f"no new entries after {L.no_entry_after}", 0
            if len(S.open_positions) >= L.max_open_positions:
                return False, f"max open positions ({L.max_open_positions})", 0
            if symbol in S.open_positions:
                return False, f"already holding {symbol}", 0
            if score is not None and score < L.min_signal_score:
                return False, f"score {score:.2f} below minimum {L.min_signal_score}", 0
            now = time.time()
            recent = [t for t in S.orders_ts if now - t < 1.0]
            if len(recent) >= L.max_orders_per_sec:
                return False, "order rate limit", 0
            if symbol.upper().endswith("-FUT"):
                from yonexis.config import settings
                try:
                    from yonexis.data.connectors.upstox import instruments
                    lot = instruments.lot_size(symbol)
                except Exception as e:
                    return False, f"futures lot size unknown: {e}", 0
                qty = max(1, settings.FUT_LOTS_PER_TRADE) * lot       # futures: fixed lots (default 1)
            else:
                qty = int(min(L.capital_per_trade, L.max_capital_per_symbol) // max(price, 0.01))
            if qty <= 0:
                return False, "price too high for capital per trade", 0
            S.orders_ts.append(now)
            S.open_positions[symbol] = {"qty": qty, "value": qty * price, "side": side, "strategy": strategy}
            return True, "ok", qty

    def on_exit(self, symbol: str, pnl: float) -> None:
        with self._lock:
            self.state.open_positions.pop(symbol, None)
            self.state.realized_pnl += pnl
            if self.state.realized_pnl <= -abs(self.limits.max_daily_loss):
                self.kill(f"daily loss limit {self.limits.max_daily_loss} reached")

    def kill(self, reason: str) -> None:
        self.state.kill_switch, self.state.kill_reason = True, reason

    def reset_kill(self) -> None:
        self.state.kill_switch, self.state.kill_reason = False, ""

    def snapshot(self) -> dict:
        s = self.state
        return {"kill_switch": s.kill_switch, "kill_reason": s.kill_reason, "day": s.day, "realized_pnl": round(s.realized_pnl, 2),
                "open_positions": s.open_positions, "limits": self.limits.__dict__}


EDITABLE = {   # key: (type, min, max, label)
    "max_open_positions": (int, 1, 100, "Max open positions"),
    "capital_per_trade": (float, 1000, 1e8, "Capital per trade (Rs)"),
    "max_capital_per_symbol": (float, 1000, 1e8, "Max capital per symbol (Rs)"),
    "max_daily_loss": (float, 100, 1e8, "Max loss per day (Rs) - kill switch"),
    "no_entry_after": (str, None, None, "No new entries after (HH:MM)"),
    "min_signal_score": (float, 0, 1, "Min AI score to trade (0 = off)"),
    "max_orders_per_sec": (int, 1, 10, "Max orders per second"),
}


def apply_limits(values: dict) -> dict:
    """Validate and apply admin edits to the live risk limits. Returns the cleaned values."""
    clean = {}
    for k, v in values.items():
        if k not in EDITABLE:
            raise ValueError(f"unknown setting {k}")
        typ, lo, hi, label = EDITABLE[k]
        if typ is str:
            hh, mm = str(v).split(":")
            v = f"{int(hh):02d}:{int(mm):02d}"
            if not ("09:15" <= v <= "15:30"):
                raise ValueError(f"{label} must be between 09:15 and 15:30")
        else:
            v = typ(v)
            if not (lo <= v <= hi):
                raise ValueError(f"{label} must be between {lo:g} and {hi:g}")
        clean[k] = v
    for k, v in clean.items():
        setattr(risk.limits, k, v)
    return clean


risk = RiskEngine()
