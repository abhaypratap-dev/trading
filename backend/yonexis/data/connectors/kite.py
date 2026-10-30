"""Zerodha Kite Connect v3: historical candles, quotes and order placement.

Needs KITE_API_KEY and a daily KITE_ACCESS_TOKEN (Kite requires a login every
trading day). Orders must come from the static IP whitelisted on your Kite app
(SEBI retail algo framework).
Docs: https://kite.trade/docs/connect/v3/
"""
from __future__ import annotations

import io
from datetime import timedelta

import httpx
import pandas as pd

from .base import Connector

BASE = "https://api.kite.trade"
INTERVALS = {"1m": "minute", "3m": "3minute", "5m": "5minute", "15m": "15minute", "30m": "30minute",
             "1h": "60minute", "1d": "day"}
MAX_DAYS = {"minute": 60, "3minute": 100, "5minute": 100, "15minute": 200, "30minute": 200, "60minute": 400, "day": 2000}


class KiteConnector(Connector):
    name = "kite"
    live_capable = True
    trading_capable = True

    def __init__(self, api_key: str, access_token: str):
        if not api_key or not access_token:
            raise ValueError("KITE_API_KEY and KITE_ACCESS_TOKEN are required")
        self.h = {"X-Kite-Version": "3", "Authorization": f"token {api_key}:{access_token}"}
        self._tokens: dict[str, int] | None = None
        self.client = httpx.Client(base_url=BASE, headers=self.h, timeout=30)

    def _get(self, path: str, **params):
        r = self.client.get(path, params=params)
        r.raise_for_status()
        return r.json()["data"]

    def instrument_token(self, symbol: str) -> int:
        if self._tokens is None:
            r = self.client.get("/instruments/NSE")
            r.raise_for_status()
            inst = pd.read_csv(io.StringIO(r.text))
            inst = inst[inst["segment"] == "NSE"]
            self._tokens = dict(zip(inst["tradingsymbol"], inst["instrument_token"]))
        return int(self._tokens[symbol.upper()])

    def fetch_history(self, symbol: str, start: str, end: str, interval: str = "5m") -> pd.DataFrame:
        iv = INTERVALS[interval]
        tok = self.instrument_token(symbol)
        s, e = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(hours=23, minutes=59)
        frames = []
        while s <= e:
            chunk_end = min(s + timedelta(days=MAX_DAYS[iv] - 1), e)
            data = self._get(f"/instruments/historical/{tok}/{iv}", **{
                "from": s.strftime("%Y-%m-%d %H:%M:%S"), "to": chunk_end.strftime("%Y-%m-%d %H:%M:%S")})
            if data["candles"]:
                frames.append(pd.DataFrame(data["candles"], columns=["ts", "open", "high", "low", "close", "volume"]))
            s = chunk_end + timedelta(seconds=1)
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames)
        df["ts"] = pd.to_datetime(df["ts"]).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        return df.set_index("ts").sort_index()

    def quote(self, symbols: list[str]) -> dict:
        return self._get("/quote", i=[f"NSE:{s.upper()}" for s in symbols])

    # ---------------------------------------------------------------- orders
    def place_order(self, symbol: str, side: str, qty: int, order_type: str = "MARKET", price: float | None = None,
                    trigger_price: float | None = None, product: str = "MIS", tag: str = "yonexis") -> str:
        from yonexis.broker import assert_live_orders_allowed
        assert_live_orders_allowed()
        payload = {"tradingsymbol": symbol.upper(), "exchange": "NSE", "transaction_type": side, "quantity": qty,
                   "order_type": order_type, "product": product, "validity": "DAY", "tag": tag[:20]}
        if price is not None:
            payload["price"] = price
        if trigger_price is not None:
            payload["trigger_price"] = trigger_price
        r = self.client.post("/orders/regular", data=payload)
        r.raise_for_status()
        return r.json()["data"]["order_id"]

    def orders(self) -> list[dict]:
        return self._get("/orders")

    def positions(self) -> dict:
        return self._get("/portfolio/positions")

    def health(self) -> dict:
        try:
            prof = self._get("/user/profile")
            return {"connector": self.name, "ok": True, "user": prof.get("user_id")}
        except Exception as e:  # pragma: no cover - network
            return {"connector": self.name, "ok": False, "error": str(e)}
