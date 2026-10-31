"""Live test for one trading day (today or any past date): rebuild that day's paper book from the real market.

For every selected stock/future and the selected strategy:
  * decisions (zone, armed levels) come from Upstox's own 5-minute candles of that day, exactly as the live engine
    makes them (built from 1-minute candles only if Upstox has no 5-minute ones);
  * execution follows that day's real 1-minute candles: entry the moment a level is touched, stop / target / 15:15
    square-off at the minute they happen (stop checked first inside a minute);
  * today only: trades that are still running stay OPEN and the live engine keeps managing them tick by tick.

That day's previous paper trades, their ledger lines and calls are replaced (one consistent book), and any voided
trades are removed. Nothing is sent to a broker.
"""
from __future__ import annotations

from datetime import datetime, time as dtime

import pandas as pd
from sqlalchemy import delete

from yonexis import db
from yonexis.config import settings


def _conn(engine):
    feed = getattr(engine, "feed", None) if engine else None
    if feed is not None:
        return feed.conn
    from yonexis.data.connectors.upstox import UpstoxConnector
    return UpstoxConnector()


_FETCH_ERR: dict[str, str] = {}


def _day_candles(engine, sym: str, day: pd.Timestamp, interval: str, is_today: bool) -> pd.DataFrame:
    """Upstox's own candles of one session at `interval` (1m or 5m): intraday API for today, history API for a past
    day, the database as fallback. Only completed candles."""
    frames = []
    try:
        c = _conn(engine)
        got = c.intraday(sym, interval) if is_today else c.fetch_history(sym, str(day.date()), str(day.date()), interval)
        frames.append(got)
        if not is_today and got is not None and not got.empty:
            try:                                   # keep it, so the same day replays later without Upstox
                db.save_candles(sym, got, interval, "upstox")
            except Exception:
                pass
    except Exception as e:
        _FETCH_ERR[sym] = str(e)[:160]
    if is_today and interval == "1m":
        feed = getattr(engine, "feed", None) if engine else None
        if feed is not None:
            frames.append(feed.session_1m(sym))
    frames.append(db.load_candles(sym, interval, str(day.date()), str(day.date()), real_only=True))
    frames = [f for f in frames if f is not None and not f.empty]
    if not frames:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    df = pd.concat(frames)
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)
    df = df[~df.index.duplicated(keep="first")].sort_index()          # Upstox candles win over tick-built / stored ones
    mins = int(interval.rstrip("m"))
    end = pd.Timestamp.now() if is_today else day + pd.Timedelta(hours=15, minutes=30)
    df = df[(df.index >= day + pd.Timedelta(hours=9, minutes=15)) & (df.index + pd.Timedelta(minutes=mins) <= end)]
    return df[["open", "high", "low", "close", "volume"]]


def _history_5m(engine, sym: str, day: pd.Timestamp, interval: str) -> pd.DataFrame:
    feed = getattr(engine, "feed", None) if engine else None
    h = feed.history.get(sym, pd.DataFrame()) if feed is not None else pd.DataFrame()
    if h is None or h.empty:
        h = db.load_candles(sym, interval, str((day - pd.Timedelta(days=20)).date()), str((day - pd.Timedelta(days=1)).date()),
                            real_only=True)
    return h[h.index < day] if not h.empty else h


