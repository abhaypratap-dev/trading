"""The YONEXIS AI zone rules executed through recorded web flows.

WebExecutor stands where the MT5 bridge stands for yonexis.mt5.auto.AutoTrader: send(op) queues the flow of the
same name (buy / sell / close / close_all) on the browser worker and take(id) returns its result. A web terminal
reports nothing back, so the account is treated as netting (one position per symbol) and YONEXIS keeps the positions
it opened itself. Nothing sits at the broker as SL / TP: WebTrader closes at the stop (and each lot at its target)
on the live Upstox price.
"""
from __future__ import annotations

import functools
import json
import logging
import threading
import time
import uuid

from yonexis import db
from yonexis.mt5.auto import AutoTrader
from yonexis.openaiap import config, player, symbols
from yonexis.openaiap import browser as browser_mod
from yonexis.openaiap.browser import _login_page, browser

log = logging.getLogger("yonexis.openaiap")
STALE_S = 30            # an order that waited longer than this for the browser is not placed any more
STAGE_MAX_S = 900       # a prepared order panel older than this is not trusted: the whole flow runs again


def log_event(kind: str, symbol: str, op: str, ok: bool | None, detail: dict, latency_ms: float | None = None,
              site: str = "default") -> None:
    try:
        db.insert_row(db.openaiap_log, site=site, kind=kind, symbol=symbol, op=op, ok=None if ok is None else int(bool(ok)),
                      latency_ms=latency_ms, detail=json.dumps(detail, default=str)[:4000])
    except Exception:                                   # pragma: no cover - logging must never stop trading
        log.exception("openaiap log failed")


def order_vars(op: str, f: dict, yx_symbol: str | None = None, price: float | None = None, query: str | None = None) -> dict:
    side = {"buy": "BUY", "sell": "SELL"}.get(op) or str(f.get("side") or "")
    return {"symbol": f.get("symbol"), "query": query or f.get("symbol"), "qty": _qty(f.get("volume")), "side": side, "side_lower": side.lower(),
            "close_side": {"BUY": "SELL", "SELL": "BUY"}.get(side, ""), "sl": _px(f.get("sl")), "target": _px(f.get("tp")),
            "price": _px(price), "yonexis_symbol": yx_symbol}


def _qty(v):
    return None if v is None else (int(v) if float(v).is_integer() else v)


def _px(v):
    return None if v in (None, 0, 0.0) else round(float(v), 2)


