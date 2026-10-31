"""Deterministic event-driven backtester.

Fill rules (conservative by design):
  * A signal on candle i is filled at the OPEN of candle i+1 (same day), with slippage.
  * Stop loss and target are checked against each candle's high/low.
    If both are touched inside one candle, the STOP is assumed to fill first.
  * Gaps through a level fill at the candle open, not the level.
  * Open positions are squared off at the strategy's square-off time.
Every number in the platform comes from here, never from an LLM.
"""
from __future__ import annotations

import math
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from yonexis.sdk import BUY, Candle, Context, Signal, Strategy, as_arrays

from .costs import CostModel
from .metrics import compute_metrics

TRADE_COLUMNS = [
    "symbol", "side", "signal_ts", "entry_ts", "entry", "exit_ts", "exit", "qty",
    "stop_loss", "target", "exit_reason", "gross_pnl", "costs", "net_pnl", "ret_pct", "reason", "tags",
    "trail", "max_minutes", "final_stop",
]


@dataclass
class BacktestConfig:
    capital_per_trade: float = 100_000.0
    slippage_bps: float = 2.0            # 1 bps = 0.01%
    fill: str = "next_open"              # "next_open" or "close"
    costs: CostModel = field(default_factory=CostModel)
    starting_capital: float = 1_000_000.0
    lot_sizes: dict = field(default_factory=dict)   # futures: symbol -> lot size (filled in automatically)
    lots_per_trade: int = 1                          # futures are traded in whole lots


def is_future(symbol: str) -> bool:
    return str(symbol).upper().endswith("-FUT")


def with_lot_sizes(cfg: "BacktestConfig", symbols) -> "BacktestConfig":
    """Fill cfg.lot_sizes for futures symbols from the instrument list (needs the instrument file; not in the sandbox)."""
    need = [s for s in symbols if is_future(s) and s not in (cfg.lot_sizes or {})]
    if not need:
        return cfg
    from dataclasses import replace
    lots = dict(cfg.lot_sizes or {})
    try:
        from yonexis.data.connectors.upstox import instruments
        for s in need:
            lots[s] = instruments.lot_size(s)
    except Exception:
        pass
    return replace(cfg, lot_sizes=lots)


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    daily: pd.DataFrame
    metrics: dict[str, Any]
    signals: list[dict]
    pending: list[dict] = field(default_factory=list)   # stop entries still resting at the end of the data (live: armed orders)

    def to_dict(self, include_trades: bool = True) -> dict:
        out = {"metrics": self.metrics, "n_signals": len(self.signals)}
        if include_trades:
            t = self.trades.copy()
            for col in ("signal_ts", "entry_ts", "exit_ts"):
                if col in t:
                    t[col] = t[col].astype(str)
            out["trades"] = t.to_dict(orient="records")
            d = self.daily.reset_index()
            d["date"] = d["date"].astype(str)
            out["daily"] = d.to_dict(orient="records")
        return out


def _slip(price: float, side: str, bps: float, entering: bool) -> float:
    """Move price against us."""
    adverse_up = (side == BUY) == entering   # buying -> pay more; selling -> receive less
    k = bps / 10_000
    return price * (1 + k) if adverse_up else price * (1 - k)