def _why_no_trade(name: str, p: dict, df5: pd.DataFrame, one: pd.DataFrame, day: pd.Timestamp) -> str:
    """Plain reason a stock made no trade today (zone strategy); generic text for other strategies."""
    if name != "first_red_candle_zone":
        return "no setup today"
    d = df5[df5.index >= day]
    d = d[d.index.time >= dtime(9, 15)]
    red = d[d["close"] < d["open"]]
    if red.empty:
        return "no red 5-minute candle today, so no zone"
    z = red.iloc[0]
    zt = red.index[0]
    pct = (z["high"] - z["low"]) / z["close"] * 100
    zone = f"zone {z['low']:.2f}-{z['high']:.2f} at {zt:%H:%M}"
    if pct > p.get("max_zone_pct", 1.5):
        return f"{zone} is {pct:.2f}% wide, more than the {p.get('max_zone_pct', 1.5)}% limit (max_zone_pct)"
    if pct < p.get("min_zone_pct", 0.05):
        return f"{zone} is only {pct:.2f}% wide, less than the {p.get('min_zone_pct', 0.05)}% minimum (min_zone_pct)"
    cut = p.get("entry_cutoff", "14:30")
    after = one[(one.index >= zt + pd.Timedelta(minutes=5)) & (one.index.strftime("%H:%M") <= cut)]
    if after.empty:
        return f"{zone}: formed after the {cut} entry cut-off"
    return (f"{zone}: price stayed inside it until the {cut} cut-off "
            f"(range {after['low'].min():.2f}-{after['high'].max():.2f})")


def reset_today(day: pd.Timestamp) -> dict:
    """Remove today's paper trades (+ their ledger lines), every voided trade, and today's calls."""
    nxt = day + pd.Timedelta(days=1)
    trades = db.fetch(db.paper_trades)
    drop = [t["id"] for t in trades if (t["entry_ts"] and day <= pd.Timestamp(t["entry_ts"]) < nxt) or t["exit_reason"] == "void"
            or str(t["tag"] or "").startswith("backfill")]           # voided rows and the old what-if rows go too
    refs = [f"paper_trade:{i}" for i in drop]
    sig_ids = [s["id"] for s in db.fetch(db.signals, (db.signals.c.ts >= day.to_pydatetime()) & (db.signals.c.ts < nxt.to_pydatetime()))]
    with db.engine.begin() as cx:
        if refs:
            cx.execute(delete(db.wallet_ledger).where(db.wallet_ledger.c.ref.in_(refs)))
        if drop:
            cx.execute(delete(db.paper_trades).where(db.paper_trades.c.id.in_(drop)))
        if sig_ids:
            cx.execute(delete(db.feedback).where(db.feedback.c.signal_id.in_(sig_ids)))
            cx.execute(delete(db.signals).where(db.signals.c.id.in_(sig_ids)))
    # wallet balance = last reset + every remaining movement after it
    ledger = db.fetch(db.wallet_ledger, order=db.wallet_ledger.c.id)
    resets = [i for i, l in enumerate(ledger) if l["kind"] == "reset"]
    start = resets[-1] if resets else 0
    bal = round((ledger[start]["amount"] if ledger else settings.PAPER_STARTING_BALANCE) + sum(l["amount"] for l in ledger[start + 1:]), 2)
    if db.fetch(db.wallet, db.wallet.c.id == 1):
        db.update_row(db.wallet, 1, balance=bal)
    return {"removed_trades": len(drop), "removed_calls": len(sig_ids), "balance_after_reset": bal}