class WebExecutor:
    kind = "web"

    def __init__(self, b=browser, pool=None, site: str = "default"):
        """pool: the per-symbol workers (None: every order on b - tests). site: which website's flows and settings."""
        self.b, self.pool, self.site = b, pool, site
        self.cfg = config.for_site(site)
        self._results: dict[str, dict] = {}
        self._lock = threading.RLock()
        self.symbols: list = []
        self.state_at = time.time()
        self._pos: list[dict] | None = None
        self._term: dict[str, str] = {}                       # web symbol -> the search text that shows it
        self._cache: dict = {"at": 0.0}

    def _c(self) -> dict:
        """Settings and missing flows, read at most once a second (AutoTrader asks many times per step)."""
        if time.time() - self._cache["at"] > 1:
            self._cache = {"at": time.time(), "cfg": self.cfg.get(), "missing": self.cfg.missing_flows()}
        return self._cache

    # ------------------------------------------------------------- what AutoTrader reads
    @property
    def state(self) -> dict:
        c = self._c()["cfg"]
        return {"account": {"trade_mode": "real" if c["account"] == "real" else "demo", "margin_mode": "netting"},
                "positions": self.positions(), "deals": [], "trade_allowed": True, "quotes": {}}

    def health(self) -> dict:
        miss = self._c()["missing"]
        if miss:
            return {"connected": False, "message": f"record the {', '.join(miss)} flow{'s' if len(miss) > 1 else ''} first"}
        ws = self.pool.all() if self.pool else [self.b]
        err = next((f"{w.name}: {w.error}" for w in ws if w.error), None)
        busy = [f"{w.name} {w.busy}" for w in ws if w.busy]
        if err:
            return {"connected": True, "message": f"ready · last flow failed ({err})"}
        return {"connected": True, "message": "ready" + (f" · running {', '.join(busy)}" if busy else "")}

    def ready(self) -> tuple[bool, str]:
        return True, ""

    # ------------------------------------------------------------- positions kept by YONEXIS
    def positions(self) -> list[dict]:
        if self._pos is None:
            self._pos = list((db.get_setting(self.cfg.key("positions")) or {}).get("list") or [])
        return [dict(p) for p in self._pos]

    def _set_positions(self, ps: list[dict]) -> None:
        self._pos = ps
        db.set_setting(self.cfg.key("positions"), {"list": ps})

    def _book(self, op: str, f: dict, price) -> int | None:
        with self._lock:                                      # workers book at the same time
            return self._book_locked(op, f, price)

    def _book_locked(self, op: str, f: dict, price) -> int | None:
        ps, sym, vol = self.positions(), f.get("symbol"), float(f.get("volume") or 0)
        mine = next((p for p in ps if p["symbol"] == sym), None)
        if op in ("buy", "sell"):
            if mine and mine["type"] != op:                       # an opposite order nets the position down
                mine["volume"] = round(mine["volume"] - vol, 8)
                if mine["volume"] <= 0:
                    ps.remove(mine)
            elif mine:
                mine["volume"] = round(mine["volume"] + vol, 8)
            else:
                mine = {"ticket": int(time.time() * 1000) % 10**9, "symbol": sym, "type": op, "volume": vol,
                        "price_open": price, "magic": int(f.get("magic") or self.cfg.get()["magic"]),
                        "opened": time.strftime("%H:%M:%S"), "comment": f.get("comment") or ""}
                ps.append(mine)
        elif op == "close" and mine:
            mine["volume"] = round(mine["volume"] - (vol or mine["volume"]), 8)
            if mine["volume"] <= 0:
                ps.remove(mine)
        elif op == "close_all":
            ps = []
        self._set_positions(ps)
        return mine["ticket"] if mine else None

    def forget(self) -> None:
        self._set_positions([])

    # ------------------------------------------------------------- commands
    def send(self, op: str, **f) -> str:
        cid = uuid.uuid4().hex[:12]
        at = time.time()
        if op in ("watch", "ping", "symbols", "modify"):        # nothing to do in a web terminal
            self._results[cid] = {"id": cid, "ok": True, "desc": f"{op}: not used by web flows"}
            return cid
        if op == "close_all" and not (self.cfg.flows().get("close_all") or {}).get("steps"):
            # no close_all flow: every position is closed by its own worker, all at the same time
            futs = [(p, self._worker(p["symbol"]).submit(self._close_one, p, dict(f), at)) for p in self.positions()]
            threading.Thread(target=self._gather, args=(cid, futs), daemon=True).start()
            return cid
        w = self._worker(f.get("symbol")) if op != "close_all" else self.b
        w.submit(self._job, w, cid, op, dict(f), at)
        return cid

    def _worker(self, web_symbol):
        return self.pool.for_symbol(web_symbol) if self.pool else self.b

    def _close_one(self, p: dict, f: dict, at: float) -> dict:
        """Worker thread: close one tracked position with the close (or opposite) flow."""
        dry = bool(f.pop("dry", False))
        if time.time() - at > STALE_S:
            return {"ok": False, "error": "waited too long for the browser", "symbol": p["symbol"]}
        w = self._worker(p["symbol"])
        _ensure_tab(w)
        one = self._flow(w, "close", {**f, "symbol": p["symbol"], "volume": p["volume"],
                                      "side": "BUY" if p["type"] == "buy" else "SELL"}, dry)
        if one["ok"] and not dry:
            self._book("close", {"symbol": p["symbol"], "volume": p["volume"]}, None)
        return {**one, "symbol": p["symbol"]}

    def _gather(self, cid: str, futs: list) -> None:
        flows = []
        for p, fut in futs:
            try:
                flows.append(fut.result(300))
            except Exception as e:
                flows.append({"ok": False, "error": str(e)[:300], "symbol": p["symbol"]})
        bad = [x for x in flows if not x.get("ok")]
        r = {"id": cid, "ok": not bad, "flows": flows, "ms": max([x.get("ms") or 0 for x in flows] or [0])}
        if bad:
            r["error"] = r["desc"] = "; ".join(f"{x.get('symbol')}: {x.get('error')}" for x in bad)[:500]
        with self._lock:
            self._results[cid] = r

    def _job(self, w, cid: str, op: str, f: dict, at: float) -> None:
        """Worker thread. f["dry"]: a test that stops before the final step and books nothing."""
        dry = bool(f.pop("dry", False))
        if time.time() - at > STALE_S:
            r = {"ok": False, "error": f"waited {time.time() - at:.0f}s for the browser - not placed"}
        else:
            _ensure_tab(w)
            if op == "close" and not f.get("side"):
                p = next((x for x in self.positions() if x["symbol"] == f.get("symbol")), None)
                f["side"] = ("BUY" if p["type"] == "buy" else "SELL") if p else ""
                f["volume"] = f.get("volume") or (p or {}).get("volume")
            r = self._fire(w, op, f) if not dry else None        # prepared panel: only the final click is left
            if r is None or (not r["ok"] and not r["final_ran"]):
                miss = r.get("error") if r else None
                r = self._flow(w, op, f, dry)
                if miss:
                    r["staged_failed"] = miss
            r["worker"] = getattr(w, "name", "main")
            if dry:
                pass
            elif r["ok"] and op != "close_all":
                r["position"] = self._book(op, f, r["price"])
            elif r["ok"]:
                self._book("close_all", f, None)
        r = {**r, "id": cid}
        if not r["ok"]:
            r.setdefault("desc", r.get("error"))
        with self._lock:
            self._results[cid] = r

    def _prep(self, op: str, f: dict) -> tuple:
        """Flow name, steps and variables of an order. Closing without a close flow = the opposite order."""
        fl = self.cfg.flows()
        name = op
        if op == "close" and not (fl.get("close") or {}).get("steps"):
            name = "sell" if f.get("side") == "BUY" else "buy"      # netting: closing = the opposite order
        mp = self.cfg.mapping()
        yx = next((k for k, v in mp.items() if v["web"] == f.get("symbol")), None)
        ltp = _ltp(yx)
        return name, (fl.get(name) or {}).get("steps"), order_vars(op, f, yx, ltp, (mp.get(yx) or {}).get("query")), ltp

    # ------------------------------------------------------------- prepared orders (stage now, fire at the trigger)
    def stage(self, op: str, **f) -> bool:
        """Prepare the next order of a symbol on its own worker: every step before the final one (row clicked, lot
        set, price loaded). At the trigger only the final click is left. Only for a worker that has one symbol."""
        if not self.pool or not f.get("symbol"):
            return False
        w = self._worker(f["symbol"])
        if len(self.pool.symbols_of(w)) != 1 or w.busy:
            return False
        w.submit(self._stage_job, w, op, dict(f))
        return True

    def _stage_job(self, w, op: str, f: dict) -> None:
        name, steps, vs, _ = self._prep(op, f)
        w.staged = None
        _ensure_tab(w)
        if not steps or not any(s.get("final") for s in steps) or not self._ensure_prepared(w, f.get("symbol")):
            return
        r = player.run(w, f"stage {name}", steps, vs, self.cfg.secrets(), True, float(self.cfg.get()["step_timeout_s"]))
        w.error = None                                   # a failed preparation is not an order error
        if r["ok"]:
            w.staged = {"name": name, "symbol": f.get("symbol"), "vars": vs, "at": time.time(), "ms": r.get("ms")}

    def _fire(self, w, op: str, f: dict) -> dict | None:
        """The order prepared on this worker: refill what changed (e.g. the quantity: a stop closes every lot), then
        the final step(s). None when nothing (or something else) was prepared."""
        st, w.staged = getattr(w, "staged", None), None
        name, steps, vs, ltp = self._prep(op, f)
        if not st or not steps or st["name"] != name or st["symbol"] != f.get("symbol") or time.time() - st["at"] > STAGE_MAX_S:
            return None
        sec = self.cfg.secrets()
        fi = next(i for i, s in enumerate(steps) if s.get("final"))

        def val(s, v):
            try:
                return player.render(s.get("value", ""), v, sec)
            except ValueError:
                return None
        refill = [s for s in steps[:fi] if s["action"] in ("fill", "type", "select") and val(s, vs) != val(s, st["vars"])]
        r = player.run(w, name, refill + steps[fi:], vs, sec, False, float(self.cfg.get()["step_timeout_s"]))
        r.update(staged=True, staged_s=round(time.time() - st["at"], 1), refilled=len(refill))
        r["price"] = r["vars"].get("fill_price") or r["vars"].get("price") or ltp
        r["vars_used"] = {k: v for k, v in vs.items() if v is not None}
        return r

    def warm(self) -> int:
        """Open every worker tab before the market (not at the first order); a worker with one symbol runs the
        optional prepare flow for it (e.g. filter the market watch to that symbol)."""
        if not self.pool:
            return 0
        n = 0
        for w in self.pool.needed():
            syms = self.pool.symbols_of(w)
            w.submit(self._warm_job, w, syms[0] if len(syms) == 1 else None)
            n += 1
        return n

    def ensure_warm(self) -> int:
        """Called by the running automations (at most every 30 s): every needed worker tab is open and shows its
        symbol - also after a restart, when the automation was already on. Only once the main tab is logged in."""
        if not self.pool or time.time() - getattr(self, "_warm_at", 0) < 30:
            return 0
        self._warm_at = time.time()
        u = self.pool.main.last_url or ""
        if not u.startswith("http") or _login_page(u):
            return 0
        has_prepare = bool((self.cfg.flows().get("prepare") or {}).get("steps"))
        n = 0
        for w in self.pool.needed():
            syms = self.pool.symbols_of(w)
            one = syms[0] if len(syms) == 1 else None
            if w.busy or getattr(w, "staged", None):
                continue
            if w.page is None or (has_prepare and one and w.prepared_for != one):
                w.submit(self._warm_job, w, one)
                n += 1
        return n

    def _warm_job(self, w, web_symbol) -> None:
        w._page()
        self._prepare(w, web_symbol)

    def _prepare(self, w, web_symbol) -> bool:
        """Run the prepare flow for this symbol on this tab (e.g. filter the market watch to it) and check the symbol
        is on the screen afterwards. The full name is typed first; if the site shows nothing for it, the saved search
        text and shorter searches are tried. False: the symbol could not be put on the screen."""
        steps = (self.cfg.flows().get("prepare") or {}).get("steps")
        if not steps or not web_symbol:
            return True
        _, _, vs, _ = self._prep("buy", {"symbol": web_symbol})
        yx = vs.get("yonexis_symbol")
        terms = [web_symbol] + [t for t in symbols.search_terms(yx or web_symbol, vs.get("query")) if t != web_symbol][:4]
        known = self._term.get(web_symbol)                    # the search that worked last time goes first
        if known in terms:
            terms = [known] + [t for t in terms if t != known]
        tmo, sec = float(self.cfg.get()["step_timeout_s"]), self.cfg.secrets()
        err = None
        for term in terms:
            r = player.run(w, "prepare", steps, {**vs, "symbol": term, "query": term}, sec, False, tmo)
            if r["ok"] and player.shows(w._page(), web_symbol, 3):
                w.error, w.prepared_for = None, web_symbol
                self._term[web_symbol] = term
                return True
            err = r.get("error") or f"{web_symbol} not on the screen"
        w.prepared_for = None
        w.error = f"prepare: {err} (searched: {', '.join(terms)})"
        return False

    def _ensure_prepared(self, w, web_symbol) -> bool:
        """A flow recorded on a prepared screen (e.g. 'click row 1' after filtering) needs that screen for THIS symbol."""
        if not (self.cfg.flows().get("prepare") or {}).get("steps") or not web_symbol:
            return True
        if getattr(w, "prepared_for", None) == web_symbol:
            return True
        return self._prepare(w, web_symbol)

    def _flow(self, w, op: str, f: dict, dry: bool = False) -> dict:
        c, fl = self.cfg.get(), self.cfg.flows()
        name, steps, vs, ltp = self._prep(op, f)
        if not steps:
            return {"ok": False, "error": f"no {op} flow recorded", "price": None, "final_ran": False, "vars": {}}
        sec, tmo, sym = self.cfg.secrets(), float(c["step_timeout_s"]), f.get("symbol")

        def attempt() -> dict:
            if not self._ensure_prepared(w, sym):            # the symbol is not on the screen: never click
                return {"ok": False, "error": w.error, "final_ran": False, "steps": [], "vars": {}, "flow": name}
            return player.run(w, name, steps, vs, sec, dry, tmo)
        r = attempt()
        # failed before the order was committed: 1) refresh the page (a stale price feed) and retry,
        # 2) log in again (on the main tab; workers take its new login) and retry. Never after a final step.
        if not r["ok"] and not r["final_ran"] and c.get("relogin", True):
            first = r.get("error")
            try:
                w._page().reload(wait_until="domcontentloaded", timeout=30000)
                w._settle(w._page())
                w.prepared_for = None
                r = {**attempt(), "refreshed": first}
            except Exception as e:
                r["refreshed"] = f"refresh failed: {e}"
            if not r["ok"] and not r["final_ran"] and (fl.get("login") or {}).get("steps"):
                main = getattr(w, "main", None) or w
                lg = (main.do(player.run, main, "login", fl["login"]["steps"], vs, sec, False, tmo, timeout=120)
                      if main is not w else player.run(w, "login", fl["login"]["steps"], vs, sec, False, tmo))
                r["relogin"] = {"ok": lg["ok"], "error": lg.get("error")}
                if lg["ok"]:
                    if self.pool:                                 # every other worker takes the new login too
                        for o in self.pool.workers.values():
                            if o is not w:
                                o.submit(o.resync)
                    if main is not w:
                        w.resync()
                    w.prepared_for = None
                    r = {**attempt(), "relogin": r["relogin"], "refreshed": first}
        r["price"] = r["vars"].get("fill_price") or r["vars"].get("price") or ltp
        r["vars_used"] = {k: v for k, v in vs.items() if v is not None}
        return r

    def take(self, cid: str) -> dict | None:
        with self._lock:
            r = self._results.pop(cid, None)
            while len(self._results) > 500:
                self._results.pop(next(iter(self._results)))
        if r is not None and r.get("ms") is not None:
            r["latency_ms"] = r["ms"]
        return r

    def call(self, op: str, timeout: float = 120, **f) -> dict:
        cid = self.send(op, **f)
        end = time.time() + timeout
        while time.time() < end:
            r = self.take(cid)
            if r is not None:
                return r
            time.sleep(0.05)
        return {"id": cid, "ok": False, "error": f"the {op} flow did not finish within {timeout:.0f}s"}


