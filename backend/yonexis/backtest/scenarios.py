"""Robustness testing: walk-forward, Monte Carlo, sensitivity, stress, regimes, holdout and the scorecard."""
from __future__ import annotations

import copy
import itertools
from dataclasses import replace
from typing import Any, Callable

import numpy as np
import pandas as pd

from yonexis.sdk import Strategy

from .engine import BacktestConfig, BacktestResult, daily_pnl, normalize_candles, run_backtest
from .metrics import compute_metrics, deflated_sharpe, max_drawdown, summarize_returns

# Known Indian market stress windows (inclusive). Extend from the admin UI.
STRESS_WINDOWS = {
    "covid_crash_2020": ("2020-02-20", "2020-04-30"),
    "rate_hikes_2022": ("2022-01-01", "2022-06-30"),
    "election_results_2024": ("2024-06-03", "2024-06-07"),
    "budget_day_2024": ("2024-07-23", "2024-07-23"),
    "budget_day_2025": ("2025-02-01", "2025-02-01"),
}


def _nop(*a, **k):
    pass


def slice_data(data: dict[str, pd.DataFrame], start=None, end=None) -> dict[str, pd.DataFrame]:
    out = {}
    for s, df in data.items():
        d = normalize_candles(df)
        if start is not None:
            d = d[d.index >= pd.Timestamp(start)]
        if end is not None:
            d = d[d.index < pd.Timestamp(end) + pd.Timedelta(days=1)]
        if not d.empty:
            out[s] = d
    return out


def trading_days(data: dict[str, pd.DataFrame]) -> list[pd.Timestamp]:
    days = set()
    for df in data.values():
        days.update(normalize_candles(df).index.normalize().unique())
    return sorted(days)


def split_holdout(data: dict[str, pd.DataFrame], holdout_frac: float = 0.2):
    """Lock the last `holdout_frac` of trading days. Agents and optimisers never see it."""
    days = trading_days(data)
    if len(days) < 5:
        return data, {}
    cut = days[int(len(days) * (1 - holdout_frac))]
    return slice_data(data, end=cut - pd.Timedelta(days=1)), slice_data(data, start=cut)


# --------------------------------------------------------------------------- walk-forward

def grid(param_space: dict[str, list]) -> list[dict]:
    if not param_space:
        return [{}]
    keys = list(param_space)
    return [dict(zip(keys, vals)) for vals in itertools.product(*(param_space[k] for k in keys))]


def objective(m: dict) -> float:
    """Default optimisation target: net P&L penalised by drawdown, needs enough trades."""
    if m.get("trades", 0) < 5:
        return -1e18
    return m["net_pnl"] + 0.5 * m.get("max_drawdown", 0.0)   # drawdown is negative


