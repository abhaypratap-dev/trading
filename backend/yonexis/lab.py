"""Strategy Lab: backtest EVERY strategy on ALL stored history, then train the AI strategy picker.

Steps (one background job, live log):
  1. load real 5-minute history for every watchlist stock/future (+ NIFTY index for market context);
  2. backtest each built-in strategy (zone, Hari Master Block, the 12-strategy intraday library) in a separate
     process using all CPU cores; pair trading on the auto-discovered correlated pairs;
  3. leaderboard: metrics per strategy, best strategy per stock, results by market regime and time of day;
  4. AI picker: features at decision time for every candidate trade (+ your closed paper trades, weighted x3 so
     it learns from its own live mistakes), purged time-ordered CV, out-of-sample comparison of "AI pick" vs
     "take every signal" vs "best single strategy"; the model becomes champion only if it beats the baseline.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import datetime

import numpy as np
import pandas as pd
from sqlalchemy import delete

from yonexis import db
from yonexis.config import settings

AI_PICK = "ai_pick"


def _worker(names: list[str], data: dict, cfg_kwargs: dict) -> dict:
    """Spawned process: run the named built-in strategies (multi-core inside run_backtest)."""
    from yonexis.backtest import BacktestConfig, run_backtest
    from yonexis.sandbox.runner import parallel_workers
    from yonexis.strategies import REGISTRY
    os.environ["YONEXIS_PARALLEL"] = str(parallel_workers())
    out = {}
    for n in names:
        t0 = time.time()
        try:
            r = run_backtest(REGISTRY[n], data, {}, BacktestConfig(**cfg_kwargs))
            out[n] = {"trades": r.trades, "metrics": r.metrics, "secs": round(time.time() - t0, 1),
                      "family": getattr(REGISTRY[n], "family", "custom")}
        except Exception as e:
            out[n] = {"error": f"{type(e).__name__}: {e}"}
    return out


def _load(symbols: list[str], start: str, progress) -> dict:
    data = {}
    for i, s in enumerate(symbols):
        progress(f"1/4 loading 5-minute history {s} ({i + 1}/{len(symbols)})", {"done": i, "total": len(symbols)})
        df = db.load_candles(s, "5m", start, None, real_only=True)
        if len(df) >= 750:                    # ~10 sessions minimum
            data[s] = df
    return data


def _index_history(start: str, progress) -> pd.DataFrame:
    df = db.load_candles("NIFTY", "5m", start, None, real_only=True)
    if len(df) > 1000:
        return df
    try:
        from yonexis.data.connectors import upstox as ux
        if ux.tokens.token:
            progress("1/4 downloading NIFTY index history for market context", {})
            df = ux.UpstoxConnector().fetch_history("NIFTY", start, str(pd.Timestamp.now().date()), "5m")
            if not df.empty:
                db.save_candles("NIFTY", df[df.index < pd.Timestamp.now().normalize()], "5m", "upstox")
    except Exception:
        pass
    return df


def _paper_rows(strategies: set[str]) -> pd.DataFrame:
    """Closed paper trades (the live results) in the engine's trade format."""
    rows = []
    sigs = {s["id"]: s for s in db.fetch(db.signals)}
    for t in db.fetch(db.paper_trades, db.paper_trades.c.status == "closed"):
        if t["exit_reason"] == "void" or t["strategy"] not in strategies or t["net_pnl"] is None:
            continue
        tags = db.uj((sigs.get(t["signal_id"]) or {}).get("tags"), {}) or {}
        rows.append({"symbol": t["symbol"], "side": t["side"], "signal_ts": t["entry_ts"], "entry_ts": t["entry_ts"],
                     "exit_ts": t["exit_ts"], "entry": t["entry_price"], "exit": t["exit_price"], "qty": t["qty"],
                     "stop_loss": t.get("init_stop") or t["stop_loss"], "net_pnl": t["net_pnl"], "tags": tags,
                     "strategy": t["strategy"]})
    return pd.DataFrame(rows)


