"""Connector interface. Every data source implements this so the engine never cares where data comes from."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator

import pandas as pd


class Connector(ABC):
    name: str = "base"
    live_capable: bool = False      # can it stream live prices
    trading_capable: bool = False   # can it place orders

    @abstractmethod
    def fetch_history(self, symbol: str, start: str, end: str, interval: str = "5m") -> pd.DataFrame:
        """Return a DataFrame indexed by timestamp (IST, naive) with open/high/low/close/volume."""

    def latest_candles(self, symbol: str, interval: str = "5m", lookback_days: int = 3) -> pd.DataFrame:
        end = pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None)
        start = end - pd.Timedelta(days=lookback_days + 3)
        return self.fetch_history(symbol, str(start.date()), str(end.date()), interval)

    def stream_live(self, symbols: list[str]) -> Iterator[dict]:  # pragma: no cover - optional
        raise NotImplementedError(f"{self.name} does not stream live ticks")

    def health(self) -> dict:
        return {"connector": self.name, "ok": True}