def normalize_candles(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df.columns = [c.lower() for c in df.columns]
    if "ts" in df.columns:
        df = df.set_index("ts")
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    if "volume" not in df:
        df["volume"] = 0.0
    return df[["open", "high", "low", "close", "volume"]].astype(float)


# ------------------------------------------------------------------ multi-core
# Symbols are independent, so a backtest over many stocks is split across CPU cores. Only enabled where it is safe
# to fork (single-threaded worker processes: the sandbox child, the model-training process), via YONEXIS_PARALLEL=N.
_FORK_JOB: tuple | None = None
PARALLEL_MIN_CANDLES = 60_000


def _workers() -> int:
    import os
    try:
        n = int(os.getenv("YONEXIS_PARALLEL", "0"))
    except ValueError:
        n = 0
    return max(0, min(n, os.cpu_count() or 1))


def _fork_part(symbols: list[str]):
    strategy_cls, data, params, cfg, exec_bars = _FORK_JOB
    return _run_symbols(strategy_cls, {s: data[s] for s in symbols}, params, cfg,
                        {s: exec_bars[s] for s in symbols if s in exec_bars} if exec_bars else None)


def _touched(sg: Signal, hi: float, lo: float) -> bool:
    """Has a resting order been reached inside a bar with this high/low?"""
    if sg.entry == "limit":
        return lo <= sg.entry_price if sg.side == BUY else hi >= sg.entry_price
    return hi >= sg.entry_price if sg.side == BUY else lo <= sg.entry_price


def _fill_px(sg: Signal, bar_open: float) -> float:
    """Fill at the level, or at the open when the bar gapped through it (always the price you would really get)."""
    if sg.entry == "limit":
        return min(bar_open, sg.entry_price) if sg.side == BUY else max(bar_open, sg.entry_price)
    return max(bar_open, sg.entry_price) if sg.side == BUY else min(bar_open, sg.entry_price)


def run_backtest(
    strategy_cls: type[Strategy],
    data: dict[str, pd.DataFrame],
    params: dict | None = None,
    config: BacktestConfig | None = None,
    exec_bars: dict[str, pd.DataFrame] | None = None,
) -> BacktestResult:
    """exec_bars: optional finer candles (e.g. 1-minute) per symbol. Strategy decisions still happen on `data`
    candles; entries, stops, targets and square-off are then simulated minute by minute inside each candle."""
    global _FORK_JOB
    cfg = with_lot_sizes(config or BacktestConfig(), list(data))
    n = _workers()
    if n > 1 and len(data) > 1 and sum(len(v) for v in data.values()) >= PARALLEL_MIN_CANDLES:
        import multiprocessing as mp
        syms = sorted(data, key=lambda s: -len(data[s]))
        k = min(n, len(syms))
        parts = [syms[i::k] for i in range(k)]                          # largest first, round-robin = balanced
        _FORK_JOB = (strategy_cls, data, params, cfg, exec_bars)
        try:
            with mp.get_context("fork").Pool(k) as pool:
                outs = pool.map(_fork_part, parts)
        finally:
            _FORK_JOB = None
        order = {s: i for i, s in enumerate(data)}
        trades = sorted((t for o in outs for t in o[0]), key=lambda t: (order[t["symbol"]], str(t["entry_ts"])))
        signals = sorted((x for o in outs for x in o[1]), key=lambda x: (order[x["symbol"]], x["ts"]))
        pend = [x for o in outs for x in o[2]]
    else:
        trades, signals, pend = _run_symbols(strategy_cls, data, params, cfg, exec_bars)
    tdf = pd.DataFrame(trades, columns=TRADE_COLUMNS)
    daily = daily_pnl(tdf, cfg.starting_capital)
    return BacktestResult(tdf, daily, compute_metrics(tdf, daily), signals, pend)


def _in_windows(ts, windows) -> bool:
    t = pd.Timestamp(ts).strftime("%H:%M")
    return any(a <= t < b for a, b in windows)


def _run_symbols(strategy_cls, data, params, cfg, exec_bars):
    trades: list[dict] = []
    signals: list[dict] = []
    pend: list[dict] = []
    params = dict(params or {})
    # tuning filters (Strategy Lab "tuned settings"): only these stocks, only entries inside these time windows
    only_syms = {x.upper() for x in (params.pop("_symbols", None) or [])}
    windows = [tuple(w) for w in (params.pop("_windows", None) or [])]
    sq_off = params.pop("_square_off", None)          # admin's square-off time (default: the strategy's 15:15)
    if sq_off:
        from datetime import time as _time
        sq_off = _time(*map(int, str(sq_off).split(":")))

    for symbol, raw in data.items():
        if only_syms and symbol.upper() not in only_syms:
            continue
        df = normalize_candles(raw)
        if df.empty:
            continue
        strat = strategy_cls(**params)
        if sq_off:
            strat.square_off = sq_off
        try:
            ind_arrays = as_arrays(strat.indicators(df))          # computed once per symbol, read via ctx.ind
        except Exception as e:                                     # a broken indicator must not kill the whole run
            raise RuntimeError(f"{strategy_cls.__name__}.indicators failed on {symbol}: {e}") from e
        fut = is_future(symbol)
        lot = (cfg.lot_sizes or {}).get(symbol, 1)
        fut_costs = cfg.costs.for_futures()
        state: dict = {}
        xb = normalize_candles(exec_bars[symbol]) if exec_bars and symbol in exec_bars and not exec_bars[symbol].empty else None
        if xb is not None:
            x_idx = xb.index.values.astype("datetime64[ns]").astype("int64")
            xo, xh, xl, xc = (xb[k].to_numpy() for k in ("open", "high", "low", "close"))
            step = (df.index.to_series().diff().dropna().mode().iloc[0]) if len(df) > 1 else pd.Timedelta(minutes=5)
        pos: dict | None = None
        pending: tuple[Signal, pd.Timestamp] | None = None
        stops: list[tuple[Signal, pd.Timestamp]] = []
        intrabar_fill = False
        idx = df.index
        o, h, lo, c, v = (df[k].to_numpy() for k in ("open", "high", "low", "close", "volume"))
        prev_day = None

        def close_pos(exit_px: float, ts, why: str) -> None:
            nonlocal pos
            p = pos
            d = 1 if p["side"] == BUY else -1
            gross = (exit_px - p["entry"]) * p["qty"] * d
            buy_v = p["entry"] * p["qty"] if d == 1 else exit_px * p["qty"]
            sell_v = exit_px * p["qty"] if d == 1 else p["entry"] * p["qty"]
            cost = (fut_costs if fut else cfg.costs).round_trip(buy_v, sell_v)
            trades.append({
                **{k: p[k] for k in ("symbol", "side", "signal_ts", "entry_ts", "entry", "qty", "stop_loss", "target", "reason", "tags",
                                     "trail", "max_minutes")}, "final_stop": p["stop"],
                "exit_ts": ts, "exit": round(exit_px, 4), "exit_reason": why,
                "gross_pnl": round(gross, 2), "costs": round(cost, 2), "net_pnl": round(gross - cost, 2),
                "ret_pct": round((gross - cost) / (p["entry"] * p["qty"]) * 100, 4),
            })
            pos = None

        def open_pos(sig: Signal, sig_ts, px: float, ts) -> bool:
            nonlocal pos
            if windows and not _in_windows(ts, windows):
                return False
            fill = _slip(px, sig.side, cfg.slippage_bps, entering=True)
            qty = int(cfg.lots_per_trade) * int(lot) if fut else math.floor(cfg.capital_per_trade / fill)
            if qty <= 0:
                return False
            # Reject fills that already breach the stop (gap through SL).
            if (sig.side == BUY and fill <= sig.stop_loss) or (sig.side != BUY and fill >= sig.stop_loss):
                return False
            pos = {"symbol": symbol, "side": sig.side, "signal_ts": sig_ts, "entry_ts": ts, "entry": round(fill, 4),
                   "qty": qty, "stop_loss": sig.stop_loss, "target": sig.target, "reason": sig.reason, "tags": sig.tags,
                   "stop": sig.stop_loss, "trail": sig.trail, "max_minutes": sig.max_minutes, "best": fill}
            return True

        for i in range(len(df)):
            ts = idx[i]
            day = ts.date()
            if day != prev_day:
                # Safety: never carry an intraday position overnight.
                if pos is not None:
                    close_pos(_slip(c[i - 1], pos["side"], cfg.slippage_bps, False), idx[i - 1], "eod")
                pending = None
                stops = []
                strat.on_new_day(state)
                prev_day = day
            intrabar_fill = False

            # Execution happens on sub-bars when finer data (e.g. 1-minute) is supplied, else on the candle itself.
            if xb is not None:
                j0, j1 = np.searchsorted(x_idx, ts.value, "left"), np.searchsorted(x_idx, (ts + step).value, "left")
                subs = [(pd.Timestamp(x_idx[j]), xo[j], xh[j], xl[j], xc[j]) for j in range(j0, j1)] or [(ts, o[i], h[i], lo[i], c[i])]
            else:
                subs = [(ts, o[i], h[i], lo[i], c[i])]
            for k_sub, (bts, bo, bh, bl, bc) in enumerate(subs):
                intrabar_fill = False
                # 1) market entry decided on the previous candle: fill at this candle's first price
                if pending is not None and pos is None and k_sub == 0:
                    sig, sig_ts = pending
                    pending = None
                    if bts.time() < strat.square_off:
                        open_pos(sig, sig_ts, bo, bts)

                # 1b) resting stop entries: fill the moment price touches the level (gap through it -> open)
                if pos is None and stops:
                    t_now = bts.time()
                    stops = [(sg, at) for sg, at in stops if t_now < strat.square_off and
                             (sg.valid_until is None or t_now <= datetime.strptime(sg.valid_until, "%H:%M").time())]
                    hit = [(sg, at) for sg, at in stops if _touched(sg, bh, bl)]
                    if hit:
                        sg, at = min(hit, key=lambda x: abs(bo - x[0].entry_price))     # nearest level to the open goes first
                        px = _fill_px(sg, bo)
                        sig_tags = {**sg.tags, "trigger": "touch", "level": sg.entry_price, "oco": sg.oco}
                        filled = open_pos(Signal(sg.side, sg.stop_loss, sg.target, sg.reason, sig_tags), bts, px, bts)
                        stops = [(x, a) for x, a in stops if x is not sg and (sg.oco is None or x.oco != sg.oco)]
                        if filled:
                            intrabar_fill = True
                            strat.on_entry(state, sg)
                            signals.append({"symbol": symbol, "ts": str(bts), "side": sg.side, "close": float(px),
                                            "stop_loss": sg.stop_loss, "target": sg.target, "reason": sg.reason, "tags": sig_tags,
                                            "trail": sg.trail, "max_minutes": sg.max_minutes})

                # 2) exits inside this bar (a stop may hand the position over to the opposite side: relay)
                stopped = None
                if pos is not None and pos["max_minutes"] and bts >= pd.Timestamp(pos["entry_ts"]) + pd.Timedelta(minutes=pos["max_minutes"]):
                    close_pos(_slip(bo, pos["side"], cfg.slippage_bps, False), bts, "time_exit")
                if pos is not None:
                    stopped = dict(pos)
                if pos is not None and intrabar_fill:
                    # Filled mid-bar: the order of later prices is unknown, so only the stop is checked (pessimistic).
                    if pos["side"] == BUY and bl <= pos["stop"]:
                        close_pos(_slip(pos["stop"], BUY, cfg.slippage_bps, False), bts, "sl")
                    elif pos["side"] != BUY and bh >= pos["stop"]:
                        close_pos(_slip(pos["stop"], "SELL", cfg.slippage_bps, False), bts, "sl")
                elif pos is not None:
                    sl, tgt = pos["stop"], pos["target"]
                    if pos["side"] == BUY:
                        if bo <= sl:
                            close_pos(_slip(bo, BUY, cfg.slippage_bps, False), bts, "sl_gap")
                        elif bl <= sl:
                            close_pos(_slip(sl, BUY, cfg.slippage_bps, False), bts, "sl")
                        elif tgt is not None and bo >= tgt:
                            close_pos(bo, bts, "target_gap")
                        elif tgt is not None and bh >= tgt:
                            close_pos(tgt, bts, "target")
                    else:
                        if bo >= sl:
                            close_pos(_slip(bo, "SELL", cfg.slippage_bps, False), bts, "sl_gap")
                        elif bh >= sl:
                            close_pos(_slip(sl, "SELL", cfg.slippage_bps, False), bts, "sl")
                        elif tgt is not None and bo <= tgt:
                            close_pos(bo, bts, "target_gap")
                        elif tgt is not None and bl <= tgt:
                            close_pos(tgt, bts, "target")

                # trailing stop: tighten AFTER this bar's exits were checked (the bar's own extreme can't save it)
                if pos is not None and pos["trail"]:
                    if pos["side"] == BUY:
                        pos["best"] = max(pos["best"], bh)
                        pos["stop"] = max(pos["stop"], pos["best"] - pos["trail"])
                    else:
                        pos["best"] = min(pos["best"], bl)
                        pos["stop"] = min(pos["stop"], pos["best"] + pos["trail"])
                if trades and pos is None and stopped is not None and trades[-1]["exit_reason"] in ("sl", "sl_gap") \
                        and stopped.get("trail") and abs(stopped["stop"] - stopped["stop_loss"]) > 1e-9:
                    trades[-1]["exit_reason"] = "trail_stop"
                if stopped is not None and pos is None and trades and trades[-1]["exit_reason"] in ("sl", "sl_gap") \
                        and bts.time() < strat.square_off:
                    rv = strat.reversal(stopped.get("tags") or {}, stopped["side"], bts)
                    if rv is not None:
                        lvl = rv.entry_price if rv.entry_price is not None else stopped["stop_loss"]
                        px = max(bo, lvl) if rv.side == BUY else min(bo, lvl)       # a gap through the level fills at the open
                        rtags = {**rv.tags, "trigger": "reversal", "level": lvl, "from_side": stopped["side"]}
                        if open_pos(Signal(rv.side, rv.stop_loss, rv.target, rv.reason, rtags), bts, px, bts):
                            trades[-1]["exit_reason"] = "reversed"
                            signals.append({"symbol": symbol, "ts": str(bts), "side": rv.side, "close": float(px),
                                            "stop_loss": rv.stop_loss, "target": rv.target, "reason": rv.reason, "tags": rtags})

                # 3) square off (sub-bars: at the first price at/after the square-off time, like the live engine)
                if pos is not None and xb is not None and bts.time() >= strat.square_off:
                    close_pos(_slip(bo, pos["side"], cfg.slippage_bps, False), bts, "square_off")

            if pos is not None and xb is None and ts.time() >= strat.square_off:
                close_pos(_slip(c[i], pos["side"], cfg.slippage_bps, False), ts, "square_off")

            # 4) strategy decision on the closed candle
            ctx = Context(symbol, df, i, state, in_position=pos is not None or pending is not None, has_pending=bool(stops),
                          indicators=ind_arrays)
            out = strat.on_candle(ctx, Candle(ts, o[i], h[i], lo[i], c[i], v[i]))
            outs = out if isinstance(out, (list, tuple)) else ([out] if out is not None else [])
            sig = None
            for x in outs:
                if x.entry in ("stop", "limit"):
                    if x.replace_pending:
                        stops = []
                    if x.entry_price is not None and x.entry_price <= 0:
                        continue                         # pure "cancel my resting orders"
                    if ts.time() < strat.square_off:
                        stops.append((x, ts))
                elif sig is None:
                    sig = x
            if sig is not None and pos is None and pending is None and ts.time() < strat.square_off:
                signals.append({"symbol": symbol, "ts": str(ts), "side": sig.side, "close": c[i],
                                "stop_loss": sig.stop_loss, "target": sig.target, "reason": sig.reason, "tags": sig.tags,
                                "trail": sig.trail, "max_minutes": sig.max_minutes})
                strat.on_entry(state, sig)
                if cfg.fill == "close":
                    open_pos(sig, ts, c[i], ts)
                else:
                    pending = (sig, ts)

        if pos is not None:
            close_pos(_slip(c[-1], pos["side"], cfg.slippage_bps, False), idx[-1], "end_of_data")
        for sg, at in stops:
            pend.append({"symbol": symbol, "armed_ts": str(at), "side": sg.side, "level": sg.entry_price, "stop_loss": sg.stop_loss,
                         "target": sg.target, "reason": sg.reason, "tags": sg.tags, "oco": sg.oco, "valid_until": sg.valid_until,
                         "entry": sg.entry, "trail": sg.trail, "max_minutes": sg.max_minutes})

    return trades, signals, pend


def daily_pnl(trades: pd.DataFrame, starting_capital: float) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["net_pnl", "equity", "ret"], index=pd.Index([], name="date"))
    t = trades.copy()
    t["date"] = pd.to_datetime(t["exit_ts"]).dt.normalize()
    d = t.groupby("date")["net_pnl"].sum().to_frame()
    d["equity"] = starting_capital + d["net_pnl"].cumsum()
    d["ret"] = d["net_pnl"] / d["equity"].shift(1).fillna(starting_capital)
    return d
