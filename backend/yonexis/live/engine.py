"""Live loop: candles -> active strategies -> AI score -> risk -> broker -> dashboard (WebSocket).

With LIVE_CONNECTOR=simulated it replays synthetic NSE sessions candle by candle so the
whole system can be exercised when the market is closed.
"""
from __future__ import annotations

import asyncio
import json
import time
import logging
import traceback
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from yonexis import db
from yonexis.backtest import BacktestConfig, run_backtest
from yonexis.backtest.engine import normalize_candles
from yonexis.broker import PaperBroker, get_broker
from yonexis.config import settings
from yonexis.data.connectors import get_connector
from yonexis.data.connectors.simulated import session_index, simulate
from yonexis.ml import TrainedModel, combine, signal_features
from yonexis.risk import risk
from yonexis.strategies import loader

CONFIRM_DELAY_S = 2       # ask Upstox for the official candle this long after the close
CONFIRM_WAIT_S = 20       # decide on the tick/exchange-feed candle if the official one is not there by then
log = logging.getLogger("yonexis.live")


def zone_status(df: pd.DataFrame) -> dict:
    """First-red-candle zone status for the dashboard table."""
    if df.empty:
        return {}
    day = df.index[-1].normalize()
    today = df[df.index >= day]
    last = df.iloc[-1]
    prev_close = df[df.index < day]["close"].iloc[-1] if (df.index < day).any() else today["open"].iloc[0]
    out = {"price": round(float(last["close"]), 2), "ts": str(df.index[-1]),
           "change_pct": round(float((last["close"] / prev_close - 1) * 100), 2),
           "open": float(today["open"].iloc[0]), "high": float(today["high"].max()), "low": float(today["low"].min())}
    red = today[today["close"] < today["open"]]
    if red.empty:
        out["status"] = "WAITING"
        return out
    z = red.iloc[0]
    out["zone_low"], out["zone_high"], out["zone_ts"] = float(z["low"]), float(z["high"]), str(red.index[0])
    out["zone_size"] = round(float(z["high"] - z["low"]), 2)
    out["zone_pct"] = round(float((z["high"] - z["low"]) / z["close"] * 100), 2)
    c = last["close"]
    out["status"] = "ABOVE" if c > z["high"] else "BELOW" if c < z["low"] else "IN ZONE"
    return out


class SimClock:
    """Reveals synthetic candles one by one."""

    def __init__(self, symbols: list[str]):
        today = pd.Timestamp.now().normalize()
        start = (today - pd.tseries.offsets.BDay(25)).strftime("%Y-%m-%d")
        end = (today + pd.tseries.offsets.BDay(120)).strftime("%Y-%m-%d")
        self.full = {s: simulate(s, start, end, seed=i + 11) for i, s in enumerate(symbols)}
        self.days = sorted({d for df in self.full.values() for d in df.index.normalize().unique()})
        first = next((i for i, d in enumerate(self.days) if d >= today), len(self.days) - 1)
        self.day_i, self.k = first, 1
        self.per_day = len(session_index(self.days[0]))

    @property
    def current_day(self) -> pd.Timestamp:
        return self.days[self.day_i]

    def advance(self) -> bool:
        """Reveal one more candle. Returns True when a new day starts."""
        self.k += 1
        if self.k > self.per_day:
            self.day_i = min(self.day_i + 1, len(self.days) - 1)
            self.k = 1
            return True
        return False

    def visible(self, symbol: str) -> pd.DataFrame:
        df = self.full[symbol]
        day = self.current_day
        past = df[df.index < day]
        today = df[(df.index >= day) & (df.index < day + pd.Timedelta(days=1))].iloc[: self.k]
        return pd.concat([past.tail(self.per_day * 15), today])