def run(engine, progress, start: str = "2024-01-01", include_pairs: bool = True) -> dict:
    import multiprocessing as mp
    from concurrent.futures import ProcessPoolExecutor, TimeoutError as FTimeout

    from yonexis import pairs as pairs_mod
    from yonexis.backtest import BacktestConfig
    from yonexis.backtest.metrics import compute_metrics
    from yonexis.backtest.engine import daily_pnl
    from yonexis.ml import selector
    from yonexis.risk import risk
    from yonexis.strategies import REGISTRY

    t_start = time.time()
    run_id = datetime.now().strftime("%Y%m%d%H%M") + uuid.uuid4().hex[:4]
    symbols = list(engine.symbols) if engine else db.get_watchlist(settings.WATCHLIST)
    data = _load(symbols, start, progress)
    if not data:
        raise RuntimeError("no stored 5-minute history - run Phase 4 setup or Data > download history first")
    idx = _index_history(start, progress)
    n_c = sum(len(v) for v in data.values())
    progress(f"1/4 {len(data)} instruments, {n_c:,} candles from {start}"
             f"{' + NIFTY index' if len(idx) else ' (no NIFTY index history: market features empty)'}", {})

    # 2 ------------------------------------------------------------------ backtest every strategy
    cfg_kwargs = {"capital_per_trade": float(min(risk.limits.capital_per_trade, risk.limits.max_capital_per_symbol)),
                  "slippage_bps": float(settings.PAPER_SLIPPAGE_BPS)}
    names = list(REGISTRY)
    results = {}
    with ProcessPoolExecutor(max_workers=1, mp_context=mp.get_context("spawn")) as ex:
        for i, n in enumerate(names):
            progress(f"2/4 backtesting {n} ({i + 1}/{len(names)}) on {len(data)} instruments", {"done": i, "total": len(names)})
            fut = ex.submit(_worker, [n], data, cfg_kwargs)
            t0 = time.time()
            while True:
                try:
                    results.update(fut.result(timeout=15))
                    break
                except FTimeout:
                    if time.time() - t0 > 3600:
                        raise RuntimeError(f"{n} took longer than 1 hour")
                    progress(f"2/4 still backtesting {n} ({time.time() - t0:.0f}s)", {"done": i, "total": len(names)})
            r = results[n]
            if "error" in r:
                progress(f"    {n}: ERROR {r['error']}", {})
            else:
                m = r["metrics"]
                progress(f"    {n}: {m.get('trades', 0)} trades, win {m.get('win_rate')}%, net Rs {m.get('net_pnl', 0):,.0f}, "
                         f"PF {m.get('profit_factor')} ({r['secs']}s)", {})
    if include_pairs:
        progress("2/4 pair trading: discovering correlated, mean-reverting pairs", {})
        found = pairs_mod.discover_pairs(data)
        progress(f"    pairs: {', '.join(p['a'] + '/' + p['b'] + f' (corr {p['corr']})' for p in found) or 'none qualified'}", {})
        pr = pairs_mod.backtest(data, cfg=BacktestConfig(**cfg_kwargs), pairs=[(p["a"], p["b"]) for p in found])
        results[pairs_mod.NAME] = {"trades": pr["trades"], "metrics": pr["metrics"], "family": "pairs", "pairs": found}
        m = pr["metrics"]
        progress(f"    pair_trading: {m.get('trades', 0)} legs, net Rs {m.get('net_pnl', 0):,.0f}", {})

    # 3 ------------------------------------------------------------------ leaderboard + store trades
    progress("3/4 leaderboard and storing trades", {})
    board = []
    with db.engine.begin() as cx:
        cx.execute(delete(db.lab_trades))
    lab_rows = []
    for n, r in results.items():
        if "error" in r:
            board.append({"strategy": n, "error": r["error"]})
            continue
        t, m = r["trades"], r["metrics"]
        board.append({"strategy": n, "family": r.get("family"), **{k: m.get(k) for k in (
            "trades", "win_rate", "net_pnl", "profit_factor", "expectancy", "sharpe", "max_drawdown", "avg_win", "avg_loss")}})
        for x in (t.to_dict("records") if not t.empty else []):
            tags = x.get("tags") if isinstance(x.get("tags"), dict) else {}
            risk_rs = (tags.get("risk") or 0) * x["qty"]
            lab_rows.append({"run_id": run_id, "strategy": n, "family": r.get("family"), "symbol": x["symbol"], "side": x["side"],
                             "signal_ts": pd.Timestamp(x["signal_ts"]).to_pydatetime(), "entry_ts": pd.Timestamp(x["entry_ts"]).to_pydatetime(),
                             "exit_ts": pd.Timestamp(x["exit_ts"]).to_pydatetime(), "entry": float(x["entry"]), "exit": float(x["exit"]),
                             "exit_reason": str(x["exit_reason"])[:24], "net_pnl": float(x["net_pnl"]), "ret_pct": float(x["ret_pct"]),
                             "r_mult": float(x["net_pnl"]) / risk_rs if risk_rs else None})
    for k in range(0, len(lab_rows), 5000):
        with db.engine.begin() as cx:
            cx.execute(db.lab_trades.insert(), lab_rows[k:k + 5000])
    board.sort(key=lambda b: -(b.get("net_pnl") or -1e18))

    # per-stock best strategy
    best_by_symbol = {}
    if lab_rows:
        lt = pd.DataFrame(lab_rows)
        g = lt.groupby(["symbol", "strategy"])["net_pnl"].agg(["sum", "count"]).reset_index()
        for sym, gg in g.groupby("symbol"):
            b = gg.sort_values("sum", ascending=False).iloc[0]
            best_by_symbol[sym] = {"strategy": b["strategy"], "net_pnl": round(float(b["sum"]), 2), "trades": int(b["count"])}

    # 4 ------------------------------------------------------------------ features + AI picker
    progress("4/4 building decision-time features for every candidate trade", {})
    ff = {s: selector.feature_frame(df) for s, df in data.items()}
    mkt = selector.market_frame({"NIFTY": idx}) if len(idx) else None
    parts = []
    for n, r in results.items():
        if "error" in r or r["trades"].empty or n == pairs_mod.NAME:
            continue
        parts.append(selector.rows_for_trades(r["trades"], n, r.get("family", "custom"), ff, mkt))
    ds = pd.concat([p for p in parts if not p.empty], ignore_index=True) if parts else pd.DataFrame()
    paper = _paper_rows(set(results))
    weights = None
    if not paper.empty:
        prow = []
        for n, g in paper.groupby("strategy"):
            prow.append(selector.rows_for_trades(g, n, getattr(REGISTRY.get(n), "family", "custom"), ff, mkt))
        pdf = pd.concat([p for p in prow if not p.empty], ignore_index=True) if prow else pd.DataFrame()
        if not pdf.empty:
            pdf["_paper"] = 1
            ds = pd.concat([ds.assign(_paper=0), pdf], ignore_index=True)
            weights = pd.Series(np.where(ds["_paper"] == 1, 3.0, 1.0), index=ds.index)
            progress(f"    + {len(pdf)} of your closed paper trades (weighted x3: learn from live mistakes)", {})

    regimes = {}
    if not ds.empty:
        d = ds.assign(regime=np.select([ds["adx"] >= 25, ds["adx"] < 18], ["trending", "ranging"], "normal"),
                      tod=pd.cut(ds["minutes_from_open"], [-1, 45, 120, 240, 400], labels=["open-10:00", "10:00-11:15", "11:15-13:15", "after 13:15"]),
                      gap=np.where(ds["gap_pct"].abs() >= 1, "gap day", "normal day"))
        for col in ("regime", "tod", "gap"):
            t = d.groupby([col, "_strategy"], observed=True)["_net_pnl"].agg(["sum", "count", "mean"]).reset_index()
            regimes[col] = [{"bucket": str(r[col]), "strategy": r["_strategy"], "net_pnl": round(float(r["sum"]), 2),
                             "trades": int(r["count"]), "avg": round(float(r["mean"]), 2)} for _, r in t.iterrows()]

    model_info = None
    if len(ds) >= 200:
        progress(f"4/4 training the AI picker on {len(ds):,} candidate trades (purged time-ordered CV)", {})
        tr = selector.train(ds, weights)
        m, rep = tr["model"], tr["report"]
        m.meta.update({"run_id": run_id, "symbols": sorted(data), "start": start})
        path = m.save(settings.models_dir)
        status = "champion" if rep["beats_baseline"] else "shadow"
        if status == "champion":
            for other in db.fetch(db.models):
                if other["kind"] == "selector" and other["status"] == "champion":
                    db.update_row(db.models, other["id"], status="retired")
        mid = db.insert_row(db.models, kind="selector", version=m.version, strategy=AI_PICK, path=str(path),
                            metrics=db.j(m.metrics), status=status)
        model_info = {"id": mid, "status": status, **{k: v for k, v in rep.items() if k != "strategy_stats"}}
        progress(f"    AI picker: AUC {rep['oos_auc']} - out of sample: AI pick {rep['oos_ai_pick_trades']} trades "
                 f"Rs {rep['oos_ai_pick_pnl']:,.0f} (avg {rep['oos_ai_avg_per_trade']:,.0f}/trade) vs every signal "
                 f"Rs {rep['oos_all_strategies_pnl']:,.0f} (avg {rep['oos_all_avg_per_trade']:,.0f}/trade) -> {status}", {})
    else:
        progress(f"    only {len(ds)} candidate trades - need 200+ to train the AI picker", {})

    out = {"run_id": run_id, "finished": datetime.now().isoformat(timespec="seconds"), "secs": round(time.time() - t_start, 1),
           "start": start, "instruments": sorted(data), "candles": n_c, "leaderboard": board, "best_by_symbol": best_by_symbol,
           "regimes": regimes, "pairs": results.get(pairs_mod.NAME, {}).get("pairs", []), "selector": model_info}
    db.set_setting("lab_last", {k: out[k] for k in ("run_id", "finished", "start", "instruments", "candles", "leaderboard",
                                                    "best_by_symbol", "pairs", "selector")})
    db.log("admin", "strategy_lab", {"run_id": run_id, "strategies": len(board), "candidates": int(len(ds))})
    return out


def champion_selector():
    from yonexis.ml.models import TrainedModel
    rows = [r for r in db.fetch(db.models) if r["kind"] == "selector" and r["status"] in ("champion", "shadow")]
    if not rows:
        return None, None
    rows.sort(key=lambda r: (r["status"] == "champion", r["id"]))
    r = rows[-1]
    try:
        return TrainedModel.load(r["path"]), r
    except Exception:
        return None, r
