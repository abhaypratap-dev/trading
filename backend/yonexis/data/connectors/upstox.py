"""Upstox API v2/v3: login, instruments, historical + intraday candles, live WebSocket feed, quotes, orders.

Docs: https://upstox.com/developer/api-documentation/
  * Login:   /v2/login/authorization/dialog  ->  redirect ?code=  ->  POST /v2/login/authorization/token
             Access tokens are valid for one trading day; the admin logs in once each morning.
  * Candles: /v3/historical-candle/{key}/{unit}/{interval}/{to}/{from}   (1-15 minute data: max 1 month per call, from Jan 2022)
             /v3/historical-candle/intraday/{key}/{unit}/{interval}        (today)
  * Feed:    wss://api.upstox.com/v3/feed/market-data-feed  (protobuf, binary subscribe messages)
  * Orders:  POST https://api-hft.upstox.com/v3/order/place
"""
from __future__ import annotations

import gzip
import io
import json
import logging
import secrets
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote, urlencode

import httpx
import pandas as pd

from yonexis.config import settings

from .base import Connector

log = logging.getLogger("yonexis.upstox")

API = "https://api.upstox.com"
HFT = "https://api-hft.upstox.com"
WS_URL = "wss://api.upstox.com/v3/feed/market-data-feed"
INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
IST_OFFSET = pd.Timedelta(hours=5, minutes=30)

INDEX_KEYS = {"NIFTY": "NSE_INDEX|Nifty 50", "NIFTY50": "NSE_INDEX|Nifty 50", "BANKNIFTY": "NSE_INDEX|Nifty Bank",
              "INDIAVIX": "NSE_INDEX|India VIX", "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
              "MIDCPNIFTY": "NSE_INDEX|NIFTY MID SELECT"}


class UpstoxError(RuntimeError):
    pass


# ============================================================================ token store

class TokenStore:
    """Persists the daily access token (and the pending OAuth state) in the data volume."""

    def __init__(self, path: Path | None = None):
        self.path = path or settings.DATA_DIR / "upstox_token.json"
        self._state: str | None = None

    def load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except Exception:
            return {}

    def save(self, token: str, extra: dict | None = None) -> None:
        self.path.write_text(json.dumps({"access_token": token, "saved_at": datetime.now().isoformat(timespec="seconds"),
                                         "trading_day": str(_trading_day()), **(extra or {})}))
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)

    @property
    def token(self) -> str | None:
        d = self.load()
        # Upstox tokens die overnight: a token saved on an earlier trading day is treated as expired.
        if d.get("access_token") and d.get("trading_day") == str(_trading_day()):
            return d["access_token"]
        return None

    def new_state(self) -> str:
        self._state = secrets.token_urlsafe(16)
        return self._state

    def check_state(self, state: str | None) -> bool:
        ok = bool(self._state) and secrets.compare_digest(self._state, state or "")
        self._state = None
        return ok


def _trading_day() -> date:
    """Tokens expire early morning IST; anything before 03:30 belongs to the previous day."""
    now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    return (now - timedelta(hours=3, minutes=30)).date()


tokens = TokenStore()


def login_url(state: str) -> str:
    if not settings.UPSTOX_API_KEY:
        raise UpstoxError("UPSTOX_API_KEY is not set in .env")
    q = {"response_type": "code", "client_id": settings.UPSTOX_API_KEY, "redirect_uri": settings.UPSTOX_REDIRECT_URI, "state": state}
    return f"{API}/v2/login/authorization/dialog?{urlencode(q)}"


