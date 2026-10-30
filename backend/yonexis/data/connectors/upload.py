"""Admin uploads: Excel, CSV, JSON or Parquet with OHLCV candles or backtest trades.

Columns are auto-detected from common names; the admin can override with an
explicit mapping. Every upload is validated before it is stored.
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pandas as pd

CANDLE_ALIASES = {
    "ts": ["ts", "datetime", "timestamp", "date_time", "time", "date"],
    "open": ["open", "o", "open_price"],
    "high": ["high", "h", "high_price"],
    "low": ["low", "l", "low_price"],
    "close": ["close", "c", "close_price", "ltp", "last"],
    "volume": ["volume", "vol", "v", "qty", "quantity", "tottrdqty"],
    "symbol": ["symbol", "ticker", "scrip", "tradingsymbol", "instrument", "stock"],
}

TRADE_ALIASES = {
    "symbol": ["symbol", "ticker", "scrip", "stock", "tradingsymbol"],
    "side": ["side", "type", "direction", "signal", "action", "buy/sell", "position"],
    "entry_ts": ["entry_ts", "entry_time", "entry time", "entry date", "entry_date", "date/time", "datetime", "date"],
    "entry": ["entry", "entry_price", "entry price", "buy price", "price"],
    "exit_ts": ["exit_ts", "exit_time", "exit time", "exit date", "exit_date"],
    "exit": ["exit", "exit_price", "exit price", "sell price"],
    "net_pnl": ["net_pnl", "pnl", "profit", "p&l", "net p&l", "profit/loss"],
}


def read_any(filename: str, content: bytes) -> pd.DataFrame:
    ext = Path(filename).suffix.lower()
    buf = io.BytesIO(content)
    if ext in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(buf)
    if ext == ".json":
        return pd.read_json(buf)
    if ext == ".parquet":
        return pd.read_parquet(buf)
    if ext in (".tsv", ".txt"):
        return pd.read_csv(buf, sep=None, engine="python")
    return pd.read_csv(buf)


def detect_mapping(columns: list[str], aliases: dict[str, list[str]]) -> dict[str, str]:
    norm = {c.strip().lower(): c for c in columns}
    out = {}
    for target, names in aliases.items():
        for n in names:
            if n in norm and norm[n] not in out.values():
                out[target] = norm[n]
                break
    return out


def preview(filename: str, content: bytes) -> dict:
    df = read_any(filename, content)
    cand = detect_mapping(list(df.columns), CANDLE_ALIASES)
    trade = detect_mapping(list(df.columns), TRADE_ALIASES)
    kind = "candles" if all(k in cand for k in ("open", "high", "low", "close")) else "trades" if "side" in trade else "unknown"
    return {"columns": list(map(str, df.columns)), "rows": int(len(df)), "kind_guess": kind,
            "candle_mapping": cand, "trade_mapping": trade,
            "sample": df.head(5).astype(str).to_dict("records")}


def parse_candles(df: pd.DataFrame, mapping: dict[str, str] | None = None, default_symbol: str | None = None,
                  date_col: str | None = None, time_col: str | None = None) -> tuple[dict[str, pd.DataFrame], dict]:
    """Returns ({symbol: candles}, validation report)."""
    m = {**detect_mapping(list(df.columns), CANDLE_ALIASES), **(mapping or {})}
    if date_col and time_col:
        ts = pd.to_datetime(df[date_col].astype(str) + " " + df[time_col].astype(str), errors="coerce")
    else:
        ts = pd.to_datetime(df[m["ts"]], errors="coerce", dayfirst=False)
    out = pd.DataFrame({"ts": ts})
    for k in ("open", "high", "low", "close"):
        out[k] = pd.to_numeric(df[m[k]], errors="coerce")
    out["volume"] = pd.to_numeric(df[m["volume"]], errors="coerce").fillna(0) if "volume" in m else 0.0
    out["symbol"] = df[m["symbol"]].astype(str).str.upper().str.replace(".NS", "", regex=False) if "symbol" in m else (default_symbol or "UNKNOWN").upper()

    report = {"input_rows": int(len(out)), "issues": []}
    bad_ts = out["ts"].isna().sum()
    bad_px = out[["open", "high", "low", "close"]].isna().any(axis=1).sum()
    out = out.dropna(subset=["ts", "open", "high", "low", "close"])
    if bad_ts:
        report["issues"].append(f"{bad_ts} rows with unreadable timestamps dropped")
    if bad_px:
        report["issues"].append(f"{bad_px} rows with missing prices dropped")
    insane = (out["high"] < out[["open", "close"]].max(axis=1) - 1e-9) | (out["low"] > out[["open", "close"]].min(axis=1) + 1e-9)
    if insane.any():
        report["issues"].append(f"{int(insane.sum())} rows where high/low do not contain open/close (fixed)")
        out["high"] = out[["high", "open", "close"]].max(axis=1)
        out["low"] = out[["low", "open", "close"]].min(axis=1)
    dupes = out.duplicated(["symbol", "ts"]).sum()
    if dupes:
        report["issues"].append(f"{int(dupes)} duplicate timestamps (kept last)")
    out = out.drop_duplicates(["symbol", "ts"], keep="last").sort_values(["symbol", "ts"])

    result = {}
    for sym, g in out.groupby("symbol"):
        g = g.set_index("ts")[["open", "high", "low", "close", "volume"]]
        step = g.index.to_series().diff().dropna()
        interval_min = int(step.mode().iloc[0].total_seconds() // 60) if len(step) else 0
        jumps = (g["close"].pct_change().abs() > 0.25).sum()
        if jumps:
            report["issues"].append(f"{sym}: {int(jumps)} moves above 25% between candles - check for unadjusted splits/bonuses")
        result[sym] = g
        report.setdefault("symbols", {})[sym] = {"rows": int(len(g)), "start": str(g.index.min()), "end": str(g.index.max()),
                                                 "interval_minutes": interval_min}
    report["output_rows"] = int(sum(len(g) for g in result.values()))
    return result, report


def interval_label(minutes: int) -> str:
    return "1d" if minutes >= 1440 or minutes == 0 else f"{minutes}m" if minutes < 60 else f"{minutes // 60}h"


def parse_trades(df: pd.DataFrame, mapping: dict[str, str] | None = None) -> pd.DataFrame:
    """Normalise an admin backtest trade list (TradingView / Amibroker / Excel exports)."""
    m = {**detect_mapping(list(df.columns), TRADE_ALIASES), **(mapping or {})}
    out = pd.DataFrame()
    out["symbol"] = df[m["symbol"]].astype(str).str.upper().str.replace(".NS", "", regex=False) if "symbol" in m else "UNKNOWN"
    side = df[m["side"]].astype(str).str.upper()
    out["side"] = np.where(side.str.contains("BUY|LONG"), "BUY", np.where(side.str.contains("SELL|SHORT"), "SELL", side))
    out["entry_ts"] = pd.to_datetime(df[m["entry_ts"]], errors="coerce")
    out["entry"] = pd.to_numeric(df[m["entry"]], errors="coerce") if "entry" in m else np.nan
    if "exit_ts" in m:
        out["exit_ts"] = pd.to_datetime(df[m["exit_ts"]], errors="coerce")
    if "exit" in m:
        out["exit"] = pd.to_numeric(df[m["exit"]], errors="coerce")
    if "net_pnl" in m:
        out["net_pnl"] = pd.to_numeric(df[m["net_pnl"]], errors="coerce")
    return out.dropna(subset=["entry_ts"]).sort_values("entry_ts").reset_index(drop=True)
