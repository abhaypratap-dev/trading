"""Pair trading (statistical arbitrage between two correlated NSE instruments), intraday only.

How it works (professional version, not "buy one sell the other"):
  1. PAIR SELECTION - discover_pairs(): over the last `lookback_days`, daily log returns must be highly correlated
     (corr >= min_corr) and the log-price spread must be mean reverting (half-life of an AR(1) fit between
     min_half_life and max_half_life candles). Sector pairs (HDFCBANK/ICICIBANK, TCS/INFY ...) usually qualify.
  2. HEDGE RATIO - beta from an OLS of log(A) on log(B) over the previous `beta_days` sessions (no look-ahead),
     so the position is beta-neutral: notional_B = beta * notional_A.
  3. SIGNAL - spread = log(A) - beta*log(B); z = (spread - rolling mean) / rolling std over `z_window` candles.
     z >= +entry_z -> A is rich: SELL A, BUY B. z <= -entry_z -> BUY A, SELL B.
  4. EXIT - z crosses back to exit_z (convergence = profit), or |z| >= stop_z (divergence keeps going = stop),
     or time stop `max_minutes`, or the 15:15 square-off. No new entries after entry_cutoff.
  5. COSTS - both legs pay the full Indian intraday cost model and slippage.
Results are per pair-trade (two legs) and roll into the same metrics as every other strategy.
"""
from __future__ import annotations

import itertools
import math
import uuid

import numpy as np
import pandas as pd

from yonexis.backtest.engine import BacktestConfig, _slip, daily_pnl, normalize_candles
from yonexis.backtest.metrics import compute_metrics

NAME = "pair_trading"
PARAMS = {"z_window": 48, "entry_z": 2.0, "exit_z": 0.3, "stop_z": 3.5, "beta_days": 20, "max_minutes": 150,
          "start": "09:45", "entry_cutoff": "14:30", "min_corr": 0.7, "lookback_days": 60,
          "min_half_life": 3, "max_half_life": 60, "max_pairs": 6, "max_trades_per_day": 2}
DEFAULT_PAIRS = [("HDFCBANK", "ICICIBANK"), ("AXISBANK", "KOTAKBANK"), ("TCS", "INFY"), ("HCLTECH", "WIPRO"),
                 ("SBIN", "BANKBARODA"), ("NIFTY-FUT", "BANKNIFTY-FUT"), ("HINDUNILVR", "ITC"), ("MARUTI", "HEROMOTOCO")]


