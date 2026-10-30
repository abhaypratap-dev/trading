"""Phase 4 setup, one click: real history -> baseline full evaluation -> AI score model (shadow) -> report.

1. Download 5-minute Upstox history for every watchlist stock that has less than ~1 year stored.
2. Full evaluation of every strategy in paper status on that real data: this sets the band paper results are judged by.
3. Train the meta-label model (probability a call works) on the real backtest trades; it is promoted to champion only
   if it beats chance out of sample (AUC > 0.53 and the top-scored calls hit more often than average).
"""
from __future__ import annotations

import time
from concurrent.futures import TimeoutError as FuturesTimeout
from datetime import datetime, timedelta

import pandas as pd

from yonexis import db
from yonexis.config import settings

MIN_CANDLES = 15000   # ~1 year of 5m candles


def run(engine, progress, start: str = "2024-01-01", max_symbols: int = 20) -> dict:
    from yonexis.data.connectors import upstox as ux
    from yonexis.strategies.loader import latest_versions

    out: dict = {"started": datetime.now().isoformat(timespec="seconds"), "steps": []}
    syms = (engine.symbols if engine else db.get_watchlist(settings.WATCHLIST))[:max_symbols]
    end = str((pd.Timestamp.now() - pd.Timedelta(days=1)).date())

    # 1 ---------------------------------------------------------------- history
    have = {c["symbol"]: c["n"] for c in db.candle_inventory() if c["interval"] == "5m" and c.get("source") != "simulated"}
    need = [s for s in syms if have.get(s, 0) < MIN_CANDLES]
    dl = {}
    if need:
        if not ux.tokens.token:
            raise RuntimeError("log in to Upstox first (Data tab) - history download needs it")
        c = ux.UpstoxConnector()
        for i, s in enumerate(need):
            progress(f"1/3 downloading history {s} ({i + 1}/{len(need)})", {"done": i, "total": len(need)})
            try:
                df = c.fetch_history(s, start, end, "5m")
                dl[s] = db.save_candles(s, df, "5m", "upstox") if not df.empty else 0
            except Exception as e:
                dl[s] = f"error: {e}"
    out["steps"].append({"step": "history", "downloaded": dl, "already_had": [s for s in syms if s not in need]})

    progress("loading stored candles", {})
    data = {}
    for s in syms:
        df = db.load_candles(s, "5m", start, end, real_only=True)
        if len(df) >= 2000:
            data[s] = df
    if not data:
        raise RuntimeError("no real 5m history available")

    # 2 ---------------------------------------------------------------- baseline evaluation
    evals = {}
    for row in latest_versions().values():
        if row["status"] not in ("paper", "live_small", "live"):
            continue
        params = db.uj(row["params"], {}) or {}
        progress(f"2/3 full evaluation of {row['name']} on {len(data)} stocks, {sum(len(v) for v in data.values()):,} candles", {})
        # runs in the sandbox process so the live engine keeps full speed
        from yonexis.sandbox import run as sandbox_run
        r = sandbox_run(row["code"], data, {"kind": "full", "params": params, "param_space": {"rr": [1.5, 2.0, 2.5]},
                                            "wf_train_days": 120, "wf_test_days": 30}, timeout=3600,
                       progress=lambda m, i: progress(f"2/3 {row['name']}: {m.strip()}", i))
        if not r.get("ok"):
            evals[row["name"]] = {"error": r.get("error")}
            continue
        ev = r["result"]
        bid = db.insert_row(db.backtests, strategy=row["name"], strategy_version=row["version"], kind="full",
                            params=db.j({**params, "_source": "db", "_phase4": True}), symbols=db.j(sorted(data)),
                            start=start, end=end, result=db.j(ev))
        evals[row["name"]] = {"backtest_id": bid, "verdict": ev["verdict"], "score": ev["score"],
                              "net_pnl": ev["development"].get("net_pnl"), "win_rate": ev["development"].get("win_rate"),
                              "trades": ev["development"].get("trades"), "holdout_net": (ev.get("holdout") or {}).get("net_pnl"),
                              "checks": ev["checks"]}

        # 3 ------------------------------------------------------------ AI score model
        progress(f"3/3 training the AI score model for {row['name']}", {})
        try:
            from concurrent.futures import ProcessPoolExecutor
            import multiprocessing as mp
            with ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn")) as ex:
                fut = ex.submit(_train, row["origin"], row["name"], row["code"], params, data)
                t0 = time.time()
                while True:
                    try:
                        m = fut.result(timeout=15)
                        break
                    except FuturesTimeout:
                        if time.time() - t0 > 3600:
                            raise RuntimeError("training took longer than 1 hour")
                        progress(f"3/3 still training the AI score model for {row['name']} ({time.time() - t0:.0f}s)", {})
            m.meta.update({"strategy": row["name"], "strategy_version": row["version"], "symbols": sorted(data), "phase4": True})
            path = m.save(settings.models_dir)
            mid = db.insert_row(db.models, kind="meta", version=m.version, strategy=row["name"], path=str(path),
                                metrics=db.j(m.metrics), status="shadow")
            good = (m.metrics.get("oos_auc") or 0) > 0.53 and (m.metrics.get("hit_rate_top40pct") or 0) > (m.metrics.get("hit_rate_all") or 1)
            if good:
                for other in db.fetch(db.models):
                    if other["kind"] == "meta" and other["strategy"] == row["name"] and other["status"] == "champion":
                        db.update_row(db.models, other["id"], status="retired")
                db.update_row(db.models, mid, status="champion")
            evals[row["name"]]["model"] = {"id": mid, "status": "champion" if good else "shadow (not better than chance)",
                                           **{k: m.metrics.get(k) for k in ("oos_auc", "hit_rate_top40pct", "hit_rate_all", "rows")}}
        except Exception as e:
            evals[row["name"]]["model"] = {"error": str(e)}
    out["steps"].append({"step": "evaluation", "strategies": evals})
    out["finished"] = datetime.now().isoformat(timespec="seconds")
    db.log("admin", "phase4_setup", {"symbols": len(data), "strategies": list(evals)})
    return out


def _train(origin: str, name: str, code: str, params: dict, data: dict):
    """Separate process: backtest the strategy on real data and fit the meta-label model."""
    import os
    from yonexis.sandbox.runner import parallel_workers
    os.environ["YONEXIS_PARALLEL"] = str(parallel_workers())      # fresh single-threaded process: safe to fork
    from yonexis.backtest import run_backtest
    from yonexis.ml import train_meta
    from yonexis.strategies import REGISTRY
    from yonexis.strategies.loader import load_code
    cls = REGISTRY[name] if origin == "builtin" and name in REGISTRY else load_code(code)
    return train_meta(run_backtest(cls, data, params), data)