class LiveEngine:
    def __init__(self):
        self.connector_name = settings.LIVE_CONNECTOR
        if self.connector_name == "upstox" and not settings.UPSTOX_API_KEY:
            log.warning("LIVE_CONNECTOR=upstox but UPSTOX_API_KEY is empty: using the simulated feed")
            self.connector_name = "simulated"
        try:
            self.symbols = db.get_watchlist(settings.WATCHLIST)
        except Exception:
            self.symbols = list(settings.WATCHLIST)
        self.status: dict[str, dict] = {}
        self.emitted: set[tuple] = set()
        self.subscribers: set[asyncio.Queue] = set()
        self.broker = get_broker()
        self.sim = SimClock(self.symbols) if self.connector_name == "simulated" else None
        self.connector = None if self.sim else get_connector(self.connector_name)
        self.feed = None
        self._last_closed: dict[str, pd.Timestamp] = {}
        self._feed_day = None
        self._last_reconcile = 0.0
        if self.connector_name == "upstox":
            from yonexis.data.connectors.upstox import UpstoxFeed
            self.feed = UpstoxFeed(self.connector, self.symbols, settings.UPSTOX_FEED_MODE)
        self.armed: dict[tuple, tuple] = {}     # (strategy, symbol) -> (row, [resting stop entries])
        self.triggered: set[tuple] = set()      # touch entries already fired today
        # Do not re-emit today's signals after a restart.
        self.reload_today()
        self._models: dict[str, tuple[str, TrainedModel]] = {}
        self.running = False
        self.last_error: str | None = None
        self.cache: dict[str, pd.DataFrame] = {}
        if self.sim:
            risk.ignore_clock = True

    # ------------------------------------------------------------ data
    def candles(self, symbol: str) -> pd.DataFrame:
        if self.sim:
            return self.sim.visible(symbol)
        if self.feed is not None:
            df = self.feed.candles(symbol, settings.LIVE_INTERVAL)
            b = getattr(self, "_boundary", None)
            if not df.empty and b is not None and df.index[-1] + self._step_td() == b and not self.feed.is_confirmed(symbol, b) \
                    and pd.Timestamp.now() < b + pd.Timedelta(seconds=CONFIRM_WAIT_S):
                df = df.iloc[:-1]            # wait (max CONFIRM_WAIT_S) for the exchange's official candle before deciding
            if df.empty:      # not logged in yet today / restarted: show the last stored session
                start = str((pd.Timestamp.now() - pd.Timedelta(days=20)).date())
                df = db.load_candles(symbol, settings.LIVE_INTERVAL, start, real_only=True)
                df = normalize_candles(df) if not df.empty else df
            return df
        df = self.connector.latest_candles(symbol, settings.LIVE_INTERVAL, lookback_days=15)
        df = normalize_candles(df) if not df.empty else df
        if not df.empty:   # drop the still-forming candle
            minutes = int(settings.LIVE_INTERVAL.rstrip("m")) if settings.LIVE_INTERVAL.endswith("m") else 5
            if df.index[-1] + pd.Timedelta(minutes=minutes) > pd.Timestamp.now(tz="Asia/Kolkata").tz_localize(None):
                df = df.iloc[:-1]
        return df

    def ensure_feed(self) -> None:
        """Start the Upstox feed once a token exists; backfill history once per trading day."""
        if self.feed is None:
            return
        from yonexis.data.connectors.upstox import _trading_day, tokens
        if not tokens.token:
            return
        day = _trading_day()
        if self._feed_day != day:
            self._feed_day = day
            self.feed.backfill(days=15, interval=settings.LIVE_INTERVAL)
            self.feed.stop()
            self.feed.start()
            self._last_reconcile = time.time()
            self.persist(history=True)
        elif time.time() - self._last_reconcile > 900:      # candle closes are confirmed separately (confirm_candles)
            self._last_reconcile = time.time()
            self.feed.reconcile()
            self.persist()

    def persist(self, history: bool = False) -> None:
        """Save feed candles to the database so past prices survive restarts and expired logins."""
        for s in list(self.symbols):
            try:
                if history and not self.feed.history.get(s, pd.DataFrame()).empty:
                    db.save_candles(s, self.feed.history[s], settings.LIVE_INTERVAL, "upstox")
                bars = self.feed.session_1m(s)
                if not bars.empty:
                    db.save_candles(s, bars, "1m", "upstox_live")
                    from yonexis.data.connectors.upstox import _resample
                    db.save_candles(s, _resample(bars, int(settings.LIVE_INTERVAL.rstrip("m"))), settings.LIVE_INTERVAL, "upstox_live")
            except Exception as e:
                log.warning("persist %s failed: %s", s, e)

    def _step_td(self) -> pd.Timedelta:
        return pd.Timedelta(minutes=int(settings.LIVE_INTERVAL.rstrip("m")) if settings.LIVE_INTERVAL.endswith("m") else 5)

    def confirm_candles(self) -> None:
        """At every candle close, fetch Upstox's official 1-minute candles (parallel) and retry until they include
        the just-closed minute; decisions on that candle wait for it (at most CONFIRM_WAIT_S)."""
        if self.sim or self.feed is None or self.feed.status != "connected":
            return
        now = pd.Timestamp.now()
        td = self._step_td()
        day0 = now.normalize() + pd.Timedelta(hours=9, minutes=15)
        if not (day0 + td <= now <= now.normalize() + pd.Timedelta(hours=15, minutes=31)):
            return
        b = day0 + ((now - day0) // td) * td
        self._boundary = b
        if getattr(self, "_confirmed_b", None) == b or now < b + pd.Timedelta(seconds=CONFIRM_DELAY_S):
            return
        if time.time() - getattr(self, "_confirm_try", 0) < 4:        # stay well inside Upstox's rate limits
            return
        self._confirm_try = time.time()
        done = self.feed.confirm(b, list(self.symbols))
        if len(done) >= len(self.symbols) or now >= b + pd.Timedelta(seconds=CONFIRM_WAIT_S):
            self._confirmed_b = b

    def flush_orderflow(self) -> int:
        from yonexis.data.orderflow import flow
        rows = flow.drain()
        n = 0
        for r in rows:
            try:
                with db.engine.begin() as cx:
                    cx.execute(db.orderflow.insert().values(
                        ts=r["ts"].to_pydatetime(), symbol=r["symbol"], bid_qty=r.get("bid_qty"), ask_qty=r.get("ask_qty"),
                        imbalance=r.get("imbalance"), tbq=r.get("tbq"), tsq=r.get("tsq"), buy_vol=r["buy_vol"],
                        sell_vol=r["sell_vol"], delta=r["delta"], big_buys=r["big_buys"], big_sells=r["big_sells"],
                        spread_bps=r.get("spread_bps")))
                n += 1
            except Exception:
                pass
        return n

    def reload_today(self) -> None:
        """(Re)load what already happened today from the DB: calls made and touch levels already fired."""
        try:
            today0 = pd.Timestamp.now().normalize().to_pydatetime()
            emitted, triggered = set(), set()
            for r in db.fetch(db.signals, (db.signals.c.ts >= today0) & db.real_signals()):
                emitted.add((r["strategy"], r["symbol"], str(pd.Timestamp(r["ts"]))))
                tg = db.uj(r["tags"], {}) or {}
                if tg.get("trigger") == "touch":
                    triggered.add((r["strategy"], r["symbol"], tg.get("oco") or r["side"]))
            self.emitted, self.triggered = emitted, triggered
            self.armed.clear()
            self._last_closed.clear()       # recompute zones/levels on the next step
        except Exception:
            pass

    def check_triggers(self) -> list[dict]:
        """Live: fire resting stop entries the moment the live price touches their level (no waiting for a candle close)."""
        if self.sim or self.feed is None or self.feed.status != "connected":
            return []
        events = []
        now = pd.Timestamp.now()
        for (name, sym), (row, orders) in list(self.armed.items()):
            if not orders or sym not in self.symbols:
                continue
            q = self.feed.quote(sym)
            if not q or q.get("ltp") is None:
                continue
            ltp = float(q["ltp"])
            live = [o for o in orders if not o.get("valid_until") or now.strftime("%H:%M") <= o["valid_until"]]
            def reached(o):
                if o.get("entry") == "limit":           # price came back to the level
                    return ltp <= o["level"] if o["side"] == "BUY" else ltp >= o["level"]
                return ltp >= o["level"] if o["side"] == "BUY" else ltp <= o["level"]
            hit = [o for o in live if reached(o)]
            if not hit:
                continue
            o = min(hit, key=lambda x: abs(ltp - x["level"]))
            key = (row["name"], sym, o.get("oco") or o["side"])
            # one cancels the other: drop every order of this group before acting
            self.armed[(name, sym)] = (row, [x for x in orders if x is not o and (o.get("oco") is None or x.get("oco") != o.get("oco"))])
            if key in self.triggered:
                continue
            self.triggered.add(key)
            df = self.cache.get(sym)
            if df is None or df.empty:
                continue
            s = {"ts": str(pd.Timestamp(q.get("ts") or now)), "side": o["side"], "close": ltp, "stop_loss": o["stop_loss"],
                 "target": o["target"], "reason": f"{o['reason']} at {ltp:.2f}", "live_trigger": True,
                 "trail": o.get("trail"), "max_minutes": o.get("max_minutes"),
                 "tags": {**(o.get("tags") or {}), "trigger": "touch", "level": o["level"], "oco": o.get("oco")}}
            events.append(self._new_signal(row, sym, df, s))
            if sym in self.status:
                self.status[sym]["signal"] = o["side"]
        return events

    def staleness(self, signal_ts, touch: bool = False) -> str | None:
        """Live mode: a call is only traded while it is fresh and the market is open. Returns the reason if not."""
        if self.sim:
            return None
        now = pd.Timestamp.now()
        t = now.time()
        if not (datetime.strptime("09:15", "%H:%M").time() <= t < datetime.strptime("15:30", "%H:%M").time()) or now.dayofweek >= 5:
            return "not traded: market closed"
        minutes = int(settings.LIVE_INTERVAL.rstrip("m")) if settings.LIVE_INTERVAL.endswith("m") else 5
        closed_at = pd.Timestamp(signal_ts) + (pd.Timedelta(0) if touch else pd.Timedelta(minutes=minutes))
        age = (now - closed_at).total_seconds() / 60
        if age > 2 * minutes:
            return f"not traded: call was {int(age)} min old"
        return None

    def forming(self, sym: str) -> dict | None:
        """The candle still being built from live ticks (shown on the chart, never used by strategies)."""
        if self.feed is None:
            return None
        minutes = int(settings.LIVE_INTERVAL.rstrip("m")) if settings.LIVE_INTERVAL.endswith("m") else 5
        df = self.feed.builder.candles(sym, minutes, closed_only=False)
        if df.empty:
            return None
        r = df.iloc[-1]
        return {"ts": df.index[-1], "open": float(r.open), "high": float(r.high), "low": float(r.low), "close": float(r.close),
                "volume": float(r.volume)}

    def write_status_file(self) -> None:
        """data/status.json: a snapshot for troubleshooting (readable from the project folder)."""
        try:
            from yonexis.data.connectors.upstox import tokens
            rows = {}
            for sym, df in self.cache.items():
                if df is None or df.empty:
                    continue
                days = sorted({str(d.date()) for d in df.index.normalize().unique()})
                st = self.status.get(sym, {})
                rows[sym] = {"candles": len(df), "first": str(df.index[0]), "last_closed": str(df.index[-1]), "sessions": days[-5:],
                             "forming": (st.get("forming") or {}).get("ts"), "price": st.get("price"), "tick_ts": st.get("tick_ts")}
            out = {"at": datetime.now().isoformat(timespec="seconds"), "connector": self.connector_name, "running": self.running,
                   "logged_in": bool(tokens.token), "token_session": {k: v for k, v in tokens.load().items() if k != "access_token"},
                   "feed": self.feed.info() if self.feed is not None else None, "feed_day": str(self._feed_day),
                   "last_error": self.last_error, "symbols": rows, "risk": risk.snapshot()}
            (settings.DATA_DIR / "status.json").write_text(json.dumps(out, indent=1, default=str))
        except Exception as e:  # pragma: no cover
            log.warning("status file: %s", e)

    def quote(self, sym: str, strict: bool = False) -> dict:
        """Latest price: live tick when streaming, else the last closed candle (sim / display only).
        strict=True (used for paper fills and exits) never falls back to a candle when a live feed is configured."""
        if self.feed is not None:
            q = self.feed.quote(sym)
            if q and q.get("ltp"):
                return {"ltp": q["ltp"], "high": q["ltp"], "low": q["ltp"], "ts": q.get("ts")}
            if strict:
                return {}
        df = self.cache.get(sym)
        if df is None or df.empty:
            return {}
        c = df.iloc[-1]
        return {"ltp": float(c["close"]), "high": float(c["high"]), "low": float(c["low"]), "ts": str(df.index[-1])}

    def market_time(self):
        if self.sim:
            any_df = next((d for d in self.cache.values() if not d.empty), None)
            return any_df.index[-1].time() if any_df is not None else datetime.now().time()
        return datetime.now().time()

    def manage_positions(self) -> list[dict]:
        if not hasattr(self.broker, "manage"):
            return []
        events = []
        if self.feed is not None and not (self.feed.status == "connected"):
            return []                                  # no live prices: never fill paper trades on stale data
        quotes = {s: self.quote(s, strict=True) for s in list(self.symbols)}
        sq = self.market_time() >= datetime.strptime("15:15", "%H:%M").time()
        try:
            ts = None
            if self.sim:
                any_df = next((d for d in self.cache.values() if not d.empty), None)
                ts = any_df.index[-1].to_pydatetime() if any_df is not None else None
            for a in self.broker.manage(quotes, square_off=sq, ts=ts):
                events.append({"type": "paper_exit", **a})
                if a.get("exit") == "stop_loss" and not sq:
                    ev = self._relay(a, quotes.get(a["symbol"]) or {})
                    if ev:
                        events.append(ev)
        except Exception as e:
            self.last_error = f"paper manage: {e}"
        return events

    def _relay(self, exit_ev: dict, q: dict) -> dict | None:
        """Stop-and-reverse: the strategy may hand a stopped position over to the opposite side immediately."""
        t = (db.fetch(db.paper_trades, db.paper_trades.c.id == exit_ev["trade_id"]) or [None])[0]
        if not t or not t["signal_id"]:
            return None
        sig = (db.fetch(db.signals, db.signals.c.id == t["signal_id"]) or [None])[0]
        found = next(((r, c) for r, c in loader.trading_strategies() if r["name"] == t["strategy"]), None)
        if not sig or not found:
            return None
        row, cls = found
        if self.sim:
            any_df = next((d for d in self.cache.values() if not d.empty), None)
            now = any_df.index[-1] if any_df is not None else pd.Timestamp.now()
        else:
            now = pd.Timestamp.now()
        rv = cls(**(db.uj(row["params"], {}) or {})).reversal(db.uj(sig["tags"], {}) or {}, t["side"], now)
        if rv is None:
            return None
        sym = t["symbol"]
        ltp = float(q.get("ltp") or exit_ev["price"])
        lvl = rv.entry_price if rv.entry_price is not None else t["stop_loss"]
        px = max(ltp, lvl) if rv.side == "BUY" else min(ltp, lvl)
        df = self.cache.get(sym)
        if df is None or df.empty:
            return None
        s = {"ts": str(now), "side": rv.side, "close": px, "stop_loss": rv.stop_loss, "target": rv.target,
             "reason": rv.reason, "live_trigger": True, "trail": rv.trail, "max_minutes": rv.max_minutes,
             "tags": {**rv.tags, "trigger": "reversal", "level": lvl, "from_side": t["side"]}}
        if sym in self.status:
            self.status[sym]["signal"] = rv.side
        ev = self._new_signal(row, sym, df, s)
        if ev.get("order"):
            db.update_row(db.paper_trades, t["id"], exit_reason="reversed")
            db.update_row(db.signals, t["signal_id"], outcome="reversed")
        return ev

    def add_symbol(self, sym: str) -> dict:
        sym = sym.upper()
        if sym in self.symbols:
            return {"added": [], "invalid": {}}
        if self.feed is not None:
            r = self.feed.add([sym], settings.LIVE_INTERVAL)
            if sym in r["invalid"]:
                return r
        elif self.sim:
            from yonexis.data.connectors.simulated import simulate
            self.sim.full[sym] = simulate(sym, str(self.sim.days[0].date()), str(self.sim.days[-1].date()), seed=len(self.symbols) + 11)
            r = {"added": [sym], "invalid": {}}
        else:
            r = {"added": [sym], "invalid": {}}
        if sym not in self.symbols:
            self.symbols.append(sym)
        return r

    def remove_symbol(self, sym: str) -> None:
        sym = sym.upper()
        if self.feed is not None:
            self.feed.remove([sym])
        if sym in self.symbols:
            self.symbols.remove(sym)
        self.status.pop(sym, None)
        self.cache.pop(sym, None)
        self._last_closed.pop(sym, None)

    def on_login(self) -> None:
        self._feed_day = None

    # ------------------------------------------------------------ models
    def champion(self, kind: str, strategy: str) -> TrainedModel | None:
        rows = [m for m in db.fetch(db.models, order=db.models.c.id) if m["kind"] == kind and m["status"] == "champion"
                and m["strategy"] in (strategy, None, "")]
        if not rows:
            return None
        r = rows[-1]
        cached = self._models.get(f"{kind}:{strategy}")
        if cached and cached[0] == r["version"]:
            return cached[1]
        try:
            m = TrainedModel.load(r["path"])
            self._models[f"{kind}:{strategy}"] = (r["version"], m)
            return m
        except Exception:
            return None

    # ------------------------------------------------------------ loop
    async def publish(self, event: dict) -> None:
        msg = json.dumps(event, default=str)
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass

    def step(self) -> list[dict]:
        """One full cycle (fast + decision). The live loop runs the two parts separately (see run())."""
        return self.fast_step() + self.decide_step()

    def fast_step(self) -> list[dict]:
        """Every second: feed upkeep, stops/targets/time exits, touch entries, pairs. Never waits for strategy math."""
        events: list[dict] = []
        if self.feed is not None:
            try:
                self.ensure_feed()
            except Exception as e:
                self.last_error = f"feed: {e}"
        try:
            self.confirm_candles()
        except Exception as e:
            self.last_error = f"candle confirm: {e}"
        if time.time() - getattr(self, "_of_flush", 0) > 60:
            self._of_flush = time.time()
            self.flush_orderflow()
        if self.feed is not None and time.time() - getattr(self, "_news_at", 0) > 900:
            self._news_at = time.time()
            import threading
            from yonexis import news
            threading.Thread(target=news.collect_safe, args=(list(self.symbols),), daemon=True).start()
        events += self.manage_positions()
        events += self.check_triggers()
        if not self.sim and self.feed is not None and self.feed.status == "connected" and \
                datetime.strptime("09:15", "%H:%M").time() <= datetime.now().time() <= datetime.strptime("15:30", "%H:%M").time():
            try:
                events += self.step_pairs()
            except Exception as e:
                self.last_error = f"pairs: {e}"
        return events

    def decide_step(self) -> list[dict]:
        """Strategy decisions on closed candles (heavy at each candle close when many strategies are active)."""
        events: list[dict] = []
        strategies = loader.trading_strategies()
        scoped = {s for s in self.symbols if loader.in_scope(s)}
        cfg = BacktestConfig(capital_per_trade=risk.limits.capital_per_trade, lots_per_trade=settings.FUT_LOTS_PER_TRADE)
        for sym in list(self.symbols):
            try:
                df = self.candles(sym)
            except Exception as e:
                self.last_error = f"{sym}: {e}"
                continue
            if df.empty:
                continue
            self.cache[sym] = df
            forming = self.forming(sym)
            if forming is not None and forming["ts"] > df.index[-1]:
                view = pd.concat([df, pd.DataFrame([forming]).set_index("ts")[["open", "high", "low", "close", "volume"]]])
                st = zone_status(view)
                st["forming"] = {**forming, "ts": str(forming["ts"])}
                if st.get("zone_ts") == str(forming["ts"]):
                    st["zone_provisional"] = True
            else:
                st = zone_status(df)
            st["last_closed"] = str(df.index[-1])
            if sym.endswith("-FUT") and self.feed is not None:
                try:
                    from yonexis.data.connectors.upstox import instruments
                    st["contract"] = instruments.contract(sym)
                except Exception:
                    pass
            if self.feed is not None:
                q = self.feed.quote(sym)
                if q:
                    st["price"], st["tick_ts"] = round(q["ltp"], 2), q["ts"]
                    if q.get("cp"):
                        st["change_pct"] = round((q["ltp"] / q["cp"] - 1) * 100, 2)
            today = df.index[-1].normalize()
            new_candle = self._last_closed.get(sym) != df.index[-1]
            self._last_closed[sym] = df.index[-1]
            if not new_candle and sym in self.status:
                prev = self.status[sym]
                st["signal"] = prev.get("signal") if str(prev.get("last_closed", ""))[:10] == str(df.index[-1])[:10] else None
                self.status[sym] = st
                continue
            if not self.sim:
                today = pd.Timestamp.now().normalize()      # live: only the real current session can produce calls
                if df.index[-1] < today:
                    st.setdefault("signal", None)
                    self.status[sym] = st
                    continue
            if sym not in scoped:                  # watched and charted, but not part of the paper test
                self.armed = {k: v for k, v in self.armed.items() if k[1] != sym}
                st.setdefault("signal", None)
                st["not_in_test"] = True
                self.status[sym] = st
                continue
            for row, cls in strategies:
                res = run_backtest(cls, {sym: df}, db.uj(row["params"], {}) or {}, cfg)
                self.armed[(row["name"], sym)] = (row, [p for p in res.pending if pd.Timestamp(p["armed_ts"]) >= today])
                for s in res.signals:
                    ts = pd.Timestamp(s["ts"])
                    if ts < today:
                        continue
                    tg = s.get("tags") or {}
                    if tg.get("trigger") == "reversal":
                        continue        # live reversals are made the moment the stop is hit (manage_positions)
                    if tg.get("trigger") == "touch" and (row["name"], sym, tg.get("oco") or s["side"]) in self.triggered:
                        continue
                    key = (row["name"], sym, s["ts"])
                    if key in self.emitted:
                        continue
                    self.emitted.add(key)
                    reason = self.staleness(s["ts"], tg.get("trigger") == "touch")
                    if reason and "old" in reason:
                        continue        # happened while the engine was down / before an update: not a call anymore
                    events.append(self._new_signal(row, sym, df, s))
                self._update_outcomes(row, sym, res.trades, today)
                st.setdefault("signal", None)
                todays = [s for s in res.signals if pd.Timestamp(s["ts"]) >= today]
                if todays:
                    st["signal"] = todays[-1]["side"]
            self.status[sym] = st
        events.append({"type": "status", "ts": datetime.now().isoformat(timespec="seconds"),
                       "sim_day": str(self.sim.current_day.date()) if self.sim else None, "rows": self.status,
                       "feed": self.feed.info() if self.feed is not None else None})
        return events

    def _new_signal(self, row: dict, sym: str, df: pd.DataFrame, s: dict) -> dict:
        feats = signal_features(df, s["ts"], s["side"], s.get("tags"))
        meta = self.champion("meta", row["name"])
        imit = self.champion("imitation", row["name"])
        ai_p = float(meta.predict_proba([feats])[0]) if meta else None
        ad_p = float(imit.predict_proba([feats])[0]) if imit else None
        score = combine(ai_p, ad_p) if ai_p is not None else ad_p
        sid = db.insert_row(db.signals, source="simulated" if self.sim else "live", ts=pd.Timestamp(s["ts"]).to_pydatetime(), symbol=sym, strategy=row["name"],
                            strategy_version=row["version"], side=s["side"], price=float(s["close"]),
                            stop_loss=s["stop_loss"], target=s["target"], ai_score=ai_p, admin_score=ad_p, score=score,
                            model_version=(meta.version if meta else None), reason=s["reason"],
                            features=db.j(feats), tags=db.j(s.get("tags")), status="open")
        order = None
        if row["status"] in ("paper", "live_small", "live"):
            q = self.quote(sym)
            live_ok = self.feed is None or self.feed.status == "connected"
            ref = float(s["close"]) if (self.sim or s.get("live_trigger")) else (q.get("ltp") or float(s["close"]))
            touch = (s.get("tags") or {}).get("trigger") == "touch"
            stale = self.staleness(s["ts"], touch and not s.get("live_trigger"))
            if stale:
                db.update_row(db.signals, sid, status=stale[:64])
                return {"type": "signal", "id": sid, "symbol": sym, "strategy": row["name"], "side": s["side"], "ts": s["ts"],
                        "price": s["close"], "stop_loss": s["stop_loss"], "target": s["target"], "reason": s["reason"],
                        "ai_score": ai_p, "admin_score": ad_p, "score": score, "order": None, "not_traded": stale}
            sc_ = loader.get_scope()
            if sc_ and sc_.get("strategy") == "ai_pick":
                verdict = self._ai_gate(row, sym, df, s)
                score = verdict.get("p", score)
                if not verdict["ok"]:
                    db.update_row(db.signals, sid, status=f"not traded: {verdict['why']}"[:64], score=score)
                    return {"type": "signal", "id": sid, "symbol": sym, "strategy": row["name"], "side": s["side"], "ts": s["ts"],
                            "price": s["close"], "stop_loss": s["stop_loss"], "target": s["target"], "reason": s["reason"],
                            "ai_score": ai_p, "admin_score": ad_p, "score": score, "order": None, "not_traded": verdict["why"]}
                db.update_row(db.signals, sid, score=score)
                score = None          # the AI picker already decided; skip the generic min-score rule
            ok, why, qty = risk.check_entry(sym, s["side"], ref, score, row["name"])
            if ok and not live_ok:
                ok, why = False, "no live prices (log in to Upstox)"
                risk.state.open_positions.pop(sym, None)
            if ok:
                if row["status"] == "live_small":
                    qty = max(1, qty // 4)
                try:
                    order = self.broker.place(sym, s["side"], qty, ref, s["stop_loss"], s["target"], f"{row['name']}-{sid}",
                                              signal_id=sid, strategy=row["name"],
                                              ts=pd.Timestamp(s["ts"]).to_pydatetime() if self.sim else None,
                                              trail=s.get("trail"), max_minutes=s.get("max_minutes")) if isinstance(self.broker, PaperBroker) \
                        else self.broker.place(sym, s["side"], qty, ref, s["stop_loss"], s["target"], f"{row['name']}-{sid}")
                    db.update_row(db.signals, sid, status="taken")
                    db.log("engine", "order", order)
                except Exception as e:
                    risk.state.open_positions.pop(sym, None)
                    db.update_row(db.signals, sid, status=f"blocked: {e}"[:64])
            else:
                db.update_row(db.signals, sid, status=f"blocked: {why}"[:64])
        return {"type": "signal", "id": sid, "symbol": sym, "strategy": row["name"], "side": s["side"], "ts": s["ts"],
                "price": s["close"], "stop_loss": s["stop_loss"], "target": s["target"], "reason": s["reason"],
                "ai_score": ai_p, "admin_score": ad_p, "score": score, "order": order}

    def _ai_gate(self, row: dict, sym: str, df: pd.DataFrame, s: dict) -> dict:
        """AI picker for a live call: P(win) and expected R for this strategy on this stock right now."""
        try:
            from yonexis.lab import champion_selector
            from yonexis.ml import selector
            from yonexis.strategies import REGISTRY
            model, mrow = champion_selector()
            if model is None:
                return {"ok": False, "why": "AI pick: no trained picker (run Strategy Lab)"}
            ff = selector.feature_frame(df)
            idx = selector.index_5m(pd.Timestamp.now().normalize(), getattr(self.feed, "conn", None))
            if idx.empty:
                idx = self.cache.get("NIFTY-FUT")
            mkt = selector.market_frame({"NIFTY": idx}) if idx is not None and not idx.empty else None
            t = {"symbol": sym, "side": s["side"], "signal_ts": pd.Timestamp(s["ts"]), "entry_ts": pd.Timestamp(s["ts"]),
                 "exit_ts": pd.Timestamp(s["ts"]), "entry": s["close"], "qty": 1, "stop_loss": s["stop_loss"], "net_pnl": 0.0,
                 "tags": s.get("tags") or {}}
            rows = selector.rows_for_trades(pd.DataFrame([t]), row["name"], getattr(REGISTRY.get(row["name"]), "family", "custom"),
                                            {sym: ff}, mkt)
            if rows.empty:
                return {"ok": False, "why": "AI pick: no features yet"}
            r = selector.score(model, rows).iloc[0]
            p, ev = float(r["p"]), float(r["ev"])
            s.setdefault("tags", {}).update({"ai_p": round(p, 3), "ai_ev": round(ev, 3)})
            need = selector.min_p_for(model)
            if p < need or ev < 0.05:
                return {"ok": False, "p": p, "why": f"AI pick: P(win) {p:.0%} (needs {need:.0%}), EV {ev:+.2f}R"}
            return {"ok": True, "p": p}
        except Exception as e:
            return {"ok": False, "why": f"AI pick error: {e}"[:60]}

    def step_pairs(self) -> list[dict]:
        """Live pair trading (scope strategy = pair_trading): open both legs when the spread z-score stretches,
        close both when it converges / diverges further / times out."""
        from yonexis import pairs as pm
        from yonexis.strategies.loader import get_scope
        sc = get_scope()
        if not sc or sc.get("strategy") != "pair_trading" or not isinstance(self.broker, PaperBroker):
            return []
        events, now = [], pd.Timestamp.now()
        syms = [x for x in (sc.get("symbols") or self.symbols) if x in self.cache]
        day = now.normalize()
        if getattr(self, "_pairs_day", None) != day:
            hist = {x: self.cache[x][self.cache[x].index < day] for x in syms}
            self._pairs = [(p["a"], p["b"]) for p in pm.discover_pairs(hist)] or [(a, b) for a, b in pm.DEFAULT_PAIRS if a in syms and b in syms]
            self._pairs_day = day
        opens = [t for t in db.fetch(db.paper_trades, db.paper_trades.c.status == "open") if str(t["tag"] or "").startswith("pair:")]
        for a, b in self._pairs:
            key = f"pair:{a}/{b}"
            try:
                zf = pm.zscore_frame(self.cache[a], self.cache[b], pm.PARAMS)
            except Exception:
                continue
            if zf.empty:
                continue
            z = float(zf["z"].iloc[-1])
            legs = [t for t in opens if str(t["tag"]).startswith(key)]
            if legs:
                age = (now - pd.Timestamp(legs[0]["entry_ts"])).total_seconds() / 60
                sign = 1 if any(t["symbol"] == a and t["side"] == "SELL" for t in legs) else -1
                why = ("converged" if z == z and (abs(z) <= pm.PARAMS["exit_z"] or np.sign(z) != sign) else
                       "diverged_stop" if z == z and abs(z) >= pm.PARAMS["stop_z"] else
                       "time_exit" if age >= pm.PARAMS["max_minutes"] else None)
                if why:
                    for t in legs:
                        q = self.quote(t["symbol"], strict=True)
                        r = self.broker.close(t["id"], (q or {}).get("ltp") or t["last_price"], why)
                        if r:
                            events.append({"type": "paper_exit", **r})
                continue
            t_close = (zf.index[-1] + pd.Timedelta(minutes=5)).strftime("%H:%M")
            done_today = sum(1 for t in db.fetch(db.paper_trades, db.paper_trades.c.entry_ts >= day.to_pydatetime())
                             if str(t["tag"] or "").startswith(key)) // 2
            if (z != z or abs(z) < pm.PARAMS["entry_z"] or abs(z) >= pm.PARAMS["stop_z"] or not (pm.PARAMS["start"] <= t_close <= pm.PARAMS["entry_cutoff"])
                    or done_today >= pm.PARAMS["max_trades_per_day"] or a in risk.state.open_positions or b in risk.state.open_positions):
                continue
            beta = float(zf["beta"].iloc[-1]) if zf["beta"].iloc[-1] == zf["beta"].iloc[-1] else 1.0
            qa, qb = self.quote(a, strict=True), self.quote(b, strict=True)
            if not qa or not qb:
                continue
            half = risk.limits.capital_per_trade / 2
            sign = 1 if z > 0 else -1
            pid = f"{key}:{now:%H%M}"
            for sym, side, px, qty in ((a, "SELL" if sign > 0 else "BUY", qa["ltp"], max(1, int(half // qa["ltp"]))),
                                       (b, "BUY" if sign > 0 else "SELL", qb["ltp"], max(1, int(half * min(beta, 3.0) // qb["ltp"])))):
                sid = db.insert_row(db.signals, source="live", ts=now.to_pydatetime(), symbol=sym, strategy="pair_trading", strategy_version=1,
                                    side=side, price=float(px), stop_loss=float(px) * (0.9 if side == "BUY" else 1.1), target=None,
                                    reason=f"Pair {a}/{b} z={z:+.2f} beta={beta:.2f}", features=db.j({}),
                                    tags=db.j({"pair": f"{a}/{b}", "z": round(z, 2), "beta": round(beta, 3), "family": "pairs"}), status="taken")
                try:
                    o = self.broker.place(sym, side, qty, float(px), float(px) * (0.9 if side == "BUY" else 1.1), None, pid,
                                          signal_id=sid, strategy="pair_trading")
                    risk.state.open_positions[sym] = {"qty": qty, "value": qty * px, "side": side, "strategy": "pair_trading"}
                    events.append({"type": "signal", "id": sid, "symbol": sym, "strategy": "pair_trading", "side": side, "ts": str(now),
                                   "price": px, "stop_loss": None, "target": None, "reason": f"Pair {a}/{b} z={z:+.2f}", "order": o})
                except Exception as e:
                    db.update_row(db.signals, sid, status=f"blocked: {e}"[:64])
        return events

    def _update_outcomes(self, row: dict, sym: str, trades: pd.DataFrame, today: pd.Timestamp) -> None:
        if trades.empty:
            return
        done = trades[(pd.to_datetime(trades["signal_ts"]) >= today) & (trades["exit_reason"] != "end_of_data")]
        if done.empty:
            return
        open_sigs = {str(pd.Timestamp(s["ts"])): s for s in db.fetch(db.signals, db.real_signals() & (db.signals.c.symbol == sym) &
                     (db.signals.c.strategy == row["name"]) & (db.signals.c.outcome_pnl.is_(None)))}
        for t in done.itertuples():
            s = open_sigs.get(str(pd.Timestamp(t.signal_ts)))
            if s and not str(s["status"]).startswith("taken") and s["status"] != "closed":
                # hypothetical result of a call that was not traded (blocked / skipped) - used for feedback scoring
                db.update_row(db.signals, s["id"], outcome_pnl=float(t.net_pnl), outcome=f"would: {t.exit_reason}")

    async def run(self) -> None:
        self.running = True
        log.info("live engine started: connector=%s symbols=%d", self.connector_name, len(self.symbols))
        last_status_file = 0.0
        while self.running:
            if time.time() - last_status_file > 30:
                last_status_file = time.time()
                await asyncio.to_thread(self.write_status_file)
            try:
                if self.sim:
                    events = await asyncio.to_thread(self.step)
                else:
                    events = await asyncio.to_thread(self.fast_step)
                    if getattr(self, "_decide_task", None) is None or self._decide_task.done():
                        self._decide_task = asyncio.create_task(self._decide())
                for e in events:
                    await self.publish(e)
                self.last_error = None
            except Exception as e:  # keep the loop alive
                self.last_error = f"{e}\n{traceback.format_exc()[-1500:]}"
                log.exception("live step failed")
            if self.sim:
                if self.sim.advance():
                    self.emitted = {k for k in self.emitted if pd.Timestamp(k[2]) >= self.sim.current_day}
                await asyncio.sleep(settings.LIVE_REPLAY_SPEED)
            else:
                await asyncio.sleep(1 if self.feed is not None else 30)

    async def _decide(self) -> None:
        """Decision part in its own thread, so a slow candle close never delays stop/target checks."""
        try:
            for e in await asyncio.to_thread(self.decide_step):
                await self.publish(e)
        except Exception as e:
            self.last_error = f"decide: {e}"
            log.exception("live decision step failed")

    def stop(self) -> None:
        self.running = False
        if self.feed is not None:
            self.feed.stop()


engine: LiveEngine | None = None
