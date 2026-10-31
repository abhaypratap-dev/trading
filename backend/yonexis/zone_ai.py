"""YONEXIS AI backtest: search the zone strategy's configurations and the best futures to trade with it.

The strategy itself is never changed (yonexis.zone_lab.run_day). Only its settings are combined:
  * 2 lots per entry: lot 1 target from `lot1` (1.5, 2, 2.5 ...), lot 2 target from `lot2` (20, 18, 16 ...);
    every pair with lot 1 <= lot 2 (a pair is the same trade whichever lot is called "1");
  * target multiplier base (Zone / IZ), spike on / off, max entries per day.
Every configuration is replayed on every futures symbol and day. Trick that keeps this fast: the entries and stops
of a day do not depend on the smaller target (the entry lasts until the biggest target, the stop or square-off), so
one replay with legs [all lot-1 targets ..., lot-2 target] gives the result of every lot-1 choice at once.

Then the symbol sets: for N = 5, 10 ... 40 futures, two answers are reported:
  * "honest pick": configuration and symbols chosen ONLY on the first part of the period (train, default 70%),
    then checked on the last part, which played no part in the choice (test). This is the number to trust.
  * "maximum profit": chosen on the whole period - the best that was possible in hindsight (over-fitted by nature).
"""
from __future__ import annotations

import multiprocessing as mp
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from itertools import product

import numpy as np
import pandas as pd

from yonexis import db

DEFAULT_GRID = {"lot1": [1.5, 2, 2.5, 3, 3.5, 4, 4.5, 5], "lot2": [20, 18, 16, 14, 12, 10, 8, 6],
                "entries": [1, 2], "bases": ["zone", "iz"], "spikes": [True, False],
                "sizes": [5, 10, 15, 20, 25, 30, 35, 40], "train_share": 0.7}
MAX_CONFIGS = 3000


def build_configs(g: dict) -> list[tuple]:
    out = []
    for base, spike, cyc in product(g["bases"], g["spikes"], g["entries"]):
        for b in sorted(set(g["lot2"]), reverse=True):
            for a in sorted(set(g["lot1"])):
                if a <= b:
                    out.append((base, bool(spike), int(cyc), float(a), float(b)))
    return out


def cfg_dict(c: tuple) -> dict:
    base, spike, cyc, a, b = c
    return {"target_base": base, "spike": spike, "max_cycles": cyc, "lot_targets": [a, b], "target_mult": a}


def _symbol_job(args):
    """Replay one symbol for every configuration. Returns per-configuration daily net P&L."""
    from yonexis import datacheck
    from yonexis.zone_lab import DEFAULTS, prepare_day, run_day
    sym, lot, df, base_p, grid, cfgs, refined = args
    df.index = pd.to_datetime(df.index)
    days, preps, skipped = [], [], 0
    for d, g in df.groupby(df.index.normalize()):
        if refined and datacheck.day_problem(g, "5m"):
            skipped += 1
            continue
        days.append(str(d.date()))
        preps.append(prepare_day(g))
    index = {c: i for i, c in enumerate(cfgs)}
    nets = np.zeros((len(cfgs), len(days)), dtype=np.float32)
    trades = np.zeros(len(cfgs), dtype=np.int32)
    wins = np.zeros(len(cfgs), dtype=np.int32)
    entries = np.zeros(len(cfgs), dtype=np.int32)
    for base, spike, cyc in product(grid["bases"], grid["spikes"], grid["entries"]):
        for b in sorted(set(grid["lot2"]), reverse=True):
            la = [float(a) for a in sorted(set(grid["lot1"])) if a <= b]
            if not la:
                continue
            mults = la + [float(b)]
            p = {**DEFAULTS, **base_p, "target_base": base, "spike": bool(spike), "max_cycles": int(cyc), "lot_targets": mults, "_lean": True}
            cis = [index[(base, bool(spike), int(cyc), a, float(b))] for a in la]
            for di, prep in enumerate(preps):
                r = run_day(sym, None, p, lot, prep)
                if not r["trades"]:
                    continue
                acc = [0.0] * len(mults)
                cnt = [0] * len(mults)
                win = [0] * len(mults)
                for t in r["trades"]:
                    if t["exit_reason"] == "open":
                        continue
                    k = t["lot"] - 1
                    acc[k] += t["net_pnl"]
                    cnt[k] += 1
                    win[k] += t["net_pnl"] > 0
                for j, ci in enumerate(cis):
                    nets[ci, di] += acc[j] + acc[-1]
                    trades[ci] += cnt[j] + cnt[-1]
                    wins[ci] += win[j] + win[-1]
                    entries[ci] += cnt[-1]
    return {"symbol": sym, "lot": lot, "last_px": float(df["close"].iloc[-1]), "days": days, "nets": nets, "trades": trades, "wins": wins, "entries": entries,
            "skipped": skipped}