def _ensure_tab(w) -> None:
    """A worker tab that is blank, logged out or on another site is opened again from the main tab first."""
    if getattr(w, "main", None) is not None:
        try:
            w.ensure_like_main()
        except Exception as e:
            w.error = f"tab: {e}"


def _ltp(sym: str | None):
    if not sym:
        return None
    try:
        from yonexis.live import engine as live_mod
        e = live_mod.engine
        return (e.quote(sym, strict=True) or {}).get("ltp") if e else None
    except Exception:
        return None


class WebTrader(AutoTrader):
    """AutoTrader on web flows. Spike mode: the stop is watched here (a web order carries no SL).
    Each symbol's next order is prepared on its worker ahead of the trigger (stage), so at the trigger only the final
    click is left: no position -> the entry on the side price is nearer to; a position -> its exit (one lot's
    quantity; a stop refills the quantity of every lot)."""

    def step(self) -> None:
        super().step()
        a, cfg = self._configs()[0], self._configs()[1]
        if a.get("enabled") and cfg.get("mode") in ("demo", "live") and hasattr(self.bridge_getter(), "ensure_warm"):
            try:
                self.bridge_getter().ensure_warm()               # worker tabs open + on their symbol, even after a restart
            except Exception as e:
                self.last_error = f"workers: {e}"

    def _symbol(self, sym, eng, bridge, cfg, p, now) -> None:
        super()._symbol(sym, eng, bridge, cfg, p, now)
        try:
            self._stage(sym, bridge, cfg, p, now)
        except Exception as e:                           # preparing is an optimisation: never stop trading for it
            self.last_error = f"stage {sym}: {e}"

    def _stage(self, sym, bridge, cfg, p, now) -> None:
        st = self.syms.get(sym)
        if cfg["mode"] not in ("demo", "live") or not st or st.get("skip") or st.get("done") or not st.get("zone") \
                or not hasattr(bridge, "stage") or st.get("last_px") is None:
            return
        legs = st["legs"]
        if any(l["status"] == "sending" for l in legs) or time.time() - st.get("staged_at", 0) < 3:
            return
        mp = self._map.get(sym) or {}
        open_ = [l for l in legs if l["status"] == "open"]
        if open_:
            want = ("close", open_[0]["side"], open_[0]["volume"])
        elif self._can_enter(st, p, now):
            hi, lo = st["zone"]["high"], st["zone"]["low"]
            px, mid, band = st["last_px"], (st["zone"]["high"] + st["zone"]["low"]) / 2, (hi - lo) * 0.1
            prev = (st.get("staged") or [None])[0]
            side = "buy" if px >= mid else "sell"
            if prev in ("buy", "sell") and prev != side and abs(px - mid) < band:
                side = prev                              # no flip-flop while price sits in the middle of the zone
            if side == "sell" and not p.get("allow_short", True):
                side = "buy"
            lots = len(p.get("lot_targets") or [p.get("target_mult", 1.5)])
            want = (side, None, round(float(mp.get("volume") or 1) * lots, 8))
        else:
            return
        if tuple(st.get("staged") or ()) == want:
            return
        st["staged"], st["staged_at"] = list(want), time.time()
        f = {"symbol": mp.get("mt5"), "volume": want[2], "magic": int(cfg["magic"])}
        if want[0] == "close":
            f["side"] = want[1]
        bridge.stage(want[0], **f)

    def close_all(self, bridge=None, cfg=None, why: str = "admin") -> str | None:
        """Close only THIS automation's open lots (other automations of the website keep theirs)."""
        bridge = bridge or self.bridge_getter()
        cfg = cfg or self._configs()[1]
        sent = None
        for sym, st in self.syms.items():
            if any(l["status"] in ("open", "sending") for l in st["legs"]):
                self.note(sym, f"{why}: closing")
                self._close_legs(sym, st, bridge, cfg, why)
                sent = sent or "sent"
            st["done"] = st["done"] or why
        self._save()
        return sent

    def _send(self, sym, bridge, cfg, op, f, legs=(), new_entry=False, check=True):
        st = self.syms.get(sym)
        if st:
            st["staged"] = None                          # after this order the next one is prepared again
        return super()._send(sym, bridge, cfg, op, f, legs, new_entry, check)

    def _netting_targets(self, sym, st, bridge, cfg, px) -> None:
        p = self._configs()[0]["params"]
        legs = [l for l in st["legs"] if l["status"] == "open"]
        if legs and p.get("spike", True):
            side, stop = legs[0]["side"], legs[0]["stop"]
            if (px <= stop) if side == "BUY" else (px >= stop):
                self._stop_and_relay(sym, st, bridge, cfg, p, stop, self.clock(), f"price reached the stop {stop:.2f}")
                return
        super()._netting_targets(sym, st, bridge, cfg, px)


