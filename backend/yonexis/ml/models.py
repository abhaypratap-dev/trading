"""Meta-label and imitation models.

* Meta-label model: P(signal hits target / ends profitable | features at signal time).
  Labels come from the deterministic backtester (triple-barrier: target, stop, or square-off).
* Imitation model: P(admin AGREES with the call | features), trained on live feedback.

Both are validated with purged, time-ordered folds (never random splits) and
calibrated so that a 0.7 score means roughly 70% of such calls work.
"""
from __future__ import annotations

import json
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV
from sklearn.metrics import brier_score_loss, roc_auc_score

from yonexis.backtest.engine import BacktestResult, normalize_candles

from .features import signal_features

try:  # LightGBM when available, sklearn otherwise
    from lightgbm import LGBMClassifier

    def _base_model():
        return LGBMClassifier(n_estimators=300, learning_rate=0.03, num_leaves=15, min_child_samples=20,
                              subsample=0.8, subsample_freq=1, colsample_bytree=0.8, verbose=-1)
except Exception:  # pragma: no cover
    from sklearn.ensemble import HistGradientBoostingClassifier

    def _base_model():
        return HistGradientBoostingClassifier(max_iter=300, learning_rate=0.05, max_leaf_nodes=15)


def build_meta_dataset(result: BacktestResult, data: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """One row per executed trade: features at signal time + label (1 = net profitable)."""
    norm = {s: normalize_candles(df) for s, df in data.items()}
    rows = []
    for t in result.trades.itertuples():
        f = signal_features(norm[t.symbol], t.signal_ts, t.side, t.tags if isinstance(t.tags, dict) else {})
        f.update({"_ts": pd.Timestamp(t.signal_ts), "_symbol": t.symbol, "_net_pnl": t.net_pnl,
                  "label": int(t.net_pnl > 0)})
        rows.append(f)
    return pd.DataFrame(rows).sort_values("_ts").reset_index(drop=True) if rows else pd.DataFrame()


def purged_time_folds(ts: pd.Series, n_folds: int = 5, embargo_days: int = 2):
    """Expanding-window folds by date with an embargo gap so no information leaks across the split."""
    days = np.array(sorted(ts.dt.normalize().unique()))
    if len(days) < n_folds + 2:
        return
    edges = np.linspace(0, len(days), n_folds + 2, dtype=int)
    for k in range(1, n_folds + 1):
        train_end = days[edges[k] - 1]
        test_start_i = min(edges[k] + embargo_days, len(days) - 1)
        test_days = days[test_start_i: edges[k + 1]]
        if len(test_days) == 0:
            continue
        tr = np.where(ts.dt.normalize() <= train_end)[0]
        te = np.where(ts.dt.normalize().isin(test_days))[0]
        if len(tr) > 20 and len(te) > 5:
            yield tr, te


@dataclass
class TrainedModel:
    kind: str                       # "meta" or "imitation"
    features: list[str]
    model: object
    metrics: dict
    version: str = field(default_factory=lambda: time.strftime("%Y%m%d-%H%M%S"))
    meta: dict = field(default_factory=dict)

    def predict_proba(self, rows: list[dict] | pd.DataFrame) -> np.ndarray:
        X = pd.DataFrame(rows)
        for f in self.features:
            if f not in X:
                X[f] = np.nan
        return self.model.predict_proba(X[self.features].astype(float))[:, 1]

    def save(self, folder: str | Path) -> Path:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / f"{self.kind}-{self.version}.pkl"
        with open(p, "wb") as fh:
            pickle.dump(self, fh)
        (folder / f"{self.kind}-{self.version}.json").write_text(json.dumps(
            {"kind": self.kind, "version": self.version, "features": self.features, "metrics": self.metrics, "meta": self.meta},
            indent=2, default=str))
        return p

    @staticmethod
    def load(path: str | Path) -> "TrainedModel":
        with open(path, "rb") as fh:
            return pickle.load(fh)


def train_classifier(df: pd.DataFrame, kind: str, label_col: str = "label", n_folds: int = 5) -> TrainedModel:
    feats = [c for c in df.columns if not c.startswith("_") and c != label_col]
    X, y, ts = df[feats].astype(float), df[label_col].astype(int), pd.to_datetime(df["_ts"])
    if y.nunique() < 2 or len(df) < 40:
        raise ValueError(f"need at least 40 rows with both outcomes; have {len(df)} rows, classes {y.unique().tolist()}")

    oof = pd.Series(np.nan, index=df.index)
    fold_scores = []
    for tr, te in purged_time_folds(ts, n_folds):
        m = _base_model().fit(X.iloc[tr], y.iloc[tr])
        p = m.predict_proba(X.iloc[te])[:, 1]
        oof.iloc[te] = p
        if y.iloc[te].nunique() == 2:
            fold_scores.append(roc_auc_score(y.iloc[te], p))

    mask = oof.notna()
    metrics = {"rows": int(len(df)), "base_rate": round(float(y.mean()), 4), "folds": len(fold_scores)}
    if mask.sum() > 0 and y[mask].nunique() == 2:
        metrics.update({
            "oos_auc": round(float(roc_auc_score(y[mask], oof[mask])), 4),
            "oos_brier": round(float(brier_score_loss(y[mask], oof[mask])), 4),
            "fold_auc": [round(float(s), 4) for s in fold_scores],
        })
        # How much better are high-confidence calls? (the number that matters for trading)
        hi = oof[mask] >= oof[mask].quantile(0.6)
        metrics["hit_rate_top40pct"] = round(float(y[mask][hi].mean()), 4)
        metrics["hit_rate_all"] = round(float(y[mask].mean()), 4)
        if "_net_pnl" in df:
            pnl = df.loc[mask, "_net_pnl"]
            metrics["pnl_all_oos"] = round(float(pnl.sum()), 2)
            metrics["pnl_top40pct_oos"] = round(float(pnl[hi].sum()), 2)

    # Final model: calibrated on time-ordered CV of the whole set.
    final = CalibratedClassifierCV(_base_model(), method="isotonic" if len(df) > 500 else "sigmoid", cv=3)
    final.fit(X, y)
    return TrainedModel(kind=kind, features=feats, model=final, metrics=metrics,
                        meta={"trained_from": str(ts.min()), "trained_to": str(ts.max())})


def train_meta(result: BacktestResult, data) -> TrainedModel:
    ds = build_meta_dataset(result, data)
    if ds.empty:
        raise ValueError("backtest produced no trades to learn from")
    return train_classifier(ds, "meta")


def train_imitation(feedback_rows: list[dict]) -> TrainedModel:
    """feedback_rows: [{'features': {...}, 'action': 'agree'|'reject'|'modify'|'skip', 'ts': ...}]"""
    rows = []
    for r in feedback_rows:
        if r["action"] not in ("agree", "reject", "modify"):
            continue
        f = dict(r["features"])
        f["_ts"] = pd.Timestamp(r["ts"])
        f["label"] = int(r["action"] in ("agree", "modify"))
        rows.append(f)
    return train_classifier(pd.DataFrame(rows).sort_values("_ts").reset_index(drop=True), "imitation")


def combine(ai_p: float, admin_p: float | None, w_admin: float = 0.5) -> float:
    """Ensemble of meta-label probability and admin-imitation probability."""
    if admin_p is None or admin_p != admin_p:
        return ai_p
    return (1 - w_admin) * ai_p + w_admin * admin_p
