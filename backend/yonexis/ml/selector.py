"""AI strategy picker: learns WHICH strategy to trust for this stock, this day, this moment.

Training data = every trade every strategy would have taken on all available history (Strategy Lab), plus the
paper trades actually taken (weighted up, so the model learns from its own live mistakes). For each candidate
trade we know, at decision time only:
  * market context of the stock: time of day, gap, trend strength (ADX, EMA slope), stretch from VWAP in ATRs,
    relative volume, volatility (ATR%, Bollinger width), RSI, position in the day's range, CPR width, the
    previous day's move, day of week;
  * index context (NIFTY futures/spot when available): index move so far and its trend;
  * which strategy / family proposed it and the side.
Label: did the trade end profitable after costs (plus the R multiple for expected value).

The model (gradient boosting, purged time-ordered CV, calibrated) outputs P(win). Expected value per candidate =
P(win) * avg_win_R(strategy) - (1 - P(win)) * avg_loss_R(strategy). The picker takes, per stock, only the best
candidate whose P(win) >= min_p and EV >= min_ev, one position at a time. Out-of-sample results of exactly this
rule are reported next to "every strategy" and "best single strategy" so you can see if the AI adds value.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from yonexis.backtest.engine import normalize_candles
from yonexis.sdk.indicators import adx, atr, bollinger, ema, pivots, rsi, rvol, session, vwap

FEATS = ["minutes_from_open", "gap_pct", "atr_pct", "rvol", "adx", "di_diff", "rsi", "vwap_dist_atr", "ema_trend_atr",
         "day_ret_atr", "prev_day_ret_pct", "range_pos", "bb_width_pct", "cpr_width_pct", "dow", "ret_3_atr",
         "mkt_day_ret_pct", "mkt_adx", "side_long"]


def feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Causal features at every candle of one symbol (value at t uses candles <= t)."""
    df = normalize_candles(df)
    c = df["close"]
    a = atr(df, 14)
    s = session(df)
    ax = adx(df, 14)
    out = pd.DataFrame(index=df.index)
    out["minutes_from_open"] = s["mins"]
    out["gap_pct"] = s["gap_pct"]
    out["atr_pct"] = a / c * 100
    out["rvol"] = rvol(df)
    out["adx"] = ax["adx"]
    out["di_diff"] = ax["pdi"] - ax["mdi"]
    out["rsi"] = rsi(c, 14)
    out["vwap_dist_atr"] = (c - vwap(df)) / a
    out["ema_trend_atr"] = (ema(c, 9) - ema(c, 21)) / a
    out["day_ret_atr"] = (c - s["day_open"]) / a
    daily = c.groupby(df.index.normalize()).last()
    out["prev_day_ret_pct"] = pd.Series(df.index.normalize().map(daily.pct_change().shift() * 100), index=df.index)
    rng = (s["day_high"] - s["day_low"]).replace(0, np.nan)
    out["range_pos"] = ((c - s["day_low"]) / rng).fillna(0.5)
    out["bb_width_pct"] = bollinger(c)["bb_width_pct"]
    out["cpr_width_pct"] = pivots(df)["cpr_width_pct"]
    out["dow"] = df.index.dayofweek.astype(float)
    out["ret_3_atr"] = (c - c.shift(3)) / a
    return out


def market_frame(data: dict[str, pd.DataFrame]) -> pd.DataFrame | None:
    for k in ("NIFTY-FUT", "NIFTY", "NIFTY50"):
        if k in data and not data[k].empty:
            df = normalize_candles(data[k])
            s = session(df)
            return pd.DataFrame({"mkt_day_ret_pct": (df["close"] / s["day_open"] - 1) * 100, "mkt_adx": adx(df)["adx"]},
                                index=df.index)
    return None


_IDX_CACHE: dict = {}


