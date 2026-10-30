"""Yahoo Finance via yfinance. Free, good for research and backfill, NOT for live trading.

Limits (Yahoo's, not ours): 5m/15m data only for the last ~60 days, 1m for ~7 days,
daily data for decades. Prices can be delayed.
"""
from __future__ import annotations

import pandas as pd

from .base import Connector

INTERVALS = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m", "1d": "1d"}


def to_yahoo(symbol: str) -> str:
    s = symbol.upper()
    if s in ("NIFTY", "NIFTY50", "^NSEI"):
        return "^NSEI"
    if s in ("BANKNIFTY", "^NSEBANK"):
        return "^NSEBANK"
    if s in ("INDIAVIX", "^INDIAVIX"):
        return "^INDIAVIX"
    return s if s.endswith(".NS") or s.startswith("^") else f"{s}.NS"


class YahooConnector(Connector):
    name = "yahoo"
    live_capable = True   # delayed polling only

    def fetch_history(self, symbol: str, start: str, end: str, interval: str = "5m") -> pd.DataFrame:
        import yfinance as yf

        iv = INTERVALS.get(interval, interval)
        end_excl = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        df = yf.download(to_yahoo(symbol), start=start, end=end_excl, interval=iv, progress=False,
                         auto_adjust=False, threads=False)
        if df is None or df.empty:
            return pd.DataFrame()
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        idx = pd.to_datetime(df.index)
        if idx.tz is not None:
            idx = idx.tz_convert("Asia/Kolkata").tz_localize(None)
        df.index = idx
        df.index.name = "ts"
        return df.dropna()

    def health(self) -> dict:
        try:
            ok = not self.fetch_history("RELIANCE", str((pd.Timestamp.now() - pd.Timedelta(days=7)).date()),
                                        str(pd.Timestamp.now().date()), "1d").empty
            return {"connector": self.name, "ok": ok}
        except Exception as e:  # pragma: no cover - network
            return {"connector": self.name, "ok": False, "error": str(e)}