def rebuild(engine, progress=lambda *a: None, scope: dict | None = None, date: str | None = None) -> dict:
    """scope = {"symbols": [...], "strategy": name, "version": n}: rebuild only those stocks/futures with that one
    strategy; it is saved, so the live engine then trades exactly the same selection for the rest of the day."""
    from yonexis.backtest import BacktestConfig, run_backtest
    from yonexis.backtest.costs import CostModel
    from yonexis.broker.paper import Wallet, margin_needed
    from yonexis.data.connectors.upstox import _resample
    from yonexis.risk import risk
    from yonexis.ml.features import signal_features
    from yonexis.strategies.loader import latest_versions, load_code

    if engine is not None and engine.sim:
        raise RuntimeError("today's live test needs the real Upstox feed (LIVE_CONNECTOR=upstox)")
    now = pd.Timestamp.now()
    day = pd.Timestamp(date).normalize() if date else now.normalize()
    is_today = day == now.normalize()
    if day > now.normalize():
        raise RuntimeError("that date is in the future")
    if day.dayofweek >= 5:
        raise RuntimeError(f"{day:%a %d %b %Y} is a weekend - pick a trading day")
    if is_today and now.time() < dtime(9, 16):
        raise RuntimeError("the market has not traded yet today")
    label = "today" if is_today else f"{day:%d %b %Y}"
    interval = settings.LIVE_INTERVAL
    minutes = int(interval.rstrip("m"))
    symbols = list(engine.symbols) if engine else db.get_watchlist(settings.WATCHLIST)
    special = (scope or {}).get("strategy") if (scope or {}).get("strategy") in ("ai_pick", "pair_trading") else None
    if special == "ai_pick":
        from yonexis.strategies import REGISTRY
        strategies = [r for n, r in latest_versions().items() if n in REGISTRY and r["status"] != "retired"]
        if scope.get("symbols"):
            want = {x.upper() for x in scope["symbols"]}
            symbols = [x for x in symbols if x.upper() in want]
    elif special == "pair_trading":
        strategies = []
        want = {x.upper() for x in scope.get("symbols") or symbols}
        symbols = [x for x in symbols if x.upper() in want]
    elif scope and scope.get("strategy"):
        from yonexis.strategies.loader import scope_strategy
        row = scope_strategy(scope)
        if row is None:
            raise RuntimeError(f"unknown strategy {scope['strategy']}")
        strategies = [row]
        if scope.get("symbols"):
            want = {x.upper() for x in scope["symbols"]}
            symbols = [x for x in symbols if x.upper() in want]
        if not symbols:
            raise RuntimeError("select at least one stock or future from the watchlist")
    else:
        strategies = [r for r in latest_versions().values() if r["status"] in ("paper", "live_small", "live")]
    if not strategies and special != "pair_trading":
        raise RuntimeError("no strategy is in paper status (Strategies tab)")

    # ---------------------------------------------------------------- data first (nothing is changed if this fails)
    data, src5, five_only = {}, {}, []
    _FETCH_ERR.clear()
    for i, sym in enumerate(symbols):
        progress(f"loading {label}'s 5-minute (zone) and 1-minute (entries) candles {sym} ({i + 1}/{len(symbols)})",
                 {"done": i, "total": len(symbols)})
        one = _day_candles(engine, sym, day, "1m", is_today)
        five = _day_candles(engine, sym, day, interval, is_today)                    # Upstox's own 5-minute candles
        if one.empty and not five.empty:
            one = five.copy()                 # no 1-minute candles anywhere: entries/exits on the 5-minute candles
            five_only.append(sym)
        if one.empty:
            continue
        src5[sym] = "upstox"
        if five.empty:                                                                # fallback: built from the 1-minute ones
            five, src5[sym] = _resample(one, minutes), "built from 1m"
            five = five[five.index + pd.Timedelta(minutes=minutes) <= (now if is_today else day + pd.Timedelta(hours=15, minutes=30))]
        hist = _history_5m(engine, sym, day, interval)
        df = pd.concat([f for f in (hist, five) if not f.empty])
        data[sym] = (df[~df.index.duplicated(keep="last")].sort_index(), one)
    if not data:
        err = next(iter(_FETCH_ERR.values()), None)
        raise RuntimeError(f"no candles for {label} in Upstox or in stored data"
                           + (f" - Upstox said: {err}" if err else " (holiday? or log in to Upstox on the Data tab)"))

    # ---------------------------------------------------------------- replay every stock with the current logic
    cfg = BacktestConfig(capital_per_trade=min(risk.limits.capital_per_trade, risk.limits.max_capital_per_symbol),
                         slippage_bps=settings.PAPER_SLIPPAGE_BPS, costs=CostModel(), lots_per_trade=settings.FUT_LOTS_PER_TRADE)
    found, rows = [], {}
    for row in strategies:
        cls = load_code(row["code"])
        params = db.uj(row["params"], {}) or {}
        for sym, (df, one) in data.items():
            progress(f"replaying {sym} with {row['name']} v{row['version']}", {})
            res = run_backtest(cls, {sym: df}, {**params, "_square_off": db.square_off_time()}, cfg, exec_bars={sym: one})
            t = res.trades
            t = t[(pd.to_datetime(t["entry_ts"]) >= day) & (pd.to_datetime(t["entry_ts"]) < day + pd.Timedelta(days=1))] if not t.empty else t
            sig_tags = {(str(pd.Timestamp(sg["ts"])), sg["side"]): sg.get("tags") or {} for sg in res.signals}
            for tr in t.to_dict("records"):
                key = (str(pd.Timestamp(tr["signal_ts"])), tr["side"])
                tr["tags"] = {**(tr.get("tags") or {}), **sig_tags.get(key, sig_tags.get((str(pd.Timestamp(tr["entry_ts"])), tr["side"]), {}))}
                tr["_df"] = df
                found.append((row, sym, tr))
            armed = [p for p in res.pending if pd.Timestamp(p["armed_ts"]) >= day]
            rows[(row["name"], sym)] = {"symbol": sym, "strategy": row["name"], "trades": len(t),
                                        "armed": [f"{p['side']} at {p['level']:.2f}" for p in armed],
                                        "last_price": float(one["close"].iloc[-1]),
                                        "why": _why_no_trade(row["name"], {**getattr(cls, "params", {}), **params}, df, one, day)}

    pair_info = None
    if special == "pair_trading":
        found, pair_info = _pairs_for_day(data, day, cfg, progress)
        for sym in data:
            rows[("pair_trading", sym)] = {"symbol": sym, "strategy": "pair_trading", "trades": sum(1 for _, s_, _ in found if s_ == sym),
                                           "armed": [], "last_price": float(data[sym][1]["close"].iloc[-1]),
                                           "why": "not part of a qualifying pair, or the spread never stretched to the entry z-score"}
    ai_info = None
    if special == "ai_pick":
        _ENGINE["e"] = engine
        found, ai_info = _ai_pick(found, data, day, progress)
        picked = {(r["name"], s_) for r, s_, _ in found}
        names = {"first_red_candle_zone": "zone"}
        for k, v in rows.items():
            info = (ai_info.get("per_symbol") or {}).get(k[1])
            if k not in picked:
                v["trades"] = 0
                if info:
                    v["why"] = (f"AI: {info['n']} candidate call{'s' if info['n'] != 1 else ''} "
                                f"({', '.join(names.get(x, x.replace('_', ' ')) for x in info['strategies'][:4])}"
                                f"{'…' if len(info['strategies']) > 4 else ''}); best {names.get(info['best_ev'], info['best_ev'].replace('_', ' '))}: "
                                f"{info['p_at_ev']:.0%} win chance (needs {ai_info['min_p']:.0%}), expected {info['ev_max']:+.2f}R per trade "
                                + ("(needs +0.05R: it would lose money on average after costs)" if info['ev_max'] < 0.05 else ""))
                else:
                    v["why"] = "no strategy made a call on this stock that day"

    # ---------------------------------------------------------------- write one consistent book
    progress(f"writing {label}'s trades", {})
    reset = reset_today(day)
    if is_today and scope and scope.get("strategy"):
        from yonexis.strategies.loader import SCOPE_KEY
        db.set_setting(SCOPE_KEY, {"symbols": [x.upper() for x in symbols], "strategy": special or strategies[0]["name"],
                                   "version": 1 if special else strategies[0]["version"]})
    if is_today:
        risk.state.open_positions.clear()
        risk.state.realized_pnl = 0.0
        risk.state.day = datetime.now().strftime("%Y-%m-%d")
    w = Wallet()
    found.sort(key=lambda x: pd.Timestamp(x[2]["entry_ts"]))
    open_now: dict[str, dict] = {}
    held: list[tuple] = []                 # (margin, exit_ts or None, net_pnl)
    skipped_chain: set = set()             # a skipped trade's later relay legs are skipped too
    summary = []
    for row, sym, tr in found:
        et = pd.Timestamp(tr["entry_ts"])
        chain = (row["name"], sym, (tr.get("tags") or {}).get("oco"))
        if chain in skipped_chain:
            continue
        # positions limit at the moment of entry (positions that closed earlier free their slot)
        live_slots = [s for s, xt in open_now.items() if xt is None or xt > et]
        still_open = is_today and tr["exit_reason"] == "end_of_data"
        need = margin_needed(sym, tr["qty"], tr["entry"])
        cash = reset["balance_after_reset"] + sum(n for m, x, n in held if x is not None and x <= et)
        blocked = sum(m for m, x, n in held if x is None or x > et)
        if sym in live_slots:              # one position per stock, like the live risk engine
            summary.append({"symbol": sym, "result": f"skipped {row['name']}: already holding {sym} at {et:%H:%M}"})
            skipped_chain.add(chain)
            continue
        if len(live_slots) >= risk.limits.max_open_positions:
            summary.append({"symbol": sym, "result": f"skipped: {risk.limits.max_open_positions} positions already open at {et:%H:%M}"})
            skipped_chain.add(chain)
            continue
        if need > cash - blocked:
            summary.append({"symbol": sym, "result": f"skipped: not enough demo money at {et:%H:%M} (needs Rs {need:,.0f}, "
                                                     f"available Rs {cash - blocked:,.0f})"})
            skipped_chain.add(chain)
            continue
        held.append((need, None if still_open else pd.Timestamp(tr["exit_ts"]), 0.0 if still_open else float(tr["net_pnl"])))
        tags = {**(tr.get("tags") or {})}
        try:
            feats = signal_features(tr["_df"], str(et), tr["side"], tags)
        except Exception:
            feats = {}
        sid = db.insert_row(db.signals, source="live", ts=et.to_pydatetime(), symbol=sym, strategy=row["name"],
                            strategy_version=row["version"], side=tr["side"], price=float(tr["entry"]), stop_loss=tr["stop_loss"],
                            target=tr["target"], reason=tr["reason"], tags=db.j(tags), status="taken" if still_open else "closed",
                            outcome_pnl=None if still_open else float(tr["net_pnl"]), outcome=None if still_open else tr["exit_reason"],
                            features=db.j(feats))
        margin = need
        common = dict(signal_id=sid, strategy=row["name"], symbol=sym, side=tr["side"], qty=int(tr["qty"]), margin=margin,
                      ref_price=float(tr["entry"]), entry_price=round(float(tr["entry"]), 2), entry_ts=et.to_pydatetime(),
                      stop_loss=float(tr["stop_loss"]), target=float(tr["target"]) if tr["target"] else None,
                      tag=f"{row['name']}-{sid}")
        if still_open:
            ltp = (engine.quote(sym, strict=True) or {}).get("ltp") if engine else None
            ltp = float(ltp or rows[(row["name"], sym)]["last_price"])
            tv = tr.get("trail")
            db.insert_row(db.paper_trades, status="open", last_price=ltp, **{**common, "stop_loss": float(tr.get("final_stop") or tr["stop_loss"])},
                          init_stop=float(tr["stop_loss"]), trail=float(tv) if tv == tv and tv else None,
                          max_minutes=int(tr["max_minutes"]) if tr.get("max_minutes") == tr.get("max_minutes") and tr.get("max_minutes") else None,
                          best=float(ltp))
            risk.state.open_positions[sym] = {"qty": int(tr["qty"]), "value": tr["qty"] * tr["entry"], "side": tr["side"], "strategy": row["name"]}
            open_now[sym] = None
            mtm = round((ltp - tr["entry"]) * tr["qty"] * (1 if tr["side"] == "BUY" else -1), 2)
            summary.append({"symbol": sym, "strategy": row["name"], "side": tr["side"], "entry_time": f"{et:%H:%M}", "entry": round(tr["entry"], 2),
                            "result": "open", "ltp": ltp, "mtm": mtm, "stop_loss": tr["stop_loss"], "target": tr["target"]})
        else:
            xt = pd.Timestamp(tr["exit_ts"])
            reason = {"sl": "stop_loss", "sl_gap": "stop_loss", "target_gap": "target"}.get(tr["exit_reason"], tr["exit_reason"])
            reason = reason[:24]
            tid = db.insert_row(db.paper_trades, status="closed", exit_price=round(float(tr["exit"]), 2), exit_ts=xt.to_pydatetime(),
                                exit_reason=reason, gross_pnl=float(tr["gross_pnl"]), costs=float(tr["costs"]),
                                net_pnl=float(tr["net_pnl"]), last_price=float(tr["exit"]), **common)
            w.book(float(tr["net_pnl"]), tid, f"{tr['side']} {int(tr['qty'])} {sym} {reason}", ts=xt.to_pydatetime())
            if is_today:
                risk.state.realized_pnl += float(tr["net_pnl"])
            open_now[sym] = xt
            summary.append({"symbol": sym, "strategy": row["name"], "side": tr["side"], "entry_time": f"{et:%H:%M}", "entry": round(tr["entry"], 2),
                            "result": reason, "exit_time": f"{xt:%H:%M}", "exit": round(tr["exit"], 2), "net_pnl": tr["net_pnl"]})
    for sym in symbols:
        if sym not in data:
            summary.append({"symbol": sym, "result": f"no trade: Upstox returned no 1-minute candles for {label}"})
    traded = {x["symbol"] for x in summary if "entry" in x}
    idle: dict[str, list] = {}
    for (name, sym), r in rows.items():
        if not r["trades"] and sym not in traded:
            idle.setdefault(sym, []).append((name, r))
    for sym, lst in idle.items():
        waits = [f"{n.split('_')[0]}: " + " / ".join(r["armed"]) for n, r in lst if r["armed"]]
        summary.append({"symbol": sym, "result": ("waiting - " + "; ".join(waits)) if waits else "no trade: " + lst[0][1]["why"],
                        "ltp": lst[0][1]["last_price"]})

    if engine is not None and is_today:
        engine.reload_today()
    out = {"at": datetime.now().isoformat(timespec="seconds"), "date": str(day.date()), "is_today": is_today,
           "zone_candles": ("Upstox 5-minute" if all(v == "upstox" for v in src5.values()) else
                            f"Upstox 5-minute ({sum(v != 'upstox' for v in src5.values())} built from 1-minute)"),
           "entries_note": (f"{len(five_only)} instruments had no 1-minute candles, so entries/exits used their 5-minute candles: "
                            + ", ".join(five_only[:8]) + ("…" if len(five_only) > 8 else "")) if five_only else None,
           "reset": reset, "stocks": len(data),
           "strategy": {"ai_pick": "AI pick", "pair_trading": "pair trading"}.get(special) or ", ".join(f"{r['name']} v{r['version']}" for r in strategies),
           "ai": ai_info, "pairs": pair_info,
           "trades": sum(1 for s in summary if "entry" in s), "open": sum(1 for s in summary if s.get("result") == "open"),
           "realised": round(sum(s.get("net_pnl", 0) for s in summary), 2), "open_mtm": round(sum(s.get("mtm", 0) for s in summary), 2),
           "rows": sorted(summary, key=lambda s: (s.get("entry_time") or "99", s["symbol"]))}
    db.log("admin", "today_rebuild", {k: out[k] for k in ("stocks", "trades", "open", "realised")})
    return out