def index_5m(day: pd.Timestamp, conn=None) -> pd.DataFrame:
    """NIFTY index 5-minute candles up to `day` (stored history + that day's Upstox candles), exactly what the
    Strategy Lab trained the market features on. Cached for one minute."""
    import time as _t
    from yonexis import db
    key = str(pd.Timestamp(day).date())
    hit = _IDX_CACHE.get(key)
    if hit and _t.time() - hit[0] < 60:
        return hit[1]
    start = str((pd.Timestamp(day) - pd.Timedelta(days=45)).date())
    frames = [db.load_candles("NIFTY", "5m", start, key, real_only=True)]
    if conn is not None:
        try:
            today = pd.Timestamp.now().normalize()
            if pd.Timestamp(day).normalize() == today:
                one = conn.intraday("NIFTY", "1m")
            else:
                one = conn.fetch_history("NIFTY", key, key, "1m")
            if one is not None and not one.empty:
                from yonexis.data.connectors.upstox import _resample
                frames.append(_resample(one, 5))
        except Exception:
            pass
    frames = [f for f in frames if f is not None and not f.empty]
    df = pd.concat(frames) if frames else pd.DataFrame()
    if not df.empty:
        df.index = pd.to_datetime(df.index)
        df = df[~df.index.duplicated(keep="last")].sort_index()
    _IDX_CACHE[key] = (_t.time(), df)
    return df


def _decision_key(t) -> pd.Timestamp:
    """Features are taken from the last candle CLOSED at decision time."""
    ts = pd.Timestamp(t["signal_ts"])
    trig = (t.get("tags") or {}).get("trigger") if isinstance(t.get("tags"), dict) else None
    return ts - pd.Timedelta(minutes=5) if trig in ("touch", "reversal") else ts


def rows_for_trades(trades: pd.DataFrame, strategy: str, family: str, ff: dict[str, pd.DataFrame],
                    mkt: pd.DataFrame | None) -> pd.DataFrame:
    """Feature rows (+labels) for one strategy's trades. ff = {symbol: feature_frame}."""
    out = []
    for t in trades.to_dict("records"):
        f = ff.get(t["symbol"])
        if f is None or f.empty:
            continue
        key = _decision_key(t)
        i = f.index.searchsorted(key, side="right") - 1
        if i < 0:
            continue
        row = f.iloc[i].to_dict()
        if mkt is not None:
            j = mkt.index.searchsorted(key, side="right") - 1
            row.update(mkt.iloc[j].to_dict() if j >= 0 else {})
        tags = t.get("tags") if isinstance(t.get("tags"), dict) else {}
        risk = tags.get("risk")
        risk_rs = float(risk) * float(t["qty"]) if risk else abs(float(t["entry"]) - float(t["stop_loss"] or t["entry"])) * float(t["qty"])
        row.update({"side_long": 1.0 if t["side"] == "BUY" else 0.0, "_strategy": strategy, "_family": family,
                    "_symbol": t["symbol"], "_ts": key, "_entry_ts": pd.Timestamp(t["entry_ts"]),
                    "_exit_ts": pd.Timestamp(t["exit_ts"]), "_net_pnl": float(t["net_pnl"]),
                    "_r": float(t["net_pnl"]) / risk_rs if risk_rs and risk_rs > 0 else np.nan,
                    "label": int(t["net_pnl"] > 0)})
        out.append(row)
    return pd.DataFrame(out)


def design(ds: pd.DataFrame, strategies: list[str], families: list[str]) -> pd.DataFrame:
    """Model input: numeric features + one-hot strategy/family (fixed column order)."""
    X = pd.DataFrame(index=ds.index)
    for k in FEATS:
        X[k] = ds[k].astype(float) if k in ds else np.nan
    for s in strategies:
        X[f"strat_{s}"] = (ds["_strategy"] == s).astype(float)
    for fm in families:
        X[f"fam_{fm}"] = (ds["_family"] == fm).astype(float)
    return X


def strategy_r_stats(ds: pd.DataFrame) -> dict:
    out = {}
    for s, g in ds.groupby("_strategy"):
        r = g["_r"].replace([np.inf, -np.inf], np.nan).dropna().clip(-5, 10)
        w, l = r[r > 0], -r[r <= 0]
        out[s] = {"avg_win_r": float(w.mean()) if len(w) else 1.0, "avg_loss_r": float(l.mean()) if len(l) else 1.0,
                  "trades": int(len(g)), "win_rate": float((g["label"] == 1).mean())}
    return out


def expected_value(p: np.ndarray, strategies: pd.Series, stats: dict) -> np.ndarray:
    aw = strategies.map(lambda s: stats.get(s, {}).get("avg_win_r", 1.0)).to_numpy(dtype=float)
    al = strategies.map(lambda s: stats.get(s, {}).get("avg_loss_r", 1.0)).to_numpy(dtype=float)
    return p * aw - (1 - p) * al