def _align(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    a, b = normalize_candles(a), normalize_candles(b)
    j = a[["close"]].rename(columns={"close": "a"}).join(b[["close"]].rename(columns={"close": "b"}), how="inner")
    return j[(j["a"] > 0) & (j["b"] > 0)]


def half_life(spread: pd.Series) -> float:
    s = spread.dropna()
    if len(s) < 30:
        return math.inf
    lag, d = s.shift().iloc[1:], s.diff().iloc[1:]
    x = lag - lag.mean()
    b = float((x * (d - d.mean())).sum() / (x ** 2).sum()) if (x ** 2).sum() > 0 else 0.0
    return -math.log(2) / b if b < 0 else math.inf


def discover_pairs(data: dict[str, pd.DataFrame], p: dict | None = None, candidates: list[tuple] | None = None) -> list[dict]:
    """Rank candidate pairs (default: every combination of the given symbols) by correlation + mean reversion."""
    p = {**PARAMS, **(p or {})}
    syms = sorted(data)
    pool = candidates or list(itertools.combinations(syms, 2))
    daily = {s: normalize_candles(df)["close"].groupby(normalize_candles(df).index.normalize()).last().tail(p["lookback_days"])
             for s, df in data.items() if not df.empty}
    out = []
    for a, b in pool:
        if a not in daily or b not in daily or a == b:
            continue
        j = pd.concat([np.log(daily[a]), np.log(daily[b])], axis=1, keys=["a", "b"]).dropna()
        if len(j) < 20:
            continue
        corr = float(j.diff().corr().iloc[0, 1])
        if corr < p["min_corr"]:
            continue
        al = _align(data[a], data[b]).tail(p["lookback_days"] * 75)
        la, lb = np.log(al["a"]), np.log(al["b"])
        beta = float(np.polyfit(lb, la, 1)[0]) if len(al) > 50 else 1.0
        hl = half_life(la - beta * lb)
        if p["min_half_life"] <= hl <= p["max_half_life"]:
            out.append({"a": a, "b": b, "corr": round(corr, 3), "beta": round(beta, 3), "half_life": round(hl, 1)})
    return sorted(out, key=lambda r: (-r["corr"], r["half_life"]))[: p["max_pairs"]]


def zscore_frame(a: pd.DataFrame, b: pd.DataFrame, p: dict) -> pd.DataFrame:
    """Per 5-min candle: prices, beta (from previous sessions only), spread and z-score - all causal."""
    j = _align(a, b)
    la, lb = np.log(j["a"]), np.log(j["b"])
    day = j.index.normalize()
    days = pd.Index(sorted(set(day)))
    betas = {}
    for i, d in enumerate(days):
        win = j.index.normalize().isin(days[max(0, i - p["beta_days"]):i])
        betas[d] = float(np.polyfit(lb[win], la[win], 1)[0]) if win.sum() > 100 else np.nan
    j["beta"] = pd.Series(day.map(betas), index=j.index).astype(float)
    j["spread"] = la - j["beta"] * lb
    m = j["spread"].rolling(p["z_window"], min_periods=p["z_window"] // 2).mean()
    sd = j["spread"].rolling(p["z_window"], min_periods=p["z_window"] // 2).std()
    j["z"] = (j["spread"] - m) / sd.replace(0, np.nan)
    return j


def backtest_pair(sym_a: str, sym_b: str, a: pd.DataFrame, b: pd.DataFrame, params: dict | None = None,
                  cfg: BacktestConfig | None = None) -> list[dict]:
    """Two rows (one per leg) per pair trade, in the engine's trade format plus pair_id."""
    p = {**PARAMS, **(params or {})}
    cfg = cfg or BacktestConfig()
    j = zscore_frame(a, b, p)
    rows, pos, trades_today, day = [], None, 0, None
    t_start, t_cut, sq = p["start"], p["entry_cutoff"], "15:15"
    idx = j.index
    for i in range(1, len(j)):
        ts, r = idx[i], j.iloc[i]
        if ts.normalize() != day:
            day, trades_today = ts.normalize(), 0
        close_t = (ts + pd.Timedelta(minutes=5)).strftime("%H:%M")
        if pos is not None:
            z = r["z"]
            why = None
            if close_t >= sq or ts.normalize() != pos["day"]:
                why = "square_off"
            elif z == z and abs(z) <= p["exit_z"] or (z == z and np.sign(z) != pos["sign"]):
                why = "converged"
            elif z == z and abs(z) >= p["stop_z"]:
                why = "diverged_stop"
            elif (ts - pos["ts"]).total_seconds() / 60 >= p["max_minutes"]:
                why = "time_exit"
            if why:
                rows += _close(pos, r["a"], r["b"], ts, why, cfg)
                pos = None
            continue
        z = r["z"]
        if (z != z or r["beta"] != r["beta"] or r["beta"] <= 0 or not (t_start <= close_t <= t_cut)
                or trades_today >= p["max_trades_per_day"] or abs(z) < p["entry_z"] or abs(z) >= p["stop_z"]):
            continue
        sign = 1 if z > 0 else -1                   # +1: A rich -> sell A, buy B
        half = cfg.capital_per_trade / 2
        pos = {"pair_id": uuid.uuid4().hex[:10], "ts": ts, "day": ts.normalize(), "sign": sign, "z": float(z),
               "beta": float(r["beta"]), "a_side": "SELL" if sign > 0 else "BUY", "b_side": "BUY" if sign > 0 else "SELL",
               "a_px": _slip(r["a"], "SELL" if sign > 0 else "BUY", cfg.slippage_bps, True),
               "b_px": _slip(r["b"], "BUY" if sign > 0 else "SELL", cfg.slippage_bps, True)}
        pos["a_qty"] = max(1, math.floor(half / pos["a_px"]))
        pos["b_qty"] = max(1, math.floor(half * min(pos["beta"], 3.0) / pos["b_px"]))
        pos["syms"] = (sym_a, sym_b)
        trades_today += 1
    if pos is not None:
        rows += _close(pos, j.iloc[-1]["a"], j.iloc[-1]["b"], idx[-1], "end_of_data", cfg)
    return rows


def _close(pos, pa, pb, ts, why, cfg) -> list[dict]:
    out = []
    for leg, px_now in (("a", pa), ("b", pb)):
        side, entry, qty = pos[f"{leg}_side"], pos[f"{leg}_px"], pos[f"{leg}_qty"]
        exit_px = _slip(px_now, side, cfg.slippage_bps, False)
        d = 1 if side == "BUY" else -1
        gross = (exit_px - entry) * qty * d
        buy_v, sell_v = (entry * qty, exit_px * qty) if d == 1 else (exit_px * qty, entry * qty)
        cost = cfg.costs.round_trip(buy_v, sell_v)
        sym = pos["syms"][0 if leg == "a" else 1]
        out.append({"symbol": sym, "side": side, "signal_ts": pos["ts"], "entry_ts": pos["ts"], "entry": round(entry, 4),
                    "qty": qty, "stop_loss": None, "target": None, "exit_ts": ts, "exit": round(exit_px, 4),
                    "exit_reason": why, "gross_pnl": round(gross, 2), "costs": round(cost, 2), "net_pnl": round(gross - cost, 2),
                    "ret_pct": round((gross - cost) / (entry * qty) * 100, 4),
                    "reason": f"Pair {pos['syms'][0]}/{pos['syms'][1]} z={pos['z']:+.2f} beta={pos['beta']:.2f}",
                    "tags": {"pair_id": pos["pair_id"], "pair": f"{pos['syms'][0]}/{pos['syms'][1]}", "z": round(pos["z"], 2),
                             "beta": round(pos["beta"], 3), "family": "pairs"}})
    return out


def backtest(data: dict[str, pd.DataFrame], params: dict | None = None, cfg: BacktestConfig | None = None,
             pairs: list[tuple] | None = None) -> dict:
    """All pairs over the given data -> {"trades": DataFrame, "metrics": {...}, "pairs": [...]}."""
    cfg = cfg or BacktestConfig()
    found = [(x["a"], x["b"]) for x in discover_pairs(data, params, pairs)] if pairs is None else \
        [(a, b) for a, b in pairs if a in data and b in data]
    rows = []
    for a, b in found:
        rows += backtest_pair(a, b, data[a], data[b], params, cfg)
    t = pd.DataFrame(rows)
    d = daily_pnl(t, cfg.starting_capital) if not t.empty else pd.DataFrame()
    return {"trades": t, "metrics": compute_metrics(t, d) if not t.empty else {"trades": 0}, "pairs": found}
