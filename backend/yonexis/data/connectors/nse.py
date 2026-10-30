"""NSE public end-of-day data (bhavcopy with delivery %, corporate actions).

NSE changes these URLs from time to time and blocks clients without browser-like
headers, so URLs are configurable and failures are reported, not hidden.
"""
from __future__ import annotations

import io
import os

import httpx
import pandas as pd

HEADERS = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36",
           "Accept": "*/*", "Referer": "https://www.nseindia.com/"}
BHAV_URL = os.getenv("NSE_BHAV_URL", "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv")
CORP_ACTIONS_URL = os.getenv("NSE_CORP_ACTIONS_URL", "https://www.nseindia.com/api/corporates-corporateActions?index=equities&symbol={symbol}")


def bhavcopy(day: str) -> pd.DataFrame:
    d = pd.Timestamp(day)
    r = httpx.get(BHAV_URL.format(ddmmyyyy=d.strftime("%d%m%Y")), headers=HEADERS, timeout=30, follow_redirects=True)
    r.raise_for_status()
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = [c.strip().upper() for c in df.columns]
    df = df[df["SERIES"].astype(str).str.strip() == "EQ"]
    out = pd.DataFrame({
        "symbol": df["SYMBOL"].str.strip(), "ts": d.normalize(),
        "open": pd.to_numeric(df["OPEN_PRICE"], errors="coerce"), "high": pd.to_numeric(df["HIGH_PRICE"], errors="coerce"),
        "low": pd.to_numeric(df["LOW_PRICE"], errors="coerce"), "close": pd.to_numeric(df["CLOSE_PRICE"], errors="coerce"),
        "volume": pd.to_numeric(df["TTL_TRD_QNTY"], errors="coerce"),
        "delivery_pct": pd.to_numeric(df.get("DELIV_PER"), errors="coerce"),
    })
    return out


def corporate_actions(symbol: str) -> list[dict]:
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as c:
        c.get("https://www.nseindia.com/")           # sets cookies NSE requires
        r = c.get(CORP_ACTIONS_URL.format(symbol=symbol.upper()))
        r.raise_for_status()
        return r.json()