def pick(ds: pd.DataFrame, score_col: str = "p", ev_col: str = "ev", min_p: float = 0.5, min_ev: float = 0.05) -> pd.DataFrame:
    """Greedy, time-ordered, one position per symbol: at each decision take the best candidate if it qualifies."""
    ds = ds[(ds[score_col] >= min_p) & (ds[ev_col] >= min_ev)].sort_values(["_ts", ev_col], ascending=[True, False])
    busy: dict[str, pd.Timestamp] = {}
    keep = []
    for i, r in ds.iterrows():
        if busy.get(r["_symbol"]) is not None and busy[r["_symbol"]] > r["_entry_ts"]:
            continue
        keep.append(i)
        busy[r["_symbol"]] = r["_exit_ts"]
    return ds.loc[keep]


THRESHOLDS = (0.35, 0.40, 0.45, 0.50, 0.55, 0.60)


def _stats(chosen: pd.DataFrame, days: int) -> dict:
    n = int(len(chosen))
    pnl = chosen["_net_pnl"].astype(float) if n else pd.Series(dtype=float)
    sd = float(pnl.std(ddof=1)) if n > 1 else 0.0
    return {"trades": n, "pnl": round(float(pnl.sum()), 2) if n else 0.0, "avg": round(float(pnl.mean()), 2) if n else 0.0,
            "win_rate": round(float((chosen["label"] == 1).mean()), 4) if n else None,
            "per_day": round(n / max(days, 1), 2),
            "t_stat": round(float(pnl.mean() / (sd / np.sqrt(n))), 2) if n > 1 and sd > 0 else None}