def futures_with_data(start: str, end: str) -> list[str]:
    from sqlalchemy import text
    with db.engine.connect() as cx:
        rows = cx.execute(text("SELECT DISTINCT symbol FROM candles WHERE interval = '5m' AND symbol LIKE '%-FUT' "
                               "AND source != 'simulated' AND ts >= :a AND ts < :b"),
                          {"a": pd.Timestamp(start).to_pydatetime(), "b": (pd.Timestamp(end) + pd.Timedelta(days=1)).to_pydatetime()}).all()
    return sorted(r[0] for r in rows)


def _dd(series: np.ndarray) -> float:
    if not len(series):
        return 0.0
    eq = np.cumsum(series)
    return float((eq - np.maximum.accumulate(np.maximum(eq, 0))).min())


def run(symbols: list[str] | None, start: str, end: str, params: dict, grid: dict | None = None, progress=lambda *a: None,
        workers: int | None = None) -> dict:
    from yonexis.data.connectors.upstox import instruments
    from yonexis.zone_lab import config_label
    g = {**DEFAULT_GRID, **(grid or {})}
    cfgs = build_configs(g)
    if not cfgs:
        raise ValueError("no configuration to test: every lot-1 target is above every lot-2 target")
    if len(cfgs) > MAX_CONFIGS:
        raise ValueError(f"{len(cfgs)} configurations is too many (max {MAX_CONFIGS}) - use fewer targets")
    syms = [s.upper() for s in (symbols or []) if s.upper().endswith("-FUT")] or futures_with_data(start, end)
    if not syms:
        raise ValueError("no futures with stored 5-minute candles in this period - build continuous futures history first (Data tab)")
    refined = bool(params.get("refined"))
    base_p = {k: v for k, v in params.items() if k not in ("refined",)}
    progress(f"{len(cfgs)} configurations x {len(syms)} futures: loading candles", {"done": 0, "total": len(syms)})
    jobs = []
    for s in syms:
        df = db.load_candles(s, "5m", start, end, real_only=True)
        if df is None or df.empty:
            continue
        df = df[~df.index.duplicated(keep="last")]
        df = df[pd.to_datetime(df.index).normalize() < pd.Timestamp.now().normalize()]     # finished sessions only
        if df.empty:
            continue
        try:
            lot = instruments.lot_size(s)
        except Exception:
            lot = 1
        jobs.append((s, lot, df, base_p, g, cfgs, refined))
    if not jobs:
        raise ValueError("no stored 5-minute candles for these futures in this period")
    n = workers if workers is not None else max(1, min(12, (os.cpu_count() or 2) - 1))
    res = []
    if n > 1 and len(jobs) > 1:
        try:
            with ProcessPoolExecutor(max_workers=min(n, len(jobs)), mp_context=mp.get_context("spawn")) as ex:
                futs = [ex.submit(_symbol_job, j) for j in jobs]
                for k, f in enumerate(as_completed(futs)):
                    r = f.result()
                    res.append(r)
                    progress(f"replayed {r['symbol']} with {len(cfgs)} configurations ({k + 1}/{len(jobs)}, {n} cores)",
                             {"done": k + 1, "total": len(jobs)})
        except Exception as e:                             # pool not available: one core
            progress(f"parallel run failed ({e}); continuing on one core", {})
            res = []
    if not res:
        for k, j in enumerate(jobs):
            res.append(_symbol_job(j))
            progress(f"replayed {j[0]} with {len(cfgs)} configurations ({k + 1}/{len(jobs)})", {"done": k + 1, "total": len(jobs)})
    progress("finding the best configurations and symbol sets", {})
    return analyse(res, cfgs, g, start, end, refined, config_label)