def _pairs_for_day(data: dict, day: pd.Timestamp, cfg, progress):
    """Pair trading for one day: pairs discovered on the history BEFORE that day, traded on that day's 5-min closes."""
    from yonexis import pairs as pm
    hist = {s: df[df.index < day] for s, (df, _) in data.items()}
    found_pairs = pm.discover_pairs(hist) or [{"a": a, "b": b} for a, b in pm.DEFAULT_PAIRS if a in data and b in data]
    progress(f"pairs: {', '.join(p['a'] + '/' + p['b'] for p in found_pairs) or 'none'}", {})
    row = {"name": "pair_trading", "version": 1}
    out = []
    for p in found_pairs:
        legs = pm.backtest_pair(p["a"], p["b"], data[p["a"]][0], data[p["b"]][0], None, cfg)
        for tr in legs:
            if day <= pd.Timestamp(tr["entry_ts"]) < day + pd.Timedelta(days=1):
                tr["_df"] = data[tr["symbol"]][0]
                tr["trail"], tr["max_minutes"], tr["final_stop"] = None, None, None
                tr["stop_loss"] = tr["entry"] * (0.9 if tr["side"] == "BUY" else 1.1)     # leg safety stop; pair exits on z
                out.append((row, tr["symbol"], tr))
    return out, found_pairs