_EXECUTORS: dict[str, WebExecutor] = {}
traders: dict[tuple[str, str], WebTrader] = {}          # (website, automation) -> its loop


def get_executor(site: str = "default") -> WebExecutor:
    if site not in _EXECUTORS:
        b, p = browser_mod.get(site)
        _EXECUTORS[site] = WebExecutor(b, p, site)
    return _EXECUTORS[site]


executor = get_executor("default")
trader: WebTrader | None = None                          # the default website's first automation (older callers)


def get_trader(site: str = "default", aid: str = config.MAIN_AUTO) -> WebTrader | None:
    return traders.get((site, aid))


def start(site: str = "default", aid: str | None = None) -> WebTrader:
    """The automation loop(s) of one website: one per automation (each idle until enabled). Returns the one asked
    for (aid), else the first."""
    global trader
    cfg, ex = config.for_site(site), get_executor(site)
    for a in cfg.automation_ids():
        if (site, a) not in traders:
            t = WebTrader(bridge_getter=lambda: ex, configs=lambda a=a: (cfg.auto(aid=a), cfg.get(), cfg.mapping()),
                          state_key=cfg.key(config.state_key_name(a)), log_fn=functools.partial(log_event, site=site),
                          venue="OpenAIAP", result_timeout=180, where="OpenAIAP > Settings")
            t.site, t.aid = site, a
            traders[(site, a)] = t
            threading.Thread(target=t.run, name=f"openaiap-auto-{site}-{a}", daemon=True).start()
    if site == "default":
        trader = traders[(site, config.MAIN_AUTO)]
    return traders[(site, aid or config.MAIN_AUTO)]


def start_all() -> None:
    for s in config.sites():
        start(s["id"])


def stop_automation(site: str, aid: str) -> None:
    t = traders.pop((site, aid), None)
    if t:
        t.stop()


def stop_site(site: str) -> None:
    for key in [k for k in traders if k[0] == site]:
        traders.pop(key).stop()
    _EXECUTORS.pop(site, None)
    browser_mod.drop(site)