def analyse(res: list[dict], cfgs: list[tuple], g: dict, start: str, end: str, refined: bool, config_label) -> dict:
    res = sorted(res, key=lambda r: r["symbol"])
    syms = [r["symbol"] for r in res]
    all_days = sorted({d for r in res for d in r["days"]})
    if not all_days:
        raise ValueError("no usable days (refined data may have removed all of them)")
    di = {d: i for i, d in enumerate(all_days)}
    split = all_days[min(len(all_days) - 1, max(1, int(len(all_days) * float(g["train_share"]))))]
    train_mask = np.array([d < split for d in all_days])
    S, C, D = len(res), len(cfgs), len(all_days)
    M = np.zeros((S, C, D), dtype=np.float32)
    T = np.zeros((S, C), dtype=np.int64)
    W = np.zeros((S, C), dtype=np.int64)
    E = np.zeros((S, C), dtype=np.int64)
    for s, r in enumerate(res):
        cols = [di[d] for d in r["days"]]
        if cols:
            M[s][:, cols] = r["nets"]
        T[s], W[s], E[s] = r["trades"], r["wins"], r["entries"]
    TR, TE, FU = M[:, :, train_mask].sum(2), M[:, :, ~train_mask].sum(2), M.sum(2)

    from yonexis.config import settings
    try:
        m = db.get_setting("paper_margin") or {}
    except Exception:
        m = {}
    mpct = float(m.get("fut_margin_pct") or settings.FUT_MARGIN_PCT)
    # money for one entry of a future: 2 lots x lot size x latest price (contract value); the margin blocked is
    # mpct % of it. Entries of one future never overlap, but every future of a set can be open at the same time.
    cval = np.array([2 * r["lot"] * r.get("last_px", 0.0) for r in res])
    marg = cval * mpct / 100

    def pack(c: int, idx: np.ndarray, how: str) -> dict:
        daily = M[idx, c, :].sum(0)
        inv = float(marg[idx].sum())
        te = daily[~train_mask]
        tr_days = int(train_mask.sum())
        return {"how": how, "config": cfg_dict(cfgs[c]), "label": config_label({**cfg_dict(cfgs[c])}),
                "symbols": [syms[i] for i in idx], "train_net": round(float(TR[idx, c].sum()), 2),
                "test_net": round(float(TE[idx, c].sum()), 2), "full_net": round(float(FU[idx, c].sum()), 2),
                "trades": int(T[idx, c].sum()), "entries": int(E[idx, c].sum()),
                "win_rate": round(float(W[idx, c].sum() / max(1, T[idx, c].sum()) * 100), 1),
                "green_days": int((daily > 0).sum()), "days": int((daily != 0).sum()),
                "test_green_days": int((te > 0).sum()), "test_days": int((te != 0).sum()),
                "investment": round(inv, 2), "contract_value": round(float(cval[idx].sum()), 2), "margin_pct": mpct,
                "return_pct": round(float(FU[idx, c].sum()) / inv * 100, 2) if inv else None,
                "test_return_pct": round(float(TE[idx, c].sum()) / inv * 100, 2) if inv else None,
                "max_drawdown": round(_dd(daily), 2), "train_days": tr_days, "test_days_total": int(D - tr_days),
                "per_symbol": [{"symbol": syms[i], "train": round(float(TR[i, c]), 2), "test": round(float(TE[i, c]), 2),
                                "full": round(float(FU[i, c]), 2), "trades": int(T[i, c]),
                                "lot": int(res[i]["lot"]), "price": round(float(res[i].get("last_px", 0.0)), 2),
                                "investment": round(float(marg[i]), 2)} for i in idx]}

    sets = []
    for n in sorted({int(x) for x in g["sizes"] if int(x) > 0}):
        if n > S:
            continue
        o_tr = np.argsort(-TR, axis=0)[:n]                            # per configuration: best n symbols on train
        c_h = int(np.argmax(np.take_along_axis(TR, o_tr, 0).sum(0)))
        o_fu = np.argsort(-FU, axis=0)[:n]
        c_m = int(np.argmax(np.take_along_axis(FU, o_fu, 0).sum(0)))
        sets.append({"n": n, "honest": pack(c_h, o_tr[:, c_h], "train"), "max": pack(c_m, o_fu[:, c_m], "full")})
    tot_tr, tot_te, tot_fu = TR.sum(0), TE.sum(0), FU.sum(0)
    top = [{"config": cfg_dict(cfgs[c]), "label": config_label(cfg_dict(cfgs[c])), "train_net": round(float(tot_tr[c]), 2),
            "test_net": round(float(tot_te[c]), 2), "full_net": round(float(tot_fu[c]), 2), "trades": int(T[:, c].sum()),
            "win_rate": round(float(W[:, c].sum() / max(1, T[:, c].sum()) * 100), 1),
            "profitable_symbols": int((FU[:, c] > 0).sum())} for c in np.argsort(-tot_fu)[:15]]
    both = (TR > 0) & (TE > 0)
    sym_rows = []
    for s in range(S):
        c = int(np.argmax(FU[s]))
        sym_rows.append({"symbol": syms[s], "best": config_label(cfg_dict(cfgs[c])), "best_full": round(float(FU[s, c]), 2),
                         "best_train": round(float(TR[s, c]), 2), "best_test": round(float(TE[s, c]), 2),
                         "consistency": round(float(both[s].mean() * 100), 1), "days": len(res[s]["days"]),
                         "skipped_days": res[s]["skipped"]})
    sym_rows.sort(key=lambda r: -r["best_full"])
    return {"start": start, "end": end, "split": split, "train_share": g["train_share"], "grid": g, "configs_tested": C,
            "symbols_tested": S, "days": D, "refined": refined, "skipped_days": int(sum(r["skipped"] for r in res)),
            "sets": sets, "top_configs": top, "margin_pct": mpct, "symbols": sym_rows}