def exchange_code(code: str) -> dict:
    r = httpx.post(f"{API}/v2/login/authorization/token", timeout=30,
                   headers={"accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
                   data={"code": code, "client_id": settings.UPSTOX_API_KEY, "client_secret": settings.UPSTOX_API_SECRET,
                         "redirect_uri": settings.UPSTOX_REDIRECT_URI, "grant_type": "authorization_code"})
    body = _json(r)
    tok = body.get("access_token")
    if not tok:
        raise UpstoxError(f"token exchange failed: {body}")
    tokens.save(tok, {"user_id": body.get("user_id"), "user_name": body.get("user_name")})
    return {"user_id": body.get("user_id"), "user_name": body.get("user_name")}


def _json(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except Exception:
        raise UpstoxError(f"HTTP {r.status_code}: {r.text[:300]}")
    if r.status_code >= 400 or body.get("status") == "error":
        errs = body.get("errors") or body
        if r.status_code == 401:
            raise UpstoxError(f"Upstox session expired or invalid - log in again from the Data tab ({errs})")
        raise UpstoxError(f"Upstox HTTP {r.status_code}: {errs}")
    return body


# ============================================================================ instruments

FUT_SUFFIX = "-FUT"
INDEX_FUTURES = ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY"]


def is_future(symbol: str) -> bool:
    return str(symbol).upper().endswith(FUT_SUFFIX)


def _fut_underlying(x: dict) -> str:
    u = x.get("underlying_symbol") or x.get("asset_symbol") or ""
    if not u:
        u = str(x.get("trading_symbol", "")).upper().split(" FUT")[0].strip()
    return str(u).upper()


def _expiry_date(v) -> str | None:
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
            return str((pd.Timestamp(int(v), unit="ms", tz="UTC") + IST_OFFSET).date())
        return str(pd.Timestamp(v).date())
    except Exception:
        return None


class Instruments:
    """NSE trading symbol <-> Upstox instrument_key, from the daily instrument master file.

    Equities are plain symbols (RELIANCE). Futures are "<UNDERLYING>-FUT" (NIFTY-FUT, RELIANCE-FUT): the name always
    resolves to the NEAR-MONTH contract (nearest expiry on or after today), so it rolls to the next month by itself.
    """

    def __init__(self, cache: Path | None = None):
        self.cache = cache or settings.DATA_DIR / "upstox_nse_instruments_v3.json"
        self._by_symbol: dict[str, dict] = {}
        self._by_key: dict[str, str] = {}
        self._futs: dict[str, list[dict]] = {}
        self._loaded_for: date | None = None
        self._lock = threading.Lock()

    def _load(self) -> None:
        with self._lock:
            if self._loaded_for == date.today() and self._by_symbol:
                return
            rows = None
            if self.cache.exists() and datetime.fromtimestamp(self.cache.stat().st_mtime).date() == date.today():
                rows = json.loads(self.cache.read_text())
            if rows is None:
                r = httpx.get(INSTRUMENTS_URL, timeout=120, follow_redirects=True)
                r.raise_for_status()
                raw = json.loads(gzip.GzipFile(fileobj=io.BytesIO(r.content)).read())
                rows = [{"s": x["trading_symbol"], "k": x["instrument_key"], "n": x.get("name"), "t": x.get("tick_size"),
                         "l": x.get("lot_size", 1)}
                        for x in raw if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"]
                rows += [{"s": _fut_underlying(x) + FUT_SUFFIX, "k": x["instrument_key"], "n": x.get("trading_symbol"),
                          "t": x.get("tick_size"), "l": int(x.get("lot_size") or 1), "e": _expiry_date(x.get("expiry")), "f": 1,
                          "u": x.get("underlying_key")}
                         for x in raw if x.get("segment") == "NSE_FO" and x.get("instrument_type") == "FUT"]
                self.cache.write_text(json.dumps(rows))
            eq = [r for r in rows if not r.get("f")]
            self._by_symbol = {r["s"].upper(): r for r in eq}
            self._by_key = {r["k"]: r["s"].upper() for r in eq}
            futs: dict[str, list[dict]] = {}
            for r in rows:
                if r.get("f") and r.get("e"):
                    futs.setdefault(r["s"].upper(), []).append(r)
            today = str(date.today())
            self._futs = {s: sorted([r for r in v if r["e"] >= today], key=lambda r: r["e"]) for s, v in futs.items()}
            self._futs = {s: v for s, v in self._futs.items() if v}
            for s, v in self._futs.items():
                for r in v:
                    self._by_key[r["k"]] = s          # every month maps back to the continuous name
            self._by_key.update({v: k for k, v in INDEX_KEYS.items()})
            self._loaded_for = date.today()

    def contract(self, symbol: str) -> dict:
        """Near-month futures contract: {symbol, trading_symbol, instrument_key, expiry, lot_size}."""
        self._load()
        s = symbol.upper()
        if s not in self._futs:
            raise UpstoxError(f"{symbol}: no NSE futures contract for {s[:-len(FUT_SUFFIX)]} in the Upstox instrument list")
        r = self._futs[s][0]
        return {"symbol": s, "trading_symbol": r["n"], "instrument_key": r["k"], "expiry": r["e"], "lot_size": int(r["l"] or 1)}

    def underlying_key(self, symbol: str) -> str:
        """Instrument key of a future's underlying (NSE_INDEX|Nifty 50, NSE_EQ|INE...): needed for expired contracts."""
        self._load()
        s = symbol.upper()
        v = self._futs.get(s) or []
        u = next((r.get("u") for r in v if r.get("u")), None)
        if u:
            return u
        base = s[:-len(FUT_SUFFIX)] if s.endswith(FUT_SUFFIX) else s
        if base in INDEX_KEYS:
            return INDEX_KEYS[base]
        if base in self._by_symbol:
            return self._by_symbol[base]["k"]
        raise UpstoxError(f"{symbol}: underlying instrument not found")

    def lot_size(self, symbol: str) -> int:
        return self.contract(symbol)["lot_size"] if is_future(symbol) else 1

    def all_futures(self) -> list[dict]:
        """Every NSE future with a live contract (near month), A-Z: index futures first."""
        self._load()
        out = [{"symbol": s, "lot_size": int(v[0]["l"] or 1), "expiry": v[0]["e"], "trading_symbol": v[0]["n"]}
               for s, v in self._futs.items()]
        idx = {f + FUT_SUFFIX for f in INDEX_FUTURES}
        return sorted(out, key=lambda r: (r["symbol"] not in idx, r["symbol"]))

    def futures_for(self, underlyings: list[str]) -> list[str]:
        self._load()
        return [u.upper() + FUT_SUFFIX for u in underlyings if (u.upper() + FUT_SUFFIX) in self._futs]

    def key(self, symbol: str) -> str:
        s = symbol.upper().replace(".NS", "")
        if is_future(s):
            return self.contract(s)["instrument_key"]
        if s in INDEX_KEYS:
            return INDEX_KEYS[s]
        if "|" in s:
            return symbol
        self._load()
        if s not in self._by_symbol:
            raise UpstoxError(f"{symbol} is not an NSE equity symbol in the Upstox instrument list")
        return self._by_symbol[s]["k"]

    def symbol(self, key: str) -> str:
        if key in self._by_key:
            return self._by_key[key]
        self._load()
        return self._by_key.get(key, key)

    def search(self, q: str, limit: int = 20) -> list[dict]:
        self._load()
        q = q.upper()
        fut = [{"symbol": s, "name": f"{v[0]['n']} · lot {v[0]['l']} · expiry {v[0]['e']}", "instrument_key": v[0]["k"], "future": True}
               for s, v in self._futs.items() if s.startswith(q) or q.endswith("FUT") and s.startswith(q.replace(" FUT", "").replace("FUT", ""))]
        out = [r for s, r in self._by_symbol.items() if s.startswith(q)]
        out += [r for s, r in self._by_symbol.items() if q in (r.get("n") or "").upper() and r not in out]
        eq = [{"symbol": r["s"], "name": r["n"], "instrument_key": r["k"]} for r in out]
        exact = [f for f in fut if f["symbol"] == q + FUT_SUFFIX]
        rest = [f for f in fut if f not in exact]
        return (eq[:1] + exact + eq[1:] + rest)[:limit] if eq else (exact + rest)[:limit]


instruments = Instruments()


# ============================================================================ REST connector

def _parse_candles(candles: list) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.DataFrame(candles, columns=["ts", "open", "high", "low", "close", "volume", "oi"][: len(candles[0])])
    ts = pd.to_datetime(df["ts"], utc=True) + IST_OFFSET
    df["ts"] = ts.dt.tz_localize(None)
    return df.set_index("ts").sort_index()[["open", "high", "low", "close", "volume"]].astype(float)


def _unit_interval(interval: str) -> tuple[str, int]:
    if interval.endswith("m"):
        return "minutes", int(interval[:-1])
    if interval.endswith("h"):
        return "hours", int(interval[:-1])
    if interval in ("1d", "d", "day"):
        return "days", 1
    if interval in ("1w", "w"):
        return "weeks", 1
    raise ValueError(f"unsupported interval {interval}")


class UpstoxConnector(Connector):
    name = "upstox"
    live_capable = True
    trading_capable = True

    def __init__(self, token: str | None = None):
        self._token = token
        self.client = httpx.Client(timeout=30, headers={"Accept": "application/json"})
        self._last_call = 0.0

    @property
    def token(self) -> str:
        t = self._token or tokens.token
        if not t:
            raise UpstoxError("not logged in to Upstox today - click 'Connect Upstox' on the Data tab")
        return t

    def _get(self, url: str, **params) -> dict:
        wait = 0.12 - (time.time() - self._last_call)       # stay well under Upstox rate limits
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()
        for attempt in range(3):
            r = self.client.get(url, params=params or None, headers={"Authorization": f"Bearer {self.token}"})
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1))
                continue
            return _json(r)
        raise UpstoxError("Upstox rate limit (429) - try again in a minute")

    # ------------------------------------------------------------------ expired contracts (Upstox Plus)
    EXPIRED_IV = {"1m": "1minute", "3m": "3minute", "5m": "5minute", "15m": "15minute", "30m": "30minute", "1d": "day"}

    def expiries(self, underlying_key: str) -> list[str]:
        body = self._get(f"{API}/v2/expired-instruments/expiries", instrument_key=underlying_key)
        return sorted(str(x)[:10] for x in (body.get("data") or []))

    def expired_futures(self, underlying_key: str, expiry: str) -> list[dict]:
        body = self._get(f"{API}/v2/expired-instruments/future/contract", instrument_key=underlying_key, expiry_date=expiry)
        return list(body.get("data") or [])

    def expired_candles(self, expired_key: str, start: str, end: str, interval: str = "5m") -> pd.DataFrame:
        iv = self.EXPIRED_IV.get(interval)
        if not iv:
            raise UpstoxError(f"interval {interval} is not offered for expired contracts")
        s, e = pd.Timestamp(start).date(), pd.Timestamp(end).date()
        step = timedelta(days=27) if iv in ("1minute", "3minute", "5minute", "15minute") else timedelta(days=360)
        key = quote(expired_key, safe="")
        frames, cur_end = [], e
        while cur_end >= s:
            cur_start = max(s, cur_end - step)
            body = self._get(f"{API}/v2/expired-instruments/historical-candle/{key}/{iv}/{cur_end}/{cur_start}")
            frames.append(_parse_candles(body.get("data", {}).get("candles", [])))
            cur_end = cur_start - timedelta(days=1)
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames)
        return df[~df.index.duplicated(keep="last")].sort_index()

    # ------------------------------------------------------------------ candles
    def fetch_history(self, symbol: str, start: str, end: str, interval: str = "5m") -> pd.DataFrame:
        """Candles for a symbol. A future (NIFTY-FUT) means the near month at every date: past dates come from the
        contract that was the near month then (expired contracts, Upstox Plus), never from today's contract, whose
        early life as a far-month contract is full of flat no-trade candles."""
        if is_future(symbol):
            try:
                from yonexis import futcont
                return futcont.fetch_range(self, symbol, start, end, interval)
            except Exception as e:                       # no Upstox Plus / lookup failed: current contract only
                log.info("continuous futures fetch for %s failed (%s): using the current contract", symbol, e)
        return self._fetch_history_raw(symbol, start, end, interval)

    def _fetch_history_raw(self, symbol: str, start: str, end: str, interval: str = "5m") -> pd.DataFrame:
        key = quote(instruments.key(symbol), safe="")
        unit, n = _unit_interval(interval)
        s, e = pd.Timestamp(start).date(), pd.Timestamp(end).date()
        if unit == "minutes":
            s = max(s, date(2022, 1, 1))
            step = timedelta(days=28) if n <= 15 else timedelta(days=88)
        elif unit == "hours":
            s, step = max(s, date(2022, 1, 1)), timedelta(days=88)
        elif unit == "days":
            step = timedelta(days=3600)
        else:
            step = timedelta(days=365 * 30)
        frames = []
        cur_end = e
        while cur_end >= s:
            cur_start = max(s, cur_end - step)
            body = self._get(f"{API}/v3/historical-candle/{key}/{unit}/{n}/{cur_end}/{cur_start}")
            frames.append(_parse_candles(body.get("data", {}).get("candles", [])))
            cur_end = cur_start - timedelta(days=1)
        today = (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()
        if e >= today and unit in ("minutes", "hours"):
            frames.append(self.intraday(symbol, interval))
        frames = [f for f in frames if not f.empty]
        if not frames:
            return pd.DataFrame()
        df = pd.concat(frames)
        df = df[~df.index.duplicated(keep="last")].sort_index()
        df.index.name = "ts"
        return df

    def intraday(self, symbol: str, interval: str = "1m") -> pd.DataFrame:
        key = quote(instruments.key(symbol), safe="")
        unit, n = _unit_interval(interval)
        body = self._get(f"{API}/v3/historical-candle/intraday/{key}/{unit}/{n}")
        return _parse_candles(body.get("data", {}).get("candles", []))

    def latest_candles(self, symbol: str, interval: str = "5m", lookback_days: int = 15) -> pd.DataFrame:
        end = (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()
        return self.fetch_history(symbol, str(end - timedelta(days=lookback_days + 3)), str(end), interval)

    # ------------------------------------------------------------------ quotes / account
    def ltp(self, symbols: list[str]) -> dict[str, dict]:
        keys = {instruments.key(s): s.upper() for s in symbols}
        out: dict[str, dict] = {}
        items = list(keys)
        for k in range(0, len(items), 500):
            body = self._get(f"{API}/v3/market-quote/ltp", instrument_key=",".join(items[k:k + 500]))
            for v in (body.get("data") or {}).values():
                sym = keys.get(v.get("instrument_token")) or instruments.symbol(v.get("instrument_token", ""))
                out[sym] = {"ltp": v.get("last_price"), "cp": v.get("cp"), "volume": v.get("volume")}
        return out

    def profile(self) -> dict:
        return self._get(f"{API}/v2/user/profile").get("data", {})

    def funds(self) -> dict:
        return self._get(f"{API}/v2/user/get-funds-and-margin", segment="SEC").get("data", {})

    def positions(self) -> list:
        return self._get(f"{API}/v2/portfolio/short-term-positions").get("data", [])

    def orders(self) -> list:
        return self._get(f"{API}/v2/order/retrieve-all").get("data", [])

    def health(self) -> dict:
        try:
            p = self.profile()
            return {"connector": self.name, "ok": True, "user": p.get("user_id"), "name": p.get("user_name")}
        except Exception as e:
            return {"connector": self.name, "ok": False, "error": str(e)}

    # ------------------------------------------------------------------ orders
    def place_order(self, symbol: str, side: str, qty: int, order_type: str = "MARKET", price: float = 0.0,
                    trigger_price: float = 0.0, product: str = "I", tag: str = "yonexis") -> list[str]:
        from yonexis.broker import assert_live_orders_allowed
        assert_live_orders_allowed()
        body = {"quantity": int(qty), "product": product, "validity": "DAY", "price": float(price or 0), "tag": tag[:40],
                "instrument_token": instruments.key(symbol), "order_type": order_type, "transaction_type": side,
                "disclosed_quantity": 0, "trigger_price": float(trigger_price or 0), "is_amo": False, "slice": True}
        if order_type in ("MARKET", "SL-M"):
            body["market_protection"] = -1       # exchange rejects unprotected API market orders
        r = self.client.post(f"{HFT}/v3/order/place", json=body,
                             headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        return _json(r).get("data", {}).get("order_ids", [])

    def cancel_order(self, order_id: str) -> dict:
        from yonexis.broker import assert_live_orders_allowed
        assert_live_orders_allowed()
        r = self.client.delete(f"{HFT}/v3/order/cancel", params={"order_id": order_id},
                               headers={"Authorization": f"Bearer {self.token}"})
        return _json(r)


# ============================================================================ live feed -> candles

def _bucket(ts: pd.Timestamp, minutes: int) -> pd.Timestamp:
    """Candle start for NSE sessions (buckets aligned to 09:15)."""
    open_ = ts.normalize() + pd.Timedelta(hours=9, minutes=15)
    k = int((ts - open_).total_seconds() // (minutes * 60))
    return open_ + pd.Timedelta(minutes=k * minutes)


def _resample(df_1m: pd.DataFrame, minutes: int) -> pd.DataFrame:
    if minutes <= 1 or df_1m.empty:
        return df_1m
    d = df_1m.copy()
    d["b"] = [_bucket(t, minutes) for t in d.index]
    out = d.groupby("b").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                             close=("close", "last"), volume=("volume", "sum"))
    out.index.name = "ts"
    return out


class CandleBuilder:
    """Builds 1-minute candles from ticks (ltp, ltt, cumulative day volume) and resamples to any N-minute bar."""

    def __init__(self):
        self.bars: dict[str, dict[pd.Timestamp, list[float]]] = {}   # sym -> {minute_start: [o,h,l,c,v]}
        self.last_vtt: dict[str, float] = {}
        self.last: dict[str, dict] = {}
        self.src: dict[str, dict[pd.Timestamp, str]] = {}            # sym -> {minute: tick | exchange | rest}
        self.corrections = 0                                          # minutes whose tick-built OHLC was wrong
        self._lock = threading.Lock()

    def on_ohlc(self, sym: str, ts_ms: int, o: float, h: float, lo: float, c: float, vol: float | None) -> None:
        """Exchange 1-minute OHLC from the full feed (marketOHLC 'I1'): exact high/low even between ticks."""
        m = pd.Timestamp(int(ts_ms), unit="ms") + IST_OFFSET
        m = m.floor("min")
        t = m.time()
        if not (datetime.strptime("09:15", "%H:%M").time() <= t < datetime.strptime("15:30", "%H:%M").time()):
            return
        now_min = _bucket(pd.Timestamp.utcnow().tz_localize(None) + IST_OFFSET, 1)
        with self._lock:
            d = self.bars.setdefault(sym, {})
            src = self.src.setdefault(sym, {})
            if src.get(m) == "rest":
                return
            b = d.get(m)
            v = float(vol) if vol is not None else (b[4] if b else 0.0)
            if m < now_min or b is None:          # completed minute: the exchange's numbers win
                if b is not None and m < now_min and (abs(b[1] - h) > 1e-9 or abs(b[2] - lo) > 1e-9):
                    self.corrections += 1
                d[m] = [o, h, lo, c, v]
            else:                                 # forming minute: widen with exchange extremes, keep live close
                b[0], b[1], b[2], b[4] = o, max(b[1], h), min(b[2], lo), max(b[4], v)
            src[m] = "exchange"

    def on_tick(self, sym: str, ltp: float, ltt_ms: int | None, vtt: float | None = None, cp: float | None = None) -> None:
        if ltp is None:
            return
        ts = (pd.Timestamp(int(ltt_ms), unit="ms") + IST_OFFSET) if ltt_ms else pd.Timestamp.utcnow().tz_localize(None) + IST_OFFSET
        t = ts.time()
        with self._lock:
            self.last[sym] = {"ltp": float(ltp), "cp": cp, "ts": str(ts.floor("s")), "vtt": vtt}
            if not (datetime.strptime("09:15", "%H:%M").time() <= t < datetime.strptime("15:30", "%H:%M").time()):
                return
            m = _bucket(ts, 1)
            vol = 0.0
            if vtt is not None:
                prev = self.last_vtt.get(sym)
                vol = max(0.0, float(vtt) - prev) if prev is not None and vtt >= prev else 0.0
                self.last_vtt[sym] = float(vtt)
            b = self.bars.setdefault(sym, {}).get(m)
            if self.src.get(sym, {}).get(m) == "rest":
                return
            if b is None:
                self.bars[sym][m] = [ltp, ltp, ltp, ltp, vol]
                self.src.setdefault(sym, {})[m] = "tick"
            else:
                b[1], b[2], b[3], b[4] = max(b[1], ltp), min(b[2], ltp), ltp, b[4] + vol

    def merge_rest(self, sym: str, df_1m: pd.DataFrame) -> None:
        """REST intraday 1m candles are authoritative for completed minutes (fixes gaps after reconnects).

        Bars from earlier sessions are kept here until UpstoxFeed rolls them into its history, so data never
        disappears at midnight or when the daily login expires.
        """
        if df_1m.empty:
            return
        with self._lock:
            d = self.bars.setdefault(sym, {})
            src = self.src.setdefault(sym, {})
            now_min = _bucket(pd.Timestamp.utcnow().tz_localize(None) + IST_OFFSET, 1)
            for ts, r in df_1m.iterrows():
                if ts < now_min:
                    b = d.get(ts)
                    if b is not None and src.get(ts) != "rest" and (abs(b[1] - r.high) > 1e-9 or abs(b[2] - r.low) > 1e-9
                                                                    or abs(b[0] - r.open) > 1e-9 or abs(b[3] - r.close) > 1e-9):
                        self.corrections += 1
                    d[ts] = [r.open, r.high, r.low, r.close, r.volume]
                    src[ts] = "rest"

    def confirmed_upto(self, sym: str) -> pd.Timestamp | None:
        """Last minute whose candle came from Upstox's official REST candles."""
        with self._lock:
            ks = [k for k, v in self.src.get(sym, {}).items() if v == "rest"]
        return max(ks) if ks else None

    def pop_before(self, sym: str, day: pd.Timestamp) -> pd.DataFrame:
        """Remove and return 1m bars from sessions before `day`."""
        with self._lock:
            d = self.bars.get(sym, {})
            old = {k: d.pop(k) for k in [k for k in d if k < day]}
            sd = self.src.get(sym, {})
            for k in list(sd):
                if k < day:
                    sd.pop(k, None)
        if not old:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame.from_dict(old, orient="index", columns=["open", "high", "low", "close", "volume"]).sort_index()
        df.index.name = "ts"
        return df

    def candles(self, sym: str, minutes: int, closed_only: bool = True) -> pd.DataFrame:
        with self._lock:
            d = dict(self.bars.get(sym, {}))
        if not d:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        df = pd.DataFrame.from_dict(d, orient="index", columns=["open", "high", "low", "close", "volume"]).sort_index()
        df.index.name = "ts"
        df = _resample(df, minutes)
        if closed_only:
            now = pd.Timestamp.utcnow().tz_localize(None) + IST_OFFSET
            df = df[df.index + pd.Timedelta(minutes=minutes) <= now]
        return df


class UpstoxFeed:
    """Live market feed over Upstox WebSocket v3 with auto-reconnect, feeding a CandleBuilder.

    Uses the protobuf schema from upstox-python-sdk (vendored in upstox_proto/), with normal TLS verification.
    """

    def __init__(self, connector: UpstoxConnector, symbols: list[str], mode: str = "full"):
        self.conn = connector
        self.symbols = [s.upper() for s in symbols]
        self.mode = mode
        self.builder = CandleBuilder()
        self.status = "stopped"
        self.error: str | None = None
        self.ticks = 0
        self.last_msg_at: float | None = None
        self._ws = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.history: dict[str, pd.DataFrame] = {}   # previous days, for strategy context
        self._keymap: dict[str, str] = {}

    # -------------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="upstox-feed", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self.status = "stopped"

    def _run(self) -> None:
        import websocket   # websocket-client
        from yonexis.data.connectors.upstox_proto import MarketDataFeedV3_pb2 as pb
        from google.protobuf import json_format

        keys = {}
        for s in list(self.symbols):
            try:
                keys[instruments.key(s)] = s
            except Exception as e:
                log.warning("skipping %s: %s", s, e)
                self.error = f"{s}: {e}"
        self._keymap = keys
        backoff = 2
        while not self._stop.is_set():
            try:
                self.status = "connecting"
                tok = self.conn.token
            except Exception as e:
                self.status, self.error = "waiting_for_login", str(e)
                self._stop.wait(15)
                continue

            def on_open(ws):
                nonlocal backoff
                backoff = 2
                self.status, self.error = "connected", None
                self._send("sub", list(self._keymap))
                log.info("upstox feed subscribed to %d instruments (%s)", len(self._keymap), self.mode)

            def on_message(ws, message):
                try:
                    msg = json_format.MessageToDict(pb.FeedResponse.FromString(message))
                    self._handle(msg)
                except Exception as e:  # never kill the socket on one bad frame
                    self.error = f"decode: {e}"

            def on_error(ws, error):
                self.error = str(error)

            self._ws = websocket.WebSocketApp(WS_URL, header={"Authorization": f"Bearer {tok}", "Accept": "*/*"},
                                              on_open=on_open, on_message=on_message, on_error=on_error)
            self._ws.run_forever(ping_interval=20, ping_timeout=10)
            if self._stop.is_set():
                break
            self.status = "reconnecting"
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 60)
        self.status = "stopped"

    def _handle(self, msg: dict) -> None:
        self.last_msg_at = time.time()
        for key, feed in (msg.get("feeds") or {}).items():
            sym = self._keymap.get(key) or instruments.symbol(key)
            ff = (feed.get("fullFeed") or {})
            mff = ff.get("marketFF") or ff.get("indexFF") or {}
            ltpc = mff.get("ltpc") or feed.get("ltpc") or {}
            if "ltp" not in ltpc:
                continue
            self.ticks += 1
            for ob in (mff.get("marketOHLC") or {}).get("ohlc") or []:
                if ob.get("interval") == "I1" and ob.get("ts"):
                    try:
                        self.builder.on_ohlc(sym, int(ob["ts"]), float(ob["open"]), float(ob["high"]), float(ob["low"]),
                                             float(ob["close"]), float(ob["vol"]) if ob.get("vol") is not None else None)
                    except (KeyError, TypeError, ValueError):
                        pass
            vtt = mff.get("vtt")
            if mff.get("marketLevel") or mff.get("tbq") is not None:
                try:
                    from yonexis.data.orderflow import flow
                    lt = int(ltpc["ltt"]) if ltpc.get("ltt") else None
                    ts = (pd.Timestamp(lt, unit="ms") + IST_OFFSET) if lt else pd.Timestamp.utcnow().tz_localize(None) + IST_OFFSET
                    flow.on_full(sym, ts, float(ltpc["ltp"]), float(vtt) if vtt is not None else None,
                                 (mff.get("marketLevel") or {}).get("bidAskQuote") or [],
                                 float(mff["tbq"]) if mff.get("tbq") is not None else None,
                                 float(mff["tsq"]) if mff.get("tsq") is not None else None)
                except Exception:
                    pass
            self.builder.on_tick(sym, float(ltpc["ltp"]), int(ltpc["ltt"]) if ltpc.get("ltt") else None,
                                 float(vtt) if vtt is not None else None, float(ltpc["cp"]) if ltpc.get("cp") else None)

    def _send(self, method: str, keys: list[str]) -> bool:
        import websocket
        ws = self._ws
        if not keys or ws is None or not getattr(ws, "sock", None) or self.status != "connected":
            return False
        data = {"instrumentKeys": keys} if method == "unsub" else {"mode": self.mode, "instrumentKeys": keys}
        ws.send(json.dumps({"guid": uuid.uuid4().hex, "method": method, "data": data}).encode(), opcode=websocket.ABNF.OPCODE_BINARY)
        return True

    def add(self, symbols: list[str], interval: str = "5m") -> dict:
        """Start streaming more symbols without reconnecting; backfill their history."""
        added, bad = [], {}
        for sym in [x.upper() for x in symbols]:
            if sym in self.symbols:
                continue
            try:
                k = instruments.key(sym)
            except Exception as e:
                bad[sym] = str(e)
                continue
            self.symbols.append(sym)
            self._keymap[k] = sym
            added.append(k)
            try:
                end = (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()
                self.history[sym] = self.conn.fetch_history(sym, str(end - timedelta(days=19)), str(end - timedelta(days=1)), interval)
                self.builder.merge_rest(sym, self.conn.intraday(sym, "1m"))
            except Exception as e:
                log.warning("backfill %s failed: %s", sym, e)
        self._send("sub", added)
        return {"added": [self._keymap[k] for k in added], "invalid": bad}

    def remove(self, symbols: list[str]) -> None:
        drop = [k for k, v in self._keymap.items() if v in {x.upper() for x in symbols}]
        self._send("unsub", drop)
        for k in drop:
            sym = self._keymap.pop(k)
            if sym in self.symbols:
                self.symbols.remove(sym)
            self.history.pop(sym, None)
            self.builder.bars.pop(sym, None)
            self.builder.last.pop(sym, None)

    # -------------------------------------------------------------- data for the engine
    def backfill(self, days: int = 15, interval: str = "5m") -> None:
        """Previous sessions from REST + the latest session's completed 1m candles."""
        self.interval = interval
        end = (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()
        for s in self.symbols:
            try:
                h = self.conn.fetch_history(s, str(end - timedelta(days=days + 4)), str(end - timedelta(days=1)), interval)
                self.history[s] = h
                self.builder.merge_rest(s, self.conn.intraday(s, "1m"))
            except Exception as e:
                log.warning("backfill %s failed: %s", s, e)
                self.error = f"backfill {s}: {e}"
        self.backfilled_at = time.time()

    def confirm(self, boundary: pd.Timestamp, symbols: list[str] | None = None) -> list[str]:
        """Right after a candle closes: pull Upstox's official 1-minute candles for every symbol (in parallel) so the
        closed candle the strategy decides on is the exchange's, not one rebuilt from ticks. Returns confirmed symbols."""
        from concurrent.futures import ThreadPoolExecutor
        last_min = boundary - pd.Timedelta(minutes=1)
        todo = [s for s in (symbols or list(self._keymap.values()) or self.symbols)
                if (self.builder.confirmed_upto(s) or pd.Timestamp(0)) < last_min]

        def one(s):
            try:
                df = self.conn.intraday(s, "1m")
                self.builder.merge_rest(s, df)
                return s if not df.empty and df.index.max() >= last_min else None
            except Exception as e:
                self.error = f"confirm {s}: {e}"
                return None
        if todo:
            with ThreadPoolExecutor(max_workers=min(16, len(todo))) as ex:
                list(ex.map(one, todo))
        self.confirm_stats = {"boundary": str(boundary), "checked": len(todo), "corrections": self.builder.corrections,
                              "at": str(pd.Timestamp.utcnow().tz_localize(None) + IST_OFFSET)}
        return [s for s in (symbols or self.symbols) if (self.builder.confirmed_upto(s) or pd.Timestamp(0)) >= last_min]

    def is_confirmed(self, sym: str, boundary: pd.Timestamp) -> bool:
        c = self.builder.confirmed_upto(sym)
        return c is not None and c >= boundary - pd.Timedelta(minutes=1)

    def reconcile(self) -> None:
        for s in [v for v in self._keymap.values()] or self.symbols:
            try:
                self.builder.merge_rest(s, self.conn.intraday(s, "1m"))
            except Exception as e:
                self.error = f"reconcile {s}: {e}"
        self.roll()

    def roll(self) -> None:
        """Move finished sessions from the tick builder into history (keeps memory flat, keeps the data)."""
        today = (pd.Timestamp.utcnow().tz_localize(None) + IST_OFFSET).normalize()
        minutes = _unit_interval(getattr(self, "interval", "5m"))[1]
        for s in self.symbols:
            old = self.builder.pop_before(s, today)
            if old.empty:
                continue
            bars = _resample(old, minutes)
            h = self.history.get(s, pd.DataFrame())
            h = pd.concat([f for f in (h, bars) if not f.empty])
            h = h[~h.index.duplicated(keep="last")].sort_index()
            self.history[s] = h[h.index >= today - pd.Timedelta(days=30)]

    def candles(self, sym: str, interval: str = "5m") -> pd.DataFrame:
        minutes = _unit_interval(interval)[1] if interval.endswith("m") else 5
        live = self.builder.candles(sym, minutes)
        hist = self.history.get(sym, pd.DataFrame())
        frames = [f for f in (hist, live) if not f.empty]
        if not frames:
            return live
        df = pd.concat(frames)
        df = df[~df.index.duplicated(keep="last")].sort_index()     # live bars win over REST history
        df.index.name = "ts"
        return df

    def session_1m(self, sym: str) -> pd.DataFrame:
        """Completed 1m bars currently held in the builder (for saving to the database)."""
        return self.builder.candles(sym, 1)

    def quote(self, sym: str) -> dict | None:
        return self.builder.last.get(sym)

    def info(self) -> dict:
        status = self.status
        if status == "stopped" and not tokens.token:
            status = "waiting_for_login"
        return {"status": status, "error": self.error, "ticks": self.ticks, "symbols": len(self.symbols),
                "candles": getattr(self, "confirm_stats", None),
                "seconds_since_last_message": round(time.time() - self.last_msg_at, 1) if self.last_msg_at else None}
