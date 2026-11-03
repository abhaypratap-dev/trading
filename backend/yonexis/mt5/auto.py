"""MT5 automation: the YONEXIS AI zone rules, live, on a watchlist, executed in MetaTrader 5.

Prices and candles come from Upstox (the live engine); orders go to MT5 through the bridge. Upstox never trades.
The rules are the same as the YONEXIS AI tab (yonexis.zone_lab) - only the timing is live:
  * zone = first red 5-minute candle of the day (known the moment that candle closes);
  * spike ON : BUY the moment a tick crosses the zone high from below (SELL: zone low from above); the stop (opposite
               zone edge) and the targets sit in MT5 as SL / TP, so MT5 closes them server-side without delay;
  * spike OFF: BUY when a 5-minute candle opens or closes above the zone high (SELL below the zone low); the stop
               needs a candle close beyond the opposite edge (YONEXIS closes it); a far "disaster" SL sits in MT5;
  * targets  : per lot, entry +/- range x multiplier; range = zone (zone candle) or IZ (entry candle, set by modifying
               the TP when that candle closes);
  * relay    : a stop starts the opposite entry at once, while entries per day remain and before the cut-off;
  * square-off at the configured time: every YONEXIS position is closed.
Netting accounts (one position per symbol) get one order for all lots; YONEXIS closes each lot at its target.
Hedging accounts get one position per lot with its own TP.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, time as dtime

import pandas as pd

from yonexis import db
from yonexis.mt5 import config
from yonexis.mt5.bridge import Bridge, get_bridge
from yonexis.zone_lab import target_price

log = logging.getLogger("yonexis.mt5")
SESSION_START, SESSION_END = dtime(9, 15), dtime(15, 30)


def _t(s: str) -> dtime:
    h, m = map(int, str(s).split(":"))
    return dtime(h, m)


def log_event(kind: str, symbol: str, op: str, ok: bool | None, detail: dict, latency_ms: float | None = None) -> None:
    try:
        db.insert_row(db.mt5_log, kind=kind, symbol=symbol, op=op, ok=None if ok is None else int(bool(ok)),
                      latency_ms=latency_ms, detail=json.dumps(detail, default=str)[:4000])
    except Exception:                                   # pragma: no cover - logging must never stop trading
        log.exception("mt5 log failed")


# ---------------------------------------------------------------------------------------------- guard
def account_kind(state: dict) -> str:
    return str((state.get("account") or {}).get("trade_mode") or "unknown")


def guard(cfg: dict, bridge: Bridge, volume: float, new_entry: bool, open_positions: int, venue: str = "MT5",
          where: str = "System > MT5 connection") -> tuple[bool, str]:
    """May YONEXIS send this order? Every order passes here."""
    mode = cfg["mode"]
    if mode == "off":
        return False, f"{venue} trading is off ({where})"
    if mode == "dry_run":
        return False, "dry run: order logged, not sent"
    h = bridge.health()
    if not h["connected"]:
        return False, h["message"]
    st = bridge.state
    kind = account_kind(st)
    if mode == "demo" and kind not in ("demo", "contest"):
        return False, f"mode is DEMO but the {venue} account is {kind} - orders blocked"
    if mode == "live":
        if kind != "real":
            return False, f"mode is LIVE but the {venue} account is {kind}"
        if cfg.get("live_confirm") != config.LIVE_PHRASE:
            return False, f"live trading is not confirmed ({where})"
    if not st.get("trade_allowed", True):
        return False, f"Algo Trading is switched off in {venue}"
    if volume > float(cfg["max_volume"]) + 1e-9:
        return False, f"volume {volume} is above the limit {cfg['max_volume']}"
    if new_entry and open_positions >= int(cfg["max_positions"]):
        return False, f"{open_positions} positions open - limit {cfg['max_positions']}"
    return True, ""


# ---------------------------------------------------------------------------------------------- trader
def _live_engine():
    from yonexis.live import engine as live_mod        # the module; its `engine` attribute is the running LiveEngine
    return live_mod.engine


class AutoTrader:
    """One instance per process; run() is a loop in its own thread (every 100 ms)."""

    def __init__(self, engine_getter=None, bridge_getter=get_bridge, clock=datetime.now, configs=None,
                 state_key="mt5_auto_state", log_fn=None, venue="MT5", result_timeout=15,
                 where="System > MT5 connection"):
        """configs / state_key / log_fn / venue let another executor (OpenAIAP web automation) run the same rules."""
        self.engine_getter = engine_getter or _live_engine
        self.bridge_getter = bridge_getter
        self.clock = clock
        self.configs_fn = configs or (lambda: (config.auto(), config.get(), config.mapping()))
        self.state_key, self.venue, self.result_timeout, self.where = state_key, venue, result_timeout, where
        self.log_event = log_fn or log_event
        self.day = None
        self.syms: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}            # command id -> what it was for
        self.events: list[dict] = []                  # recent activity for the dashboard
        self.running = False
        self.last_error = None
        self.day_equity0 = None
        self.halted = None                            # reason when the daily loss limit stopped trading
        self._loaded = False

    # ------------------------------------------------------------- persistence (survives restarts)
    def _save(self) -> None:
        db.set_setting(self.state_key, {"day": str(self.day), "syms": self.syms, "equity0": self.day_equity0,
                                          "halted": self.halted})

    def _load(self, day) -> None:
        s = db.get_setting(self.state_key) or {}
        if s.get("day") == str(day):
            self.syms = s.get("syms") or {}
            self.day_equity0 = s.get("equity0")
            self.halted = s.get("halted")
        self._loaded = True

    def note(self, sym: str, text: str, **kw) -> None:
        e = {"t": self.clock().strftime("%H:%M:%S"), "symbol": sym, "text": text, **kw}
        self.events = (self.events + [e])[-200:]
        log.info("mt5 %s: %s", sym, text)

    # ------------------------------------------------------------- main loop
    def run(self) -> None:
        self.running = True
        while self.running:
            try:
                self.step()
                self.last_error = None
            except Exception as e:                      # keep looping
                self.last_error = str(e)
                log.exception("mt5 auto step failed")
            time.sleep(0.1)

    def stop(self) -> None:
        self.running = False

    def _configs(self):
        if time.time() - getattr(self, "_cfg_at", 0) > 1:       # read the settings at most once a second
            self._cfg = self.configs_fn()
            self._cfg_at = time.time()
        return self._cfg

    def step(self) -> None:
        a, cfg, self._map = self._configs()
        now = self.clock()
        day = now.date()
        if day != self.day:
            self.day, self.syms, self.pending, self.halted, self.day_equity0 = day, {}, {}, None, None
            self._loaded = False
        if not self._loaded:
            self._load(day)
        bridge = self.bridge_getter()
        self._collect(bridge, cfg)
        self._watch(bridge)
        if not a["enabled"] or cfg["mode"] == "off":
            return
        eng = self.engine_getter()
        if eng is None or not (SESSION_START <= now.time() <= SESSION_END):
            return
        if getattr(eng, "connector_name", "upstox") != "upstox":
            if not getattr(self, "_warned_feed", False):
                self._warned_feed = True
                self.note("*", "automation needs live Upstox prices (LIVE_CONNECTOR=upstox) - waiting", level="warn")
            return
        self._daily_loss(bridge, cfg)
        p = a["params"]
        for sym in a["symbols"]:
            try:
                self._symbol(sym, eng, bridge, cfg, p, now)
            except Exception as e:
                self.note(sym, f"error: {e}", level="error")

    def _watch(self, bridge: Bridge) -> None:
        """Tell the EA which MT5 symbols to report quotes for (every mapped symbol), again every minute."""
        want = ",".join(sorted({v["mt5"] for v in self._map.values()}))
        if not want or not bridge.health()["connected"]:
            return
        if want != getattr(self, "_watched", None) or time.time() - getattr(self, "_watch_at", 0) > 60:
            bridge.send("watch", symbol=want)
            self._watched, self._watch_at = want, time.time()

    # ------------------------------------------------------------- results from the EA
    def _collect(self, bridge: Bridge, cfg: dict) -> None:
        for cid, meta in list(self.pending.items()):
            r = bridge.take(cid)
            if r is None:
                if time.time() - meta["at"] > self.result_timeout:
                    self.pending.pop(cid)
                    self.note(meta["sym"], f"{meta['op']}: no answer from {self.venue} in {self.result_timeout} s", level="error")
                    self.log_event("timeout", meta["sym"], meta["op"], False, meta)
                    self._failed(meta)
                continue
            self.pending.pop(cid)
            ok = bool(r.get("ok"))
            self.log_event("result", meta["sym"], meta["op"], ok, {"result": r, "for": meta}, r.get("latency_ms"))
            st = self.syms.get(meta["sym"])
            if not ok:
                self.note(meta["sym"], f"{meta['op']} rejected by {self.venue}: {r.get('desc') or r.get('error')} (retcode {r.get('retcode')})", level="error")
                self._failed(meta)
                continue
            self.note(meta["sym"], f"{meta['op']} done at {r.get('price')} in {r.get('latency_ms')} ms", level="ok")
            if st and meta["op"] in ("buy", "sell"):
                for leg in st["legs"]:
                    if leg["id"] in meta.get("legs", []):
                        leg["status"], leg["position"], leg["fill"] = "open", r.get("position"), r.get("price")
                        leg["open_at"] = time.time()
            self._save()

    def _failed(self, meta: dict) -> None:
        st = self.syms.get(meta["sym"])
        if st and meta["op"] in ("buy", "sell"):
            st["legs"] = [l for l in st["legs"] if l["id"] not in meta.get("legs", [])]
            self._save()

    def _daily_loss(self, bridge: Bridge, cfg: dict) -> None:
        acc = bridge.state.get("account") or {}
        eq = acc.get("equity")
        if eq is None:
            return
        if self.day_equity0 is None:
            self.day_equity0 = float(eq)
            self._save()
        lim = float(cfg.get("max_daily_loss") or 0)
        if lim > 0 and not self.halted and self.day_equity0 - float(eq) >= lim:
            self.halted = f"daily loss {self.day_equity0 - float(eq):.2f} reached the limit {lim}"
            self.note("*", f"STOPPED: {self.halted}; closing all YONEXIS positions", level="error")
            self.close_all(bridge, cfg, "daily loss limit")

    # ------------------------------------------------------------- per symbol
    def _open_positions(self, bridge: Bridge, cfg: dict) -> list[dict]:
        return [p for p in (bridge.state.get("positions") or []) if int(p.get("magic") or 0) == int(cfg["magic"])]

    def _symbol(self, sym: str, eng, bridge: Bridge, cfg: dict, p: dict, now: datetime) -> None:
        st = self.syms.setdefault(sym, {"zone": None, "cycles": 0, "legs": [], "last_px": None, "bar": None,
                                        "bar_ohlc": None, "prev_close": None, "done": None, "skip": None})
        if st["skip"]:
            return
        mp = self._map.get(sym)
        if not mp:
            st["skip"] = f"no {self.venue} symbol mapped ({self.venue} tab > Symbol mapping)"
            self.note(sym, st["skip"], level="warn")
            return
        q = eng.quote(sym, strict=True)
        px = q.get("ltp") if q else None
        if not px:
            return
        px = float(px)
        sq = _t(p["square_off"])
        if now.time() >= sq:
            if any(l["status"] in ("open", "sending") for l in st["legs"]):
                self.note(sym, f"square-off {p['square_off']}: closing")
                self._close_legs(sym, st, bridge, cfg, "square_off")
            st["done"] = st["done"] or "square-off"
            return
        self._sync_closed(sym, st, bridge, cfg, p, px, now)
        fm = eng.forming(sym)
        bar_ts = str(fm["ts"]) if fm else None
        new_bar = bar_ts is not None and bar_ts != st["bar"]
        closed = st["bar_ohlc"] if (new_bar and st["bar"] is not None) else None
        if new_bar:
            st["bar"] = bar_ts
        if fm:
            st["bar_ohlc"] = {"ts": bar_ts, "open": fm["open"], "high": fm["high"], "low": fm["low"], "close": fm["close"]}
        # ---- the zone: first red 5-minute candle of today
        if st["zone"] is None:
            z = None
            if closed or time.time() - st.get("zone_check", 0) > 2:
                st["zone_check"] = time.time()
                z = self._find_zone(eng, sym, now, closed)
            if z is None:
                st["last_px"] = px
                return
            rng = z["high"] - z["low"]
            pct = rng / z["close"] * 100 if z["close"] else 0
            if rng <= 0 or pct > float(p.get("max_zone_pct") or 100):
                st["skip"] = f"zone {pct:.2f}% wide - outside the limit"
                self.note(sym, st["skip"], level="warn")
                return
            st["zone"] = {"ts": z["ts"], "high": z["high"], "low": z["low"], "range": rng}
            st["prev_close"] = z["close"]
            self.note(sym, f"zone {z['low']:.2f} - {z['high']:.2f} (range {rng:.2f})")
            self._save()
        z = st["zone"]
        hi, lo = z["high"], z["low"]
        if closed:                                     # a 5-minute candle just closed
            self._on_close(sym, st, bridge, cfg, p, closed, now)
        if p.get("spike", True):
            self._spike_tick(sym, st, bridge, cfg, p, px, now, hi, lo)
        elif new_bar and fm:
            self._open_rule(sym, st, bridge, cfg, p, float(fm["open"]), now, hi, lo)
        if cfg["mode"] == "dry_run":
            self._paper_exits(sym, st, bridge, cfg, p, px, now)
        else:
            self._netting_targets(sym, st, bridge, cfg, px)
        st["last_px"] = px

    def _find_zone(self, eng, sym, now, closed) -> dict | None:
        today = pd.Timestamp(now.date())
        cand = []
        df = eng.candles(sym)
        if df is not None and not df.empty:
            d = df[(df.index >= today + pd.Timedelta(hours=9, minutes=15)) & (df.index < today + pd.Timedelta(days=1))]
            cand = [{"ts": str(t), "open": float(r.open), "high": float(r.high), "low": float(r.low), "close": float(r.close)}
                    for t, r in d.iterrows()]
        if closed and pd.Timestamp(closed["ts"]) >= today + pd.Timedelta(hours=9, minutes=15) and \
                not any(c["ts"] == closed["ts"] for c in cand):
            cand.append(closed)
        for c in sorted(cand, key=lambda c: c["ts"]):
            if c["close"] < c["open"]:
                return c
        return None

    def _can_enter(self, st, p, now) -> bool:
        return (not st["done"] and not self.halted and st["cycles"] < int(p["max_cycles"]) and now.time() < _t(p["entry_cutoff"])
                and not any(l["status"] in ("open", "sending") for l in st["legs"]))

    # ---- entries
    def _spike_tick(self, sym, st, bridge, cfg, p, px, now, hi, lo) -> None:
        last = st["last_px"]
        if last is None or not self._can_enter(st, p, now):
            return
        if last < hi <= px:
            self._enter(sym, st, bridge, cfg, p, "BUY", hi, now, f"price crossed the zone high {hi:.2f}")
        elif p.get("allow_short", True) and last > lo >= px:
            self._enter(sym, st, bridge, cfg, p, "SELL", lo, now, f"price crossed the zone low {lo:.2f}")

    def _open_rule(self, sym, st, bridge, cfg, p, o, now, hi, lo) -> None:
        legs = [l for l in st["legs"] if l["status"] in ("open", "sending")]
        if legs:
            side = legs[0]["side"]
            if (side == "BUY" and o < lo) or (side == "SELL" and o > hi):
                self._stop_and_relay(sym, st, bridge, cfg, p, o, now, f"candle opened beyond the stop at {o:.2f}")
            return
        pc = st["prev_close"]
        if pc is None or not self._can_enter(st, p, now):
            return
        if o > hi and pc <= hi:
            self._enter(sym, st, bridge, cfg, p, "BUY", o, now, f"candle opened above the zone high at {o:.2f}")
        elif p.get("allow_short", True) and o < lo and pc >= lo:
            self._enter(sym, st, bridge, cfg, p, "SELL", o, now, f"candle opened below the zone low at {o:.2f}")

    def _on_close(self, sym, st, bridge, cfg, p, c, now) -> None:
        z = st["zone"]
        hi, lo = z["high"], z["low"]
        for leg in st["legs"]:                         # IZ: the entry candle has closed -> its range fixes the target
            if leg["target"] is None and leg["bar"] == c["ts"] and leg["status"] in ("open", "sending"):
                leg["target"] = target_price("iz", leg["side"], leg["mult"], leg["ref"], hi, lo, c["high"], c["low"])
                leg["iz_range"] = round(c["high"] - c["low"], 4)
                self.note(sym, f"lot {leg['lot']}: IZ range {leg['iz_range']} -> target {leg['target']:.2f}")
                if leg.get("position") and not self._netting(bridge):
                    self._send(sym, bridge, cfg, "modify", {"symbol": leg["mt5"], "ticket": leg["position"], "sl": leg["sl"],
                                                            "tp": leg["target"] + leg["off"]}, legs=[leg["id"]], check=False)
        if not p.get("spike", True):
            legs = [l for l in st["legs"] if l["status"] in ("open", "sending")]
            if legs:
                side = legs[0]["side"]
                if (side == "BUY" and c["close"] < lo) or (side == "SELL" and c["close"] > hi):
                    self._stop_and_relay(sym, st, bridge, cfg, p, c["close"], now, f"candle closed beyond the stop at {c['close']:.2f}")
            elif self._can_enter(st, p, now) and st["prev_close"] is not None:
                pc = st["prev_close"]
                if c["close"] > hi and pc <= hi and c["open"] <= hi:
                    self._enter(sym, st, bridge, cfg, p, "BUY", c["close"], now, f"candle closed above the zone high at {c['close']:.2f}")
                elif p.get("allow_short", True) and c["close"] < lo and pc >= lo and c["open"] >= lo:
                    self._enter(sym, st, bridge, cfg, p, "SELL", c["close"], now, f"candle closed below the zone low at {c['close']:.2f}")
        st["prev_close"] = c["close"]
        self._save()

    def _netting(self, bridge) -> bool:
        return str((bridge.state.get("account") or {}).get("margin_mode") or "hedging") != "hedging"

    def _enter(self, sym, st, bridge, cfg, p, side, ref, now, why) -> None:
        z = st["zone"]
        hi, lo, rng = z["high"], z["low"], z["range"]
        mp = self._map[sym]
        off = self._offset(sym, mp, bridge)
        mults = [float(x) for x in (p.get("lot_targets") or [p.get("target_mult", 1.5)])]
        base = p.get("target_base", "zone")
        spike = p.get("spike", True)
        stop = lo if side == "BUY" else hi
        sl = stop if spike else (lo - rng if side == "BUY" else hi + rng)       # spike off: far "disaster" stop only
        bar = st["bar"]
        st["cycles"] += 1
        legs = []
        for i, m in enumerate(mults):
            legs.append({"id": f"{sym}:{now:%Y%m%d}:{st['cycles']}:{i + 1}", "side": side, "lot": i + 1, "mult": m, "ref": ref,
                         "stop": stop, "sl": sl + off, "off": off, "target": target_price(base, side, m, ref, hi, lo) if base == "zone" else None,
                         "bar": bar, "mt5": mp["mt5"], "volume": mp["volume"], "status": "sending", "position": None,
                         "cycle": st["cycles"], "opened": now.strftime("%H:%M:%S")})
        st["legs"] += legs
        tg = " / ".join(f"{l['target'] + off:.2f}" for l in legs if l["target"] is not None) or \
            "set when the entry candle closes (IZ)"
        self.note(sym, f"entry {st['cycles']}: {side} {mp['mt5']} at {ref + off:.2f} · SL {stop + off:.2f} · "
                       f"target{'s' if len(legs) > 1 else ''} {tg} · {len(legs)} lot{'s' if len(legs) > 1 else ''} ({why})",
                  level="signal", side=side, mt5=mp["mt5"], price=round(ref + off, 2), sl=round(stop + off, 2),
                  targets=[round(l["target"] + off, 2) for l in legs if l["target"] is not None])
        op = "buy" if side == "BUY" else "sell"
        if self._netting(bridge):
            self._send(sym, bridge, cfg, op, {"symbol": mp["mt5"], "volume": round(mp["volume"] * len(legs), 8), "sl": sl + off,
                                              "comment": f"YX {sym[:12]} {st['cycles']}"}, legs=[l["id"] for l in legs], new_entry=True)
        else:
            for leg in legs:
                tp = leg["target"] + off if leg["target"] is not None else None
                self._send(sym, bridge, cfg, op, {"symbol": mp["mt5"], "volume": mp["volume"], "sl": sl + off, "tp": tp,
                                                  "comment": f"YX {sym[:10]} {leg['cycle']}/{leg['lot']}"}, legs=[leg["id"]], new_entry=True)
        self._save()

    def _offset(self, sym, mp, bridge) -> float:
        """Zone levels come from Upstox prices; with price mode 'offset' they are shifted by the MT5 - Upstox gap."""
        if mp.get("price") != "offset":
            return 0.0
        q = (bridge.state.get("quotes") or {}).get(mp["mt5"]) or {}
        eng = self.engine_getter()
        u = (eng.quote(sym, strict=True) or {}).get("ltp") if eng else None
        if q.get("bid") and q.get("ask") and u:
            return round((float(q["bid"]) + float(q["ask"])) / 2 - float(u), 6)
        return 0.0

    # ---- exits
    def _stop_and_relay(self, sym, st, bridge, cfg, p, px, now, why) -> None:
        legs = [l for l in st["legs"] if l["status"] in ("open", "sending")]
        side = legs[0]["side"]
        self.note(sym, f"stop: {why}", level="exit")
        self._close_legs(sym, st, bridge, cfg, "stop_loss")
        nside = "SELL" if side == "BUY" else "BUY"
        if self._can_enter(st, p, now) and (nside == "BUY" or p.get("allow_short", True)):
            self._enter(sym, st, bridge, cfg, p, nside, px, now, "relay after the stop")

    def _close_legs(self, sym, st, bridge, cfg, why) -> None:
        legs = [l for l in st["legs"] if l["status"] in ("open", "sending")]
        if not legs:
            return
        if self._netting(bridge):
            vol = round(sum(l["volume"] for l in legs), 8)
            self._send(sym, bridge, cfg, "close", {"symbol": legs[0]["mt5"], "volume": vol, "comment": why},
                       legs=[l["id"] for l in legs], check=False)
        else:
            for l in legs:
                if l.get("position"):
                    self._send(sym, bridge, cfg, "close", {"symbol": l["mt5"], "ticket": l["position"], "comment": why},
                               legs=[l["id"]], check=False)
        for l in legs:
            l["status"], l["closed_why"] = "closed", why
        self._save()

    def _netting_targets(self, sym, st, bridge, cfg, px) -> None:
        """Netting accounts carry no TP per lot: close each lot's volume when price reaches its target."""
        if not self._netting(bridge):
            return
        for l in st["legs"]:
            if l["status"] == "open" and l["target"] is not None and l["bar"] != st["bar"]:
                hit = px >= l["target"] if l["side"] == "BUY" else px <= l["target"]
                if hit:
                    l["status"], l["closed_why"] = "closed", "target"
                    self.note(sym, f"lot {l['lot']} target {l['target']:.2f} reached", level="exit")
                    self._send(sym, bridge, cfg, "close", {"symbol": l["mt5"], "volume": l["volume"], "comment": "target"},
                               legs=[l["id"]], check=False)
                    self._save()

    def _sync_closed(self, sym, st, bridge, cfg, p, px, now) -> None:
        """Legs whose MT5 position disappeared were closed by MT5 (SL / TP / manually). A stop starts the relay.
        Hedging: matched by position ticket. Netting (one position per symbol): the symbol's YONEXIS position is gone."""
        if bridge.health()["connected"] is False:
            return
        positions = bridge.state.get("positions") or []
        # only legs filled more than 3 s ago: the EA's position list can lag its order answer by a moment
        legs = [l for l in st["legs"] if l["status"] == "open" and l.get("position") and time.time() - l.get("open_at", 0) > 3]
        if not legs:
            return
        if self._netting(bridge):
            mine = {(x.get("symbol") or "") for x in positions if int(x.get("magic") or 0) == int(cfg["magic"])}
            gone = [l for l in legs if l["mt5"] not in mine]
        else:
            open_tickets = {int(x.get("ticket") or 0) for x in positions}
            gone = [l for l in legs if int(l["position"]) not in open_tickets]
        if not gone:
            return
        deals = {int(d.get("position") or 0): d for d in (bridge.state.get("deals") or []) if d.get("entry") in ("out", "inout")}
        stopped = False
        for l in gone:
            d = deals.get(int(l["position"]))
            reason = (d or {}).get("reason") or ("sl" if (px <= l["stop"] if l["side"] == "BUY" else px >= l["stop"]) else "tp")
            l["status"], l["closed_why"] = "closed", {"sl": "stop_loss", "tp": "target"}.get(reason, reason)
            l["exit"] = (d or {}).get("price")
            stopped = stopped or reason == "sl"
            self.note(sym, f"lot {l['lot']} closed in {self.venue} ({l['closed_why']}) at {l.get('exit')}", level="exit")
        self._save()
        if stopped and p.get("spike", True) and not any(x["status"] == "open" for x in st["legs"]):
            side = gone[0]["side"]
            nside = "SELL" if side == "BUY" else "BUY"
            if self._can_enter(st, p, now) and (nside == "BUY" or p.get("allow_short", True)):
                ref = st["zone"]["low"] if nside == "SELL" else st["zone"]["high"]
                self._enter(sym, st, bridge, cfg, p, nside, ref, now, "relay after the stop")

    # ---- sending
    def _send(self, sym, bridge, cfg, op, f, legs=(), new_entry=False, check=True) -> str | None:
        mode = cfg["mode"]
        if op in ("buy", "sell"):
            ok, why = guard(cfg, bridge, float(f.get("volume") or 0), new_entry, len(self._open_positions(bridge, cfg)),
                            self.venue, self.where)
        elif mode not in ("demo", "live"):
            ok, why = False, "dry run: order logged, not sent" if mode == "dry_run" else f"{self.venue} trading is off"
        else:                                          # closing / modifying: only needs a connected EA
            h = bridge.health()
            ok, why = h["connected"], h["message"]
        if not ok:
            self.log_event("blocked" if mode != "dry_run" else "dry_run", sym, op, False,
                      {"why": why, "order": f, "legs": list(legs), "trade": self._trade_info(sym, op, f, legs)})
            self.note(sym, f"{op} not sent: {why}", level="warn" if mode == "dry_run" else "error")
            st = self.syms.get(sym)
            if st and op in ("buy", "sell"):
                if mode == "dry_run":                  # follow the trade on paper
                    for l in st["legs"]:
                        if l["id"] in legs:
                            l["status"], l["fill"] = "open", l["ref"]
                else:
                    st["legs"] = [l for l in st["legs"] if l["id"] not in legs]
            return None
        cid = bridge.send(op, magic=int(cfg["magic"]), deviation=int(cfg["deviation"]), **f)
        self.pending[cid] = {"sym": sym, "op": op, "legs": list(legs), "at": time.time(), "order": f}
        self.log_event("order", sym, op, None, {"id": cid, "order": f, "legs": list(legs), "trade": self._trade_info(sym, op, f, legs)})
        return cid

    def _trade_info(self, sym, op, f, legs) -> dict:
        """What the order log shows: side, volume, price, SL and the target of every lot."""
        st = self.syms.get(sym) or {}
        ls = [l for l in st.get("legs", []) if l["id"] in legs]
        side = {"buy": "BUY", "sell": "SELL"}.get(op) or (("SELL" if ls[0]["side"] == "BUY" else "BUY") if ls and op == "close" else "")
        info = {"side": side, "volume": f.get("volume"), "symbol": f.get("symbol")}
        if ls:
            off = ls[0].get("off", 0.0)
            info.update(price=round(ls[0]["ref"] + off, 2) if op in ("buy", "sell") else None,
                        sl=round(ls[0]["stop"] + off, 2), mt5_sl=f.get("sl"),
                        targets=[round(l["target"] + off, 2) if l.get("target") is not None else None for l in ls],
                        lots=len(ls), why=ls[0].get("closed_why") if op == "close" else None)
        return info

    def _paper_exits(self, sym, st, bridge, cfg, p, px, now) -> None:
        """Dry run: nothing is in MT5, so stops / targets are followed on the Upstox price."""
        legs = [l for l in st["legs"] if l["status"] == "open"]
        if not legs:
            return
        side = legs[0]["side"]
        if p.get("spike", True) and ((side == "BUY" and px <= legs[0]["stop"]) or (side == "SELL" and px >= legs[0]["stop"])):
            for l in legs:
                l["status"], l["closed_why"], l["exit"] = "closed", "stop_loss", legs[0]["stop"]
            self.note(sym, f"[dry run] stop {legs[0]['stop']:.2f} hit", level="exit")
            nside = "SELL" if side == "BUY" else "BUY"
            if self._can_enter(st, p, now) and (nside == "BUY" or p.get("allow_short", True)):
                self._enter(sym, st, bridge, cfg, p, nside, legs[0]["stop"], now, "relay after the stop")
            self._save()
            return
        for l in legs:
            if l["target"] is not None and l["bar"] != st["bar"] and (px >= l["target"] if side == "BUY" else px <= l["target"]):
                l["status"], l["closed_why"], l["exit"] = "closed", "target", l["target"]
                self.note(sym, f"[dry run] lot {l['lot']} target {l['target']:.2f} hit", level="exit")
                self._save()

    def close_all(self, bridge: Bridge | None = None, cfg: dict | None = None, why: str = "admin") -> str | None:
        bridge = bridge or self.bridge_getter()
        cfg = cfg or self.configs_fn()[1]
        for st in self.syms.values():
            for l in st["legs"]:
                if l["status"] in ("open", "sending"):
                    l["status"], l["closed_why"] = "closed", why
            st["done"] = st["done"] or why
        self._save()
        if not bridge.health()["connected"]:
            return None
        cid = bridge.send("close_all", magic=int(cfg["magic"]), comment=why)
        self.pending[cid] = {"sym": "*", "op": "close_all", "legs": [], "at": time.time()}
        self.log_event("order", "*", "close_all", None, {"id": cid, "why": why})
        return cid

    def snapshot(self) -> dict:
        return {"day": str(self.day), "running": self.running, "halted": self.halted, "error": self.last_error,
                "symbols": self.syms, "events": self.events[-80:], "pending": len(self.pending)}


trader: AutoTrader | None = None


def start() -> AutoTrader:
    global trader
    if trader is None:
        trader = AutoTrader()
        threading.Thread(target=trader.run, name="mt5-auto", daemon=True).start()
    return trader
