"""Connector registry."""
from __future__ import annotations

from yonexis.config import settings

from .base import Connector
from .simulated import SimulatedConnector
from .yahoo import YahooConnector


def get_connector(name: str | None = None) -> Connector:
    name = (name or settings.LIVE_CONNECTOR).lower()
    if name == "simulated":
        return SimulatedConnector()
    if name == "yahoo":
        return YahooConnector()
    if name == "upstox":
        from .upstox import UpstoxConnector
        return UpstoxConnector()
    if name == "kite":
        from .kite import KiteConnector
        return KiteConnector(settings.KITE_API_KEY, settings.KITE_ACCESS_TOKEN)
    raise KeyError(f"unknown connector {name!r}")


AVAILABLE = {
    "simulated": "Synthetic NSE session candles for testing",
    "yahoo": "Yahoo Finance (free, delayed; 5m history ~60 days, daily for decades)",
    "upstox": "Upstox API (free live WebSocket feed, 1m candles since 2022, orders; daily login)",
    "kite": "Zerodha Kite Connect (live ticks, history, orders; needs API key + daily token)",
    "upload": "Admin upload: Excel / CSV / JSON / Parquet",
    "nse_bhavcopy": "NSE end-of-day bhavcopy with delivery %",
}

__all__ = ["AVAILABLE", "Connector", "get_connector"]