def selectivity(test: pd.DataFrame) -> dict:
    """How selective should the picker be? Choose the P(win) threshold on the EARLIER half of the held-back data,
    then judge it on the LATER half it never saw (so the threshold itself is not fitted to the result)."""
    test = test.sort_values("_ts")
    cut = test["_ts"].iloc[len(test) // 2] if len(test) else None
    early, late = (test[test["_ts"] < cut], test[test["_ts"] >= cut]) if cut is not None else (test, test)
    ndays = lambda d: int(pd.to_datetime(d["_ts"]).dt.normalize().nunique()) if len(d) else 0
    table = []
    for t in THRESHOLDS:
        table.append({"min_p": t, "all": _stats(pick(test, min_p=t), ndays(test)),
                      "early": _stats(pick(early, min_p=t), ndays(early)), "late": _stats(pick(late, min_p=t), ndays(late))})
    ok = [r for r in table if r["early"]["trades"] >= 30 and r["early"]["pnl"] > 0]
    best = max(ok, key=lambda r: r["early"]["avg"]) if ok else None
    late_all = float(late["_net_pnl"].mean()) if len(late) else 0.0
    v = best["late"] if best else None
    validated = bool(best and v["trades"] >= 30 and v["pnl"] > 0 and v["avg"] > late_all)
    return {"table": table, "recommended_min_p": best["min_p"] if best else 0.5,
            "validation": {"min_p": best["min_p"] if best else None, "late": v, "late_every_signal_avg": round(late_all, 2),
                           "from": str(cut) if cut is not None else None, "passed": validated}}


def train(ds: pd.DataFrame, weights: pd.Series | None = None) -> dict:
    """Fit the selector; return {"model": TrainedModel, "report": {...}} with honest out-of-sample comparisons."""
    from sklearn.metrics import roc_auc_score

    from .models import TrainedModel, _base_model, purged_time_folds

    ds = ds.sort_values("_ts").reset_index(drop=True)
    strategies = sorted(ds["_strategy"].unique())
    families = sorted(ds["_family"].unique())
    X, y, ts = design(ds, strategies, families), ds["label"].astype(int), pd.to_datetime(ds["_ts"])
    w = weights.reindex(ds.index).fillna(1.0).to_numpy() if weights is not None else None
    if y.nunique() < 2 or len(ds) < 200:
        raise ValueError(f"need at least 200 trades with wins and losses; have {len(ds)}")
    oof = pd.Series(np.nan, index=ds.index)
    for tr, te in purged_time_folds(ts, 5):
        m = _base_model()
        m.fit(X.iloc[tr], y.iloc[tr], **({"sample_weight": w[tr]} if w is not None else {}))
        oof.iloc[te] = m.predict_proba(X.iloc[te])[:, 1]
    stats = strategy_r_stats(ds)
    mask = oof.notna()
    ev_oof = pd.Series(np.nan, index=ds.index)
    ev_oof[mask] = expected_value(oof[mask].to_numpy(), ds.loc[mask, "_strategy"], stats)
    test = ds[mask].assign(p=oof[mask], ev=ev_oof[mask])
    sel = selectivity(test)
    chosen = pick(test, min_p=sel["recommended_min_p"])
    by_strat = test.groupby("_strategy")["_net_pnl"].sum().sort_values(ascending=False)
    report = {
        "rows": int(len(ds)), "oos_rows": int(mask.sum()), "base_win_rate": round(float(y.mean()), 4),
        "oos_auc": round(float(roc_auc_score(y[mask], oof[mask])), 4) if mask.sum() and y[mask].nunique() == 2 else None,
        "oos_all_strategies_pnl": round(float(test["_net_pnl"].sum()), 2),
        "oos_best_single_strategy": by_strat.index[0] if len(by_strat) else None,
        "oos_best_single_pnl": round(float(by_strat.iloc[0]), 2) if len(by_strat) else None,
        "oos_ai_pick_trades": int(len(chosen)), "oos_ai_pick_pnl": round(float(chosen["_net_pnl"].sum()), 2),
        "oos_ai_pick_win_rate": round(float((chosen["label"] == 1).mean()), 4) if len(chosen) else None,
        "oos_ai_pick_by_strategy": chosen.groupby("_strategy")["_net_pnl"].agg(["count", "sum"]).round(2).reset_index()
                                       .to_dict("records") if len(chosen) else [],
        "strategy_stats": stats, "trained_from": str(ts.min()), "trained_to": str(ts.max()),
        "min_p": sel["recommended_min_p"], "selectivity": sel["table"], "validation": sel["validation"],
        "oos_days": int(pd.to_datetime(test["_ts"]).dt.normalize().nunique()),
    }
    from sklearn.calibration import CalibratedClassifierCV
    final = CalibratedClassifierCV(_base_model(), method="isotonic" if len(ds) > 2000 else "sigmoid", cv=3)
    final.fit(X, y, **({"sample_weight": w} if w is not None else {}))
    model = TrainedModel(kind="selector", features=list(X.columns), model=final,
                         metrics={k: v for k, v in report.items() if not isinstance(v, (dict, list))},
                         meta={"strategies": strategies, "families": families, "stats": stats, "min_p": sel["recommended_min_p"]})
    all_avg = float(test["_net_pnl"].mean()) if len(test) else 0.0
    ai_avg = float(chosen["_net_pnl"].mean()) if len(chosen) else 0.0
    report["oos_all_avg_per_trade"], report["oos_ai_avg_per_trade"] = round(all_avg, 2), round(ai_avg, 2)
    report["oos_ai_t_stat"] = _stats(chosen, report["oos_days"])["t_stat"]
    # in use only if it beats taking every signal AND the threshold, chosen on earlier data, also held up on later data
    report["beats_baseline"] = bool(report["oos_auc"] and report["oos_auc"] > 0.53 and report["oos_ai_pick_pnl"] > 0
                                    and ai_avg > all_avg and sel["validation"]["passed"])
    return {"model": model, "report": report}


def min_p_for(model) -> float:
    """P(win) a call needs: the admin's override (Strategy Lab card) or the threshold the lab validated."""
    from yonexis import db
    v = db.get_setting("ai_min_p")
    try:
        if v is not None:
            return float(v)
    except (TypeError, ValueError):
        pass
    return float((getattr(model, "meta", {}) or {}).get("min_p", 0.5))


def score(model, rows: pd.DataFrame) -> pd.DataFrame:
    """rows: feature rows with _strategy/_family -> adds p and ev."""
    meta = model.meta
    X = design(rows, meta["strategies"], meta["families"])
    for f in model.features:
        if f not in X:
            X[f] = 0.0
    p = model.model.predict_proba(X[model.features].astype(float))[:, 1]
    return rows.assign(p=p, ev=expected_value(p, rows["_strategy"], meta["stats"]))
