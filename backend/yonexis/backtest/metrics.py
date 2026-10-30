"""Performance metrics. All computed from trades after costs."""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

TRADING_DAYS = 250


def max_drawdown(equity: pd.Series) -> tuple[float, float]:
    """Returns (max drawdown in Rs, max drawdown in % of peak)."""
    if equity.empty:
        return 0.0, 0.0
    peak = equity.cummax()
    dd = equity - peak
    pct = (dd / peak).min() * 100
    return float(dd.min()), float(pct)


def sharpe(daily_ret: pd.Series) -> float:
    r = daily_ret.dropna()
    if len(r) < 2 or r.std() == 0:
        return 0.0
    return float(r.mean() / r.std() * math.sqrt(TRADING_DAYS))


def compute_metrics(trades: pd.DataFrame, daily: pd.DataFrame) -> dict:
    n = len(trades)
    if n == 0:
        return {"trades": 0, "net_pnl": 0.0, "note": "no trades"}
    pnl = trades["net_pnl"]
    wins, losses = pnl[pnl > 0], pnl[pnl <= 0]
    gross_win, gross_loss = wins.sum(), -losses.sum()
    dd_abs, dd_pct = max_drawdown(daily["equity"]) if not daily.empty else (0.0, 0.0)
    days = max(len(daily), 1)
    first, last = pd.to_datetime(trades["entry_ts"]).min(), pd.to_datetime(trades["exit_ts"]).max()
    months = max((last - first).days / 30.44, 1 / 30.44)
    return {
        "trades": int(n),
        "net_pnl": round(float(pnl.sum()), 2),
        "gross_pnl": round(float(trades["gross_pnl"].sum()), 2),
        "total_costs": round(float(trades["costs"].sum()), 2),
        "win_rate": round(len(wins) / n * 100, 2),
        "avg_win": round(float(wins.mean()) if len(wins) else 0.0, 2),
        "avg_loss": round(float(losses.mean()) if len(losses) else 0.0, 2),
        "payoff_ratio": round(float(wins.mean() / -losses.mean()) if len(wins) and len(losses) and losses.mean() < 0 else 0.0, 3),
        "profit_factor": round(float(gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0, 3),
        "expectancy": round(float(pnl.mean()), 2),
        "avg_ret_pct": round(float(trades["ret_pct"].mean()), 4) if "ret_pct" in trades else None,
        "ret_pct_std": round(float(trades["ret_pct"].std()), 4) if "ret_pct" in trades and n > 1 else None,
        "sharpe": round(sharpe(daily["ret"]) if not daily.empty else 0.0, 3),
        "max_drawdown": round(dd_abs, 2),
        "max_drawdown_pct": round(dd_pct, 3),
        "trading_days": int(days),
        "trades_per_month": round(n / months, 2),
        "exit_reasons": trades["exit_reason"].value_counts().to_dict(),
        "by_side": {s: round(float(g["net_pnl"].sum()), 2) for s, g in trades.groupby("side")},
        "start": str(first), "end": str(last),
    }


def deflated_sharpe(observed_sr: float, n_trials: int, n_obs: int, skew: float = 0.0, kurt: float = 3.0,
                    trial_sr_std: float | None = None) -> float:
    """Probability that the observed (annualised) Sharpe is real after testing `n_trials` variants.

    Bailey & Lopez de Prado (2014). Returns a probability in [0, 1]; > 0.95 is a strong result.
    """
    from statistics import NormalDist

    nd = NormalDist()
    if n_obs < 3:
        return 0.0
    sr = observed_sr / math.sqrt(TRADING_DAYS)          # de-annualise to per-observation
    sr_std = (trial_sr_std / math.sqrt(TRADING_DAYS)) if trial_sr_std else 1 / math.sqrt(n_obs)
    if n_trials > 1:
        g = 0.5772156649
        e_max = sr_std * ((1 - g) * nd.inv_cdf(1 - 1 / n_trials) + g * nd.inv_cdf(1 - 1 / (n_trials * math.e)))
    else:
        e_max = 0.0
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr ** 2))
    z = (sr - e_max) * math.sqrt(n_obs - 1) / denom
    return float(nd.cdf(z))


def summarize_returns(daily_ret: pd.Series) -> dict:
    r = daily_ret.dropna()
    if r.empty:
        return {}
    return {"skew": float(r.skew()), "kurt": float(r.kurt() + 3), "n": int(len(r)), "mean": float(r.mean()),
            "std": float(r.std()), "pct_positive_days": float((r > 0).mean() * 100)}


__all__ = ["compute_metrics", "deflated_sharpe", "max_drawdown", "sharpe", "summarize_returns", "np"]
