"""Nightly jobs (run by the `scheduler` container):
  * after market close, retrain the meta-label model for every paper/live strategy on stored data
  * retrain the admin-imitation model when enough reviewed calls exist
New models are registered in SHADOW status; the admin (or the auto-promote rule) makes them champion
only when they beat the current champion out of sample.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from yonexis import db, feedback
from yonexis.backtest import run_backtest
from yonexis.config import settings
from yonexis.ml import train_imitation, train_meta
from yonexis.strategies import REGISTRY
from yonexis.strategies.loader import LIVE_STATUSES, latest_versions, load_code

log = logging.getLogger("yonexis.scheduler")
IST = ZoneInfo("Asia/Kolkata")
RUN_AT = (16, 15)        # 4:15 pm IST
AUTO_PROMOTE_MIN_AUC_GAIN = 0.01


def _champion(kind: str, strategy: str | None):
    rows = [m for m in db.fetch(db.models) if m["kind"] == kind and m["strategy"] == strategy and m["status"] == "champion"]
    return rows[-1] if rows else None


def _register(kind: str, strategy: str | None, m) -> None:
    p = m.save(settings.models_dir)
    mid = db.insert_row(db.models, kind=kind, version=m.version, strategy=strategy, path=str(p), metrics=db.j(m.metrics), status="shadow")
    champ = _champion(kind, strategy)
    new_auc = m.metrics.get("oos_auc") or 0
    old_auc = (db.uj(champ["metrics"], {}) or {}).get("oos_auc", 0) if champ else 0
    if new_auc > 0.52 and new_auc >= old_auc + AUTO_PROMOTE_MIN_AUC_GAIN:
        if champ:
            db.update_row(db.models, champ["id"], status="retired")
        db.update_row(db.models, mid, status="champion")
        db.log("scheduler", "model_auto_promoted", {"id": mid, "kind": kind, "auc": new_auc, "previous": old_auc})
    else:
        db.log("scheduler", "model_trained_shadow", {"id": mid, "kind": kind, "auc": new_auc, "champion_auc": old_auc})


def nightly() -> None:
    db.init()
    end = datetime.now(IST).date()
    start = end - timedelta(days=730)
    syms = list(settings.WATCHLIST)
    for row in latest_versions().values():
        if row["status"] not in LIVE_STATUSES:
            continue
        cls = REGISTRY.get(row["name"]) if row["origin"] == "builtin" else load_code(row["code"])
        data = {s: df for s in syms if not (df := db.load_candles(s, settings.LIVE_INTERVAL, str(start), str(end))).empty}
        if not data:
            log.warning("no stored candles; skipping meta retrain for %s", row["name"])
            continue
        try:
            res = run_backtest(cls, data, db.uj(row["params"], {}) or {})
            _register("meta", row["name"], train_meta(res, data))
        except Exception as e:
            log.warning("meta retrain failed for %s: %s", row["name"], e)
    try:
        from yonexis.performance import auto_pause_underperformers
        auto_pause_underperformers()
    except Exception as e:
        log.warning("auto-pause check failed: %s", e)
    # Strategy Lab: re-backtest every strategy on all history incl. today and retrain the AI picker; today's paper
    # trades (and their losses) are part of the training data, weighted x3.
    try:
        from yonexis import lab
        r = lab.run(None, lambda m, i: log.info("lab: %s", m), start=str(start))
        log.info("lab done: picker %s", (r.get("selector") or {}).get("status"))
    except Exception as e:
        log.warning("strategy lab failed: %s", e)
    rows = feedback.rows_with_outcomes()
    if len(rows) >= 40:
        try:
            _register("imitation", None, train_imitation([{"features": r["features"], "action": r["action"], "ts": r["signal_ts"]} for r in rows]))
        except Exception as e:
            log.warning("imitation retrain failed: %s", e)


def main() -> None:  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    last = None
    while True:
        now = datetime.now(IST)
        if now.weekday() < 5 and (now.hour, now.minute) >= RUN_AT and last != now.date():
            log.info("running nightly jobs")
            nightly()
            last = now.date()
        time.sleep(60)


if __name__ == "__main__":
    main()
