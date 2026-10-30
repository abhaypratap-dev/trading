"""Strategy SDK.

Every strategy (imported, hand-written or AI-written) subclasses `Strategy` and
implements `on_candle`. The same class runs in backtest, paper and live mode.

A strategy only ever sees `ctx`, which exposes data up to and including the
current (closed) candle, so look-ahead is impossible by construction.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from typing import Any, ClassVar

import pandas as pd

BUY = "BUY"
SELL = "SELL"


@dataclass
class Signal:
    side: str                      # BUY or SELL
    stop_loss: float               # absolute price
    target: float | None = None    # absolute price, None = exit at square-off
    reason: str = ""
    tags: dict[str, Any] = field(default_factory=dict)  # extra info, e.g. zone levels
    # entry="market": fill at the next candle's open (decided on a closed candle).
    # entry="stop":   resting stop order at entry_price - fills the moment price touches it (BUY: at/above, SELL: at/below).
    # entry="limit":  resting limit order at entry_price - fills when price comes back to it (BUY: at/below, SELL: at/above).
    entry: str = "market"
    entry_price: float | None = None
    oco: str | None = None            # stop orders sharing an oco group cancel each other when one fills
    valid_until: str | None = None    # "HH:MM": an unfilled stop order expires after this time
    replace_pending: bool = False     # cancel this symbol's other resting orders before placing this one
    trail: float | None = None        # trailing stop distance in price units (stop follows the best price; never loosens)
    max_minutes: int | None = None    # time exit: close this many minutes after entry if neither stop nor target hit

    def __post_init__(self) -> None:
        if self.side not in (BUY, SELL):
            raise ValueError(f"side must be BUY or SELL, got {self.side!r}")
        if self.entry not in ("market", "stop", "limit"):
            raise ValueError("entry must be 'market', 'stop' or 'limit'")
        if self.entry in ("stop", "limit") and self.entry_price is None:
            raise ValueError(f"a {self.entry} entry needs entry_price")


@dataclass
class Candle:
    ts: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float


class IndRow:
    """Fast read-only view of one candle's indicator values: row["vwap"], row.get("rsi"). NaN when unavailable."""
    __slots__ = ("_a", "_i")

    def __init__(self, arrays: dict | None, i: int):
        self._a, self._i = arrays or {}, i

    def __getitem__(self, k: str) -> float:
        if self._i < 0 or k not in self._a:
            return float("nan")
        return float(self._a[k][self._i])

    def get(self, k: str, default=None):
        v = self[k]
        return default if v != v else v

    def __contains__(self, k: str) -> bool:
        return k in self._a


def as_arrays(df) -> dict:
    """Indicator DataFrame -> {column: float ndarray} for IndRow."""
    if df is None:
        return {}
    import numpy as np
    return {c: np.asarray(df[c], dtype=float) for c in df.columns}


class Context:
    """Read-only view a strategy gets on each candle."""

    def __init__(self, symbol: str, history: pd.DataFrame, i: int, state: dict, in_position: bool, has_pending: bool = False,
                 indicators: pd.DataFrame | None = None, extra: dict | None = None):
        self._ind = indicators
        self.extra = extra or {}          # e.g. {"pair": DataFrame of the paired symbol}
        self.has_pending = has_pending   # resting stop-entry orders exist
        self.symbol = symbol
        self._hist = history
        self._i = i
        self.state = state            # per-strategy, per-symbol scratch space (persists across candles)
        self.in_position = in_position

    @property
    def ind(self) -> "IndRow":
        """This candle's indicator values (from Strategy.indicators), e.g. ctx.ind["vwap"]."""
        return IndRow(self._ind, self._i)

    def ind_at(self, back: int) -> "IndRow":
        """Indicator values `back` candles ago (1 = previous candle)."""
        return IndRow(self._ind, self._i - back)

    @property
    def history(self) -> pd.DataFrame:
        """All candles up to and including the current one."""
        return self._hist.iloc[: self._i + 1]

    @property
    def today(self) -> pd.DataFrame:
        """Today's candles up to and including the current one."""
        h = self.history
        day = h.index[-1].normalize()
        return h[h.index >= day]

    @property
    def now(self) -> pd.Timestamp:
        return self._hist.index[self._i]

    def sma(self, col: str, n: int) -> float:
        return float(self.history[col].tail(n).mean())

    def atr(self, n: int = 14) -> float:
        h = self.history.tail(n + 1)
        prev_close = h["close"].shift(1)
        tr = pd.concat([h["high"] - h["low"], (h["high"] - prev_close).abs(), (h["low"] - prev_close).abs()], axis=1).max(axis=1)
        return float(tr.tail(n).mean())


class Strategy:
    """Base class. Override `params` and `on_candle`."""

    name: ClassVar[str] = "base"
    description: ClassVar[str] = ""
    params: ClassVar[dict[str, Any]] = {}

    session_start: ClassVar[time] = time(9, 15)
    square_off: ClassVar[time] = time(15, 15)
    chart_indicators: ClassVar[list[str]] = []       # what the chart draws for this strategy (sdk.indicators.CHART)
    family: ClassVar[str] = "custom"                 # reversal | momentum | breakout | scalping | ... (for the AI picker)

    def indicators(self, df: pd.DataFrame) -> pd.DataFrame | None:
        """Compute indicator columns ONCE for the whole candle history (must be causal). Read them in on_candle via
        ctx.ind. Default: none."""
        return None

    def __init__(self, **overrides: Any):
        unknown = set(overrides) - set(self.params)
        if unknown:
            raise ValueError(f"unknown params for {self.name}: {sorted(unknown)}")
        self.p = {**self.params, **overrides}

    def on_candle(self, ctx: Context, candle: Candle) -> "Signal | list[Signal] | None":  # pragma: no cover - abstract
        raise NotImplementedError

    def on_entry(self, ctx_state: dict, signal: Signal) -> None:
        """Called when one of this strategy's entries is filled (market or stop)."""

    def reversal(self, tags: dict, stopped_side: str, ts) -> "Signal | None":
        """Called the moment a position is stopped out. Return a Signal to reverse into the opposite side at once
        (stop-and-reverse / relay); None = just exit. `tags` are the stopped position's signal tags."""
        return None

    def on_new_day(self, ctx_state: dict) -> None:
        """Called before the first candle of each day. Default clears state."""
        ctx_state.clear()