def walk_forward(strategy_cls: type[Strategy], data, param_space: dict[str, list] | None = None,
                 train_days: int = 120, test_days: int = 30, base_params: dict | None = None,
                 config: BacktestConfig | None = None,
                 score: Callable[[dict], float] = objective, progress: Callable = _nop) -> dict:
    days = trading_days(data)
    combos = grid(param_space or {})
    n_folds = max(0, (len(days) - train_days - test_days) // test_days + 1) if len(days) >= train_days + test_days else 0
    folds, oos_trades = [], []
    n_trials = 0
    start = 0
    while start + train_days + test_days <= len(days):
        tr_s, tr_e = days[start], days[start + train_days - 1]
        te_s, te_e = days[start + train_days], days[start + train_days + test_days - 1]
        train = slice_data(data, tr_s, tr_e)
        best, best_score = None, -np.inf
        progress(f"walk-forward window {len(folds) + 1}/{n_folds}: training {tr_s.date()}..{tr_e.date()} on {len(combos)} variants, "
                 f"testing {te_s.date()}..{te_e.date()}", {"done": len(folds), "total": n_folds})
        for p in combos:
            params = {**(base_params or {}), **p}
            m = run_backtest(strategy_cls, train, params, config).metrics
            n_trials += 1
            sc = score(m)
            if sc > best_score:
                best, best_score = params, sc
        test_res = run_backtest(strategy_cls, slice_data(data, te_s, te_e), best, config)
        oos_trades.append(test_res.trades)
        folds.append({"train": [str(tr_s.date()), str(tr_e.date())], "test": [str(te_s.date()), str(te_e.date())],
                      "best_params": best, "test_metrics": _brief(test_res.metrics)})
        tm = folds[-1]["test_metrics"]
        progress(f"  window {len(folds)} result: picked {best and {k: best[k] for k in (param_space or {})}} -> "
                 f"{tm.get('trades', 0)} trades, net Rs {tm.get('net_pnl', 0):,.0f}", {"done": len(folds), "total": n_folds})
        start += test_days
    if not folds:
        return {"folds": [], "note": f"need at least {train_days + test_days} trading days, have {len(days)}"}
    all_t = pd.concat(oos_trades, ignore_index=True) if oos_trades else pd.DataFrame()
    cfg = config or BacktestConfig()
    d = daily_pnl(all_t, cfg.starting_capital)
    m = compute_metrics(all_t, d)
    folds_pos = sum(1 for f in folds if f["test_metrics"].get("net_pnl", 0) > 0)
    return {"folds": folds, "oos_metrics": m, "n_trials": n_trials,
            "pct_profitable_folds": round(folds_pos / len(folds) * 100, 1)}


def _brief(m: dict) -> dict:
    return {k: m.get(k) for k in ("trades", "net_pnl", "win_rate", "profit_factor", "sharpe", "max_drawdown")}


# --------------------------------------------------------------------------- Monte Carlo

def monte_carlo(trades: pd.DataFrame, n_sims: int = 2000, starting_capital: float = 1_000_000.0,
                seed: int = 7) -> dict:
    """Reshuffle trade order and bootstrap trades to get a distribution of outcomes."""
    if trades.empty:
        return {"note": "no trades"}
    rng = np.random.default_rng(seed)
    pnl = trades["net_pnl"].to_numpy()
    n = len(pnl)
    shuffle_dd, boot_pnl, boot_dd = [], [], []
    for _ in range(n_sims):
        seq = rng.permutation(pnl)
        eq = starting_capital + np.cumsum(seq)
        shuffle_dd.append((eq - np.maximum.accumulate(np.concatenate([[starting_capital], eq]))[1:]).min())
        b = rng.choice(pnl, size=n, replace=True)
        eqb = starting_capital + np.cumsum(b)
        boot_pnl.append(b.sum())
        boot_dd.append((eqb - np.maximum.accumulate(np.concatenate([[starting_capital], eqb]))[1:]).min())
    boot_pnl, boot_dd, shuffle_dd = map(np.array, (boot_pnl, boot_dd, shuffle_dd))
    return {
        "n_sims": n_sims,
        "net_pnl_p5": round(float(np.percentile(boot_pnl, 5)), 2),
        "net_pnl_p50": round(float(np.percentile(boot_pnl, 50)), 2),
        "net_pnl_p95": round(float(np.percentile(boot_pnl, 95)), 2),
        "prob_loss": round(float((boot_pnl < 0).mean() * 100), 2),
        "max_dd_p95_worst": round(float(np.percentile(boot_dd, 5)), 2),
        "shuffle_max_dd_p95_worst": round(float(np.percentile(shuffle_dd, 5)), 2),
    }


# --------------------------------------------------------------------------- sensitivity

def sensitivity(strategy_cls: type[Strategy], data, params: dict, config: BacktestConfig | None = None,
                pct: float = 10.0, progress: Callable = _nop) -> dict:
    """Move every numeric parameter +/- pct and report the effect on net P&L."""
    base = run_backtest(strategy_cls, data, params, config).metrics
    rows = []
    full = {**strategy_cls.params, **params}
    keys = [k for k, v in full.items() if not isinstance(v, bool) and isinstance(v, (int, float)) and v != 0]
    for k, v in full.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v == 0:
            continue
        progress(f"sensitivity: {k} = {v} moved -/+{pct:g}%", {"done": keys.index(k), "total": len(keys)})
        for direction in (-1, 1):
            nv = v * (1 + direction * pct / 100)
            if isinstance(v, int):
                nv = max(1, round(nv)) if nv != v else v + direction
            m = run_backtest(strategy_cls, data, {**params, k: nv}, config).metrics
            rows.append({"param": k, "value": nv, "net_pnl": m.get("net_pnl", 0.0), "trades": m.get("trades", 0)})
    base_pnl = base.get("net_pnl", 0.0)
    worst = min((r["net_pnl"] for r in rows), default=base_pnl)
    fragile = base_pnl > 0 and worst < 0.5 * base_pnl
    return {"base_net_pnl": base_pnl, "rows": rows, "worst_net_pnl": worst, "fragile": fragile}


# --------------------------------------------------------------------------- stress

def stress(strategy_cls: type[Strategy], data, params: dict, config: BacktestConfig | None = None, progress: Callable = _nop) -> dict:
    cfg = config or BacktestConfig()
    out = {}
    shocks = {
        "costs_x2": replace(cfg, costs=replace(cfg.costs, multiplier=cfg.costs.multiplier * 2)),
        "slippage_x2": replace(cfg, slippage_bps=cfg.slippage_bps * 2),
        "slippage_x4": replace(cfg, slippage_bps=cfg.slippage_bps * 4),
    }
    for name, c in shocks.items():
        progress(f"stress test: {name.replace('_', ' ')}", {})
        out[name] = _brief(run_backtest(strategy_cls, data, params, c).metrics)
    windows = {}
    for name, (s, e) in STRESS_WINDOWS.items():
        sub = slice_data(data, s, e)
        if sub:
            progress(f"stress test: market window {name}", {})
            windows[name] = _brief(run_backtest(strategy_cls, sub, params, cfg).metrics)
    out["historical_windows"] = windows or {"note": "no data inside the known stress windows"}
    return out


# --------------------------------------------------------------------------- regimes

def classify_days(df: pd.DataFrame) -> pd.Series:
    """Label each day trending / sideways / volatile from its own candles."""
    d = normalize_candles(df)
    g = d.groupby(d.index.normalize())
    day = pd.DataFrame({"open": g["open"].first(), "close": g["close"].last(), "high": g["high"].max(), "low": g["low"].min()})
    rng = (day["high"] - day["low"]) / day["open"]
    body = (day["close"] - day["open"]).abs() / (day["high"] - day["low"]).replace(0, np.nan)
    vol_cut = rng.rolling(20, min_periods=5).quantile(0.8)
    label = np.where(rng > vol_cut, "volatile", np.where(body > 0.6, "trending", "sideways"))
    return pd.Series(label, index=day.index)


def regime_split(result: BacktestResult, data) -> dict:
    if result.trades.empty:
        return {}
    labels = {s: classify_days(df) for s, df in data.items()}
    t = result.trades.copy()
    t["day"] = pd.to_datetime(t["entry_ts"]).dt.normalize()
    t["regime"] = [labels.get(s, pd.Series(dtype=str)).get(d, "unknown") for s, d in zip(t["symbol"], t["day"])]
    out = {}
    for r, g in t.groupby("regime"):
        out[r] = {"trades": int(len(g)), "net_pnl": round(float(g["net_pnl"].sum()), 2),
                  "win_rate": round(float((g["net_pnl"] > 0).mean() * 100), 2)}
    return out


# --------------------------------------------------------------------------- scorecard

DEFAULT_THRESHOLDS = {
    "min_trades": 30,
    "min_profit_factor": 1.2,
    "min_sharpe": 0.8,
    "max_drawdown_pct": -15.0,
    "max_prob_loss_pct": 20.0,
    "min_deflated_sharpe": 0.90,
}


def full_evaluation(strategy_cls: type[Strategy], data, params: dict | None = None,
                    config: BacktestConfig | None = None, param_space: dict | None = None,
                    n_trials_so_far: int = 1, holdout_frac: float = 0.2,
                    baseline_net_pnl: float | None = None, thresholds: dict | None = None,
                    wf_train_days: int = 120, wf_test_days: int = 30, progress: Callable = _nop) -> dict:
    """The full battery used before a strategy can be promoted."""
    import time as _t
    t0 = _t.time()

    def say(msg, info=None):
        progress(f"[{_t.time() - t0:5.0f}s] {msg}", info or {})
    params = params or {}
    cfg = config or BacktestConfig()
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    dev, holdout = split_holdout(data, holdout_frac)

    n_c = sum(len(v) for v in data.values())
    say(f"evaluating on {len(data)} stocks, {n_c:,} candles; last {holdout_frac:.0%} of days kept aside as hold-out")
    say("1/6 development backtest (first 80% of the history)")
    dev_res = run_backtest(strategy_cls, dev, params, cfg)
    dm = dev_res.metrics
    say(f"    -> {dm.get('trades', 0)} trades, win {dm.get('win_rate', 0)}%, net Rs {dm.get('net_pnl', 0):,.0f}")
    say(f"2/6 walk-forward {'with ' + str(len(grid(param_space))) + ' variants' if param_space else 'skipped (no grid)'}")
    wf = walk_forward(strategy_cls, dev, param_space, wf_train_days, wf_test_days, params, cfg,
                      progress=lambda m, i: say(m, i)) if param_space else None
    n_trials = n_trials_so_far + (wf["n_trials"] if wf and "n_trials" in wf else 0)
    say("3/6 Monte Carlo (2000 reshuffles of the trades)")
    mc = monte_carlo(dev_res.trades, starting_capital=cfg.starting_capital)
    say("4/6 parameter sensitivity")
    sens = sensitivity(strategy_cls, dev, params, cfg, progress=lambda m, i: say(m, i))
    say("5/6 stress tests (double costs/slippage, crash windows)")
    st = stress(strategy_cls, data, params, cfg, progress=lambda m, i: say(m, i))
    reg = regime_split(dev_res, dev)
    say("6/6 hold-out backtest (data never used for tuning)")
    ho = run_backtest(strategy_cls, holdout, params, cfg) if holdout else None
    if ho is not None:
        say(f"    -> hold-out {ho.metrics.get('trades', 0)} trades, net Rs {ho.metrics.get('net_pnl', 0):,.0f}")

    rs = summarize_returns(dev_res.daily["ret"]) if not dev_res.daily.empty else {}
    dsr = deflated_sharpe(dev_res.metrics.get("sharpe", 0.0), max(n_trials, 1), rs.get("n", 0),
                          rs.get("skew", 0.0), rs.get("kurt", 3.0)) if rs else 0.0

    m = dev_res.metrics
    checks = {
        "enough_trades": m.get("trades", 0) >= th["min_trades"],
        "profit_factor": m.get("profit_factor", 0) >= th["min_profit_factor"],
        "sharpe": m.get("sharpe", 0) >= th["min_sharpe"],
        "drawdown": m.get("max_drawdown_pct", 0) >= th["max_drawdown_pct"],
        "monte_carlo_prob_loss": mc.get("prob_loss", 100) <= th["max_prob_loss_pct"],
        "deflated_sharpe": dsr >= th["min_deflated_sharpe"],
        "not_fragile": not sens["fragile"],
        "survives_2x_costs": st["costs_x2"].get("net_pnl", 0) > 0,
        "holdout_profitable": bool(ho and ho.metrics.get("net_pnl", 0) > 0),
    }
    if baseline_net_pnl is not None and ho is not None:
        checks["beats_baseline_on_holdout"] = ho.metrics.get("net_pnl", 0) > baseline_net_pnl
    passed = sum(checks.values())
    return {
        "params": params,
        "development": m,
        "walk_forward": wf,
        "monte_carlo": mc,
        "sensitivity": sens,
        "stress": st,
        "regimes": reg,
        "holdout": ho.metrics if ho else {"note": "not enough data for a holdout"},
        "deflated_sharpe_prob": round(dsr, 4),
        "n_trials": n_trials,
        "checks": checks,
        "score": f"{passed}/{len(checks)}",
        "verdict": "PASS" if all(checks.values()) else "REVIEW" if passed >= len(checks) - 2 else "FAIL",
        "thresholds": th,
    }


__all__ = [
    "STRESS_WINDOWS", "full_evaluation", "monte_carlo", "regime_split", "sensitivity", "slice_data",
    "split_holdout", "stress", "walk_forward", "max_drawdown", "copy", "Any",
]