_ENGINE: dict = {}


def _ai_pick(found: list, data: dict, day: pd.Timestamp, progress):
    """Score every strategy's candidate trade of the day with the AI picker and keep the best per stock."""
    from yonexis.lab import champion_selector
    from yonexis.ml import selector
    model, mrow = champion_selector()
    if model is None:
        raise RuntimeError("no AI picker trained yet - run the Strategy Lab (Backtest tab) first")
    progress(f"AI picker v{mrow['version']} ({mrow['status']}) scoring {len(found)} candidate trades", {})
    if not found:
        return [], {"model": mrow["version"], "status": mrow["status"], "candidates": 0, "picked": 0}
    ff = {s: selector.feature_frame(df) for s, (df, _) in data.items()}
    # market context exactly as in training: the NIFTY index (not whatever happens to be selected)
    conn = getattr(getattr(_ENGINE.get("e"), "feed", None), "conn", None)
    idx = selector.index_5m(day, conn)
    if idx.empty:
        idx = data.get("NIFTY-FUT", (pd.DataFrame(),))[0]
    mkt = selector.market_frame({"NIFTY": idx}) if idx is not None and not idx.empty else None
    cand = []
    for k, (row, sym, tr) in enumerate(found):
        from yonexis.strategies import REGISTRY
        r = selector.rows_for_trades(pd.DataFrame([tr]), row["name"], getattr(REGISTRY.get(row["name"]), "family", "custom"), ff, mkt)
        if not r.empty:
            r["_k"] = k
            cand.append(r)
    if not cand:
        return [], {"model": mrow["version"], "status": mrow["status"], "candidates": 0, "picked": 0}
    sc = selector.score(model, pd.concat(cand, ignore_index=True))
    need = selector.min_p_for(model)
    chosen = selector.pick(sc, min_p=need)
    per_sym = {}
    for sym_, g in sc.groupby("_symbol"):
        b = g.sort_values("p", ascending=False).iloc[0]
        bev = g.sort_values("ev", ascending=False).iloc[0]
        per_sym[sym_] = {"n": int(len(g)), "best": str(b["_strategy"]), "p": round(float(b["p"]), 3),
                         "ev": round(float(b["ev"]), 3), "strategies": sorted(g["_strategy"].unique().tolist()),
                         "best_ev": str(bev["_strategy"]), "ev_max": round(float(bev["ev"]), 3), "p_at_ev": round(float(bev["p"]), 3)}
    keep = []
    for _, r in chosen.iterrows():
        row, sym, tr = found[int(r["_k"])]
        tr["tags"] = {**(tr.get("tags") or {}), "ai_p": round(float(r["p"]), 3), "ai_ev": round(float(r["ev"]), 3)}
        tr["reason"] = f"AI pick ({row['name']}, P(win) {r['p']:.0%}): {tr['reason']}"
        keep.append((row, sym, tr))
    return keep, {"model": mrow["version"], "status": mrow["status"], "candidates": int(len(sc)), "picked": len(keep),
                  "min_p": need, "per_symbol": per_sym}
