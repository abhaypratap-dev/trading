"""/api/openaiap/* - the OpenAIAP tab: browser + recorder, flows, secrets, settings, mapping, automation, log."""
from __future__ import annotations

import threading
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from yonexis import db
from yonexis.mt5.config import LIVE_PHRASE
from yonexis.openaiap import browser as browser_mod, config, executor, player, symbols
from yonexis.openaiap.browser import PlaywrightMissing, _login_page

router = APIRouter(prefix="/api/openaiap")


def _live():
    from yonexis.live import engine as live_mod
    return live_mod.engine


def _err(e: Exception) -> HTTPException:
    return HTTPException(400, str(e).split("\n")[0][:400])


class _Site:
    """Everything of one website: settings (cfg), main browser (b), workers (pool), executor (ex), log."""

    def __init__(self, site: str):
        if site not in config.site_ids():
            raise HTTPException(404, f"no website {site!r}")
        self.id = site
        self.cfg = config.for_site(site)
        self.b, self.pool = browser_mod.get(site)
        self.ex = executor.get_executor(site)

    def log(self, *a, **k):
        executor.log_event(*a, **k, site=self.id)

    def any_on(self) -> str | None:
        """Name of an enabled automation of this website (they share its browser), else None."""
        return next((a["name"] for a in self.cfg.automations() if a["enabled"]), None)


def R(site: str) -> _Site:
    return _Site(site or config.DEFAULT_SITE)


# ---------------------------------------------------------------------- websites
class SiteB(BaseModel):
    name: str
    copy_from: str | None = None         # start with another website's settings (not its flows / secrets / mapping)


@router.get("/sites")
def sites_get():
    out = []
    for r in config.sites():
        c = config.for_site(r["id"])
        autos = c.automations()
        a = {"enabled": any(x["enabled"] for x in autos), "symbols": c.all_symbols()}
        b, _ = browser_mod.get(r["id"]) if r["id"] in browser_mod._SITES else (None, None)
        out.append({**r, "mode": c.get()["mode"], "start_url": c.get()["start_url"], "auto": a["enabled"],
                    "symbols": len(a["symbols"]), "automations": len(autos), "flows": sorted(c.flows()), "browser": bool(b and b.page is not None),
                    "positions": len(executor.get_executor(r["id"]).positions()) if r["id"] in executor._EXECUTORS else 0})
    return out


@router.post("/sites")
def sites_add(b: SiteB):
    try:
        r = config.add_site(b.name)
    except ValueError as e:
        raise _err(e)
    if b.copy_from and b.copy_from in config.site_ids():
        src = config.get(site=b.copy_from)
        config.save({k: v for k, v in src.items() if k not in ("live_confirm", "start_url")} | {"mode": "off"}, site=r["id"])
        src = config.auto(site=b.copy_from)
        config.save_auto({"params": src["params"], "watchlist": src["watchlist"], "symbols": src["symbols"], "enabled": False}, site=r["id"])
    executor.start(r["id"])
    db.log("admin", "openaiap_site_add", r)
    return r


@router.put("/sites/{sid}")
def sites_rename(sid: str, b: SiteB):
    try:
        return config.rename_site(sid, b.name)
    except ValueError as e:
        raise _err(e)


@router.delete("/sites/{sid}")
def sites_remove(sid: str):
    if sid not in config.site_ids():
        raise HTTPException(404, f"no website {sid!r}")
    if any(x["enabled"] for x in config.automations(site=sid)):
        raise HTTPException(400, "stop that website's automations first")
    if sid in executor._EXECUTORS and executor.get_executor(sid).positions():
        raise HTTPException(400, "that website still has tracked positions - close or forget them first")
    try:
        rows = config.remove_site(sid)
    except ValueError as e:
        raise _err(e)
    executor.stop_site(sid)
    db.log("admin", "openaiap_site_remove", {"id": sid})
    return rows


# ---------------------------------------------------------------------- status
@router.get("/status")
def status(site: str = "default", aid: str = "main"):
    S = R(site)
    if aid not in S.cfg.automation_ids():
        aid = config.MAIN_AUTO
    ex, t = S.ex, executor.get_trader(site, aid)
    a = S.cfg.auto(aid=aid)
    eng = _live()
    mp = S.cfg.mapping()
    quotes = [{"symbol": s, "web": m["web"], "query": m["query"], "volume": m["volume"],
               "ltp": (eng.quote(s, strict=True) or {}).get("ltp") if eng else None} for s, m in mp.items()]
    fl = S.cfg.flows()
    pnl = None
    try:
        from yonexis.mt5 import pnl as pnl_mod
        pnl = pnl_mod.day_pnl(eng, mp, a, {}, int(S.cfg.get()["magic"]))
    except Exception:
        pass
    mp_rev = {}
    for s_, m_ in mp.items():
        if s_ in S.cfg.worker_symbols():
            mp_rev.setdefault(S.pool.index(m_["web"]), []).append(s_)
    workers = [{**w, "symbols": mp_rev.get(0 if w["name"] == "main" else int(w["name"][1:]), [])} for w in S.pool.info()]
    return {"config": S.cfg.public(), "health": ex.health(), "browser": S.b.info(), "auto": a, "workers": workers,
            "flows": {k: {"steps": len(v.get("steps") or []), "final": any(s.get("final") for s in v.get("steps") or [])} for k, v in fl.items()},
            "missing_flows": S.cfg.missing_flows(), "secrets": S.cfg.secret_names(), "positions": ex.positions(),
            "quotes": quotes, "trader": t.snapshot() if t else None, "pnl": pnl, "vars": S.cfg.VARS, "actions": S.cfg.ACTIONS,
            "aid": aid, "automations": [{"id": x["id"], "name": x["name"], "enabled": x["enabled"], "symbols": len(x["symbols"]),
                                         "symbol_list": x["symbols"], "watchlist": x["watchlist"]} for x in S.cfg.automations()]}


class Cfg(BaseModel):
    mode: str | None = None
    account: str | None = None
    live_confirm: str | None = None
    start_url: str | None = None
    max_volume: float | None = None
    max_positions: int | None = None
    step_timeout_s: float | None = None
    relogin: bool | None = None
    workers: int | None = None


@router.put("/config")
def config_put(b: Cfg, site: str = "default"):
    S = R(site)
    ch = {k: v for k, v in b.model_dump().items() if v is not None}
    if "mode" in ch and ch["mode"] not in S.cfg.MODES:
        raise HTTPException(400, f"mode must be one of {S.cfg.MODES}")
    if "account" in ch and ch["account"] not in ("demo", "real"):
        raise HTTPException(400, "account must be demo or real")
    if ch.get("mode") == "live":
        if ch.get("live_confirm", S.cfg.get().get("live_confirm")) != LIVE_PHRASE:
            raise HTTPException(400, f"to trade real money type {LIVE_PHRASE} in the confirmation box")
    elif "mode" in ch:
        ch["live_confirm"] = ""
    if "max_volume" in ch and not (0 < ch["max_volume"] <= 100000):
        raise HTTPException(400, "max quantity per order must be above 0")
    if "workers" in ch and not (1 <= ch["workers"] <= 20):
        raise HTTPException(400, "parallel workers: 1-20")
    if "step_timeout_s" in ch and not (1 <= ch["step_timeout_s"] <= 60):
        raise HTTPException(400, "step timeout must be 1-60 s")
    c = S.cfg.save(ch)
    db.log("admin", "openaiap_config", {k: v for k, v in ch.items() if k != "live_confirm"})
    return {**c, "live_confirm": c.get("live_confirm") == LIVE_PHRASE}


# ---------------------------------------------------------------------- browser + recorder
class StartB(BaseModel):
    url: str | None = None


@router.post("/browser/start")
def browser_start(b: StartB, site: str = "default"):
    S = R(site)
    url = (b.url or S.cfg.get()["start_url"] or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    try:
        return S.b.start(url)
    except PlaywrightMissing as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise _err(e)


@router.post("/browser/stop")
def browser_stop(site: str = "default"):
    S = R(site)
    S.b.record_stop()
    S.pool.stop_workers()
    S.b.stop()
    return S.b.info()


@router.get("/browser/screen")
def browser_screen(w: int = 0, site: str = "default"):
    """w: 0 = the main tab (recorder), 1.. = a worker tab."""
    S = R(site)
    if w and w not in S.pool.workers:
        return Response(status_code=204)
    j = S.pool.get(w).screen()
    if not j:
        return Response(status_code=204)
    return Response(j, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.post("/browser/act")
def browser_act(a: dict, site: str = "default"):
    S = R(site)
    try:
        r = S.b.act(a)
    except PlaywrightMissing as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise _err(e)
    return {**r, "browser": S.b.info(), "recorded": (S.b.recording or {}).get("steps")}


class Rec(BaseModel):
    flow: str
    samples: dict = {}


@router.post("/record/start")
def record_start(b: Rec, site: str = "default"):
    S = R(site)
    if S.any_on():
        raise HTTPException(400, f"stop the automation ({S.any_on()}) before recording - both use the same browser")
    S.b.record_start(b.flow.strip().lower(), b.samples)
    return S.b.info()


@router.post("/record/stop")
def record_stop(site: str = "default"):
    S = R(site)
    return S.b.record_stop() or {}


@router.get("/record")
def record_get(site: str = "default"):
    S = R(site)
    return S.b.recording or {}


# ---------------------------------------------------------------------- flows
@router.get("/flows")
def flows_get(site: str = "default"):
    S = R(site)
    return S.cfg.flows()


class Flow(BaseModel):
    steps: list[dict]
    start_url: str | None = None


@router.put("/flows/{name}")
def flow_put(name: str, b: Flow, site: str = "default"):
    S = R(site)
    try:
        f = S.cfg.save_flow(name, b.steps, b.start_url)
    except ValueError as e:
        raise _err(e)
    db.log("admin", "openaiap_flow", {"flow": name, "steps": len(f["steps"])})
    return f


@router.delete("/flows/{name}")
def flow_delete(name: str, site: str = "default"):
    S = R(site)
    S.cfg.delete_flow(name)
    db.log("admin", "openaiap_flow_delete", {"flow": name})
    return {"deleted": name}


class RunB(BaseModel):
    vars: dict = {}
    dry: bool = True
    confirm: bool = False


@router.post("/flows/{name}/run")
def flow_run(name: str, b: RunB, site: str = "default"):
    """Test a flow. dry=True stops before the steps marked final; a real run of an order flow needs confirm."""
    S = R(site)
    fl = S.cfg.flows().get(name)
    if not fl or not fl.get("steps"):
        raise HTTPException(404, f"no {name} flow")
    if not b.dry and name in ("buy", "sell", "close", "close_all") and not b.confirm:
        raise HTTPException(400, "a real order flow run needs confirm")
    if S.b.recording:
        raise HTTPException(400, "stop recording first")
    vs = {k: None for k in S.cfg.VARS}
    vs.update({k: v for k, v in (b.vars or {}).items() if v not in (None, "")})
    side = {"buy": "BUY", "sell": "SELL"}.get(name) or str(vs.get("side") or "").upper()
    vs.update(side=side, side_lower=side.lower(), close_side={"BUY": "SELL", "SELL": "BUY"}.get(side, ""))
    c = S.cfg.get()
    try:
        def job():
            if name in ("buy", "sell", "close") and vs.get("symbol") and not S.ex._ensure_prepared(S.b, vs["symbol"]):
                return {"flow": name, "ok": False, "error": S.b.error, "steps": [], "vars": {}, "final_ran": False, "dry": b.dry}
            out = player.run(S.b, name, fl["steps"], vs, S.cfg.secrets(), b.dry, float(c["step_timeout_s"]))
            if name == "login" and out["ok"]:
                end = time.time() + 8                                # the site moves past its login page a moment later
                while _login_page(S.b._page().url) and time.time() < end:
                    S.b._page().wait_for_timeout(300)
                S.b.last_url = S.b._page().url
                out["landed_on"] = S.b.last_url
                out["workers_resynced"] = S.pool.resync_all()      # every worker tab takes this login
                if any(x["enabled"] for x in S.cfg.automations()) and c["mode"] != "dry_run":
                    threading.Thread(target=S.ex.warm, daemon=True).start()   # an automation is on: open its tabs now
            S.b.prepared_for = vs.get("symbol") if name == "prepare" and out["ok"] else \
                (None if name in ("prepare", "symbols", "login") else S.b.prepared_for)
            return out
        r = S.b.submit(job).result(240)
    except Exception as e:
        raise _err(e)
    S.log("test", vs.get("yonexis_symbol") or vs.get("symbol") or "*", name, r["ok"],
          {"result": r, "dry": b.dry, "vars": {k: v for k, v in vs.items() if v is not None}}, r.get("ms"))
    return r


# ---------------------------------------------------------------------- secrets
@router.get("/secrets")
def secrets_get(site: str = "default"):
    S = R(site)
    return S.cfg.secret_names()


@router.put("/secrets")
def secrets_put(body: dict, site: str = "default"):
    S = R(site)
    try:
        names = S.cfg.save_secrets(body)
    except ValueError as e:
        raise _err(e)
    db.log("admin", "openaiap_secrets", {"names": names})
    return names


# ---------------------------------------------------------------------- symbols from the website
class Syms(BaseModel):
    symbols: list[str]


@router.post("/symbols/fetch")
def symbols_fetch(b: Syms, site: str = "default"):
    """Search the website for each symbol with the recorded symbols flow (background; poll GET /symbols)."""
    S = R(site)
    if S.any_on():
        raise HTTPException(400, f"stop the automation ({S.any_on()}) first - fetching uses the same browser")
    if S.b.recording:
        raise HTTPException(400, "stop recording first")
    if not b.symbols:
        raise HTTPException(400, "choose the symbols first")
    try:
        return symbols.fetch([s.upper() for s in b.symbols], site)
    except RuntimeError as e:
        raise _err(e)


@router.get("/symbols")
def symbols_get(site: str = "default"):
    S = R(site)
    f = symbols.found(site)
    return {"job": symbols.job(site), "queries": {k: len(v) for k, v in f.items()}, "lookups": symbols.lookups(site),
            "count": len({n for v in f.values() for n in v}), "at": symbols._store(site).get("at")}


@router.post("/symbols/suggest")
def symbols_suggest(b: Syms, site: str = "default"):
    """Best website name per YONEXIS symbol (Upstox decides the contract: base name, futures or not, near month)."""
    S = R(site)
    return symbols.suggest(b.symbols, symbols.found(site), site)


# ---------------------------------------------------------------------- mapping + automation
@router.get("/mapping")
def mapping_get(site: str = "default"):
    S = R(site)
    return {k: {"web": v["web"], "query": v["query"], "volume": v["volume"]} for k, v in S.cfg.mapping().items()}


@router.put("/mapping")
def mapping_put(body: dict, site: str = "default"):
    S = R(site)
    m = S.cfg.save_mapping(body.get("mapping") or {})
    db.log("admin", "openaiap_mapping", {"symbols": len(m)})
    return m


class Auto(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    watchlist: str | None = None
    symbols: list[str] | None = None
    params: dict | None = None


@router.get("/auto")
def auto_get(site: str = "default", aid: str = "main"):
    S = R(site)
    return S.cfg.auto(aid=aid)


@router.put("/auto")
def auto_put(b: Auto, site: str = "default", aid: str = "main"):
    S = R(site)
    if aid not in S.cfg.automation_ids():
        raise HTTPException(404, f"no automation {aid!r}")
    from yonexis import zone_lab
    ch = {k: v for k, v in b.model_dump().items() if v is not None}
    if "symbols" in ch:
        ch["symbols"] = sorted({s.strip().upper() for s in ch["symbols"] if s.strip()})
    if ch.get("watchlist") and ch["watchlist"] not in (db.get_setting("named_watchlists") or {}):
        raise HTTPException(400, f"no watchlist called {ch['watchlist']}")
    if "name" in ch:
        ch["name"] = ch["name"].strip()[:40]
    if "params" in ch:
        p = ch["params"]
        lt = [float(x) for x in (p.get("lot_targets") or []) if x not in (None, "")]
        if lt and any(not (0.1 <= x <= 50) for x in lt):
            raise HTTPException(400, "targets must be between 0.1 and 50")
        if lt:
            p["lot_targets"], p["target_mult"] = lt[:10], lt[0]
        if p.get("target_base") and p["target_base"] not in zone_lab.TARGET_BASES:
            raise HTTPException(400, "target multiplier must be zone or iz")
        for k in ("entry_cutoff", "square_off"):
            if p.get(k):
                hh, _, mm = str(p[k]).partition(":")
                if not (hh.isdigit() and mm.isdigit() and 0 <= int(hh) < 24 and 0 <= int(mm) < 60):
                    raise HTTPException(400, f"{k.replace('_', ' ')} must be HH:MM")
                p[k] = f"{int(hh):02d}:{int(mm):02d}"
        if p.get("max_cycles") is not None and not (1 <= int(p["max_cycles"]) <= 10):
            raise HTTPException(400, "entries per day must be 1-10")
    new = {**S.cfg.auto(aid=aid), **ch}
    if new.get("watchlist"):
        new["symbols"] = config._watchlist(new["watchlist"]) or []
    if new.get("enabled"):
        mine = set(new["symbols"])
        clash = {s: o for s, o in S.cfg.symbol_owner().items() if s in mine and o != aid}
        if clash:
            names = {x["id"]: x["name"] for x in S.cfg.automations()}
            raise HTTPException(400, "on one website a symbol can be traded by one automation only (the website nets "
                                     "positions): " + ", ".join(f"{s} is in {names.get(o, o)}" for s, o in list(clash.items())[:10]))
        missing = [s for s in new["symbols"] if s not in S.cfg.mapping()]
        if not new["symbols"]:
            raise HTTPException(400, "choose the symbols to automate")
        if missing:
            raise HTTPException(400, f"map these to the web terminal's symbol first (Symbol mapping): {', '.join(missing[:20])}")
        if S.cfg.get()["mode"] == "off":
            raise HTTPException(400, "OpenAIAP trading is off - choose Dry run, Demo or Live in Settings first")
        if S.cfg.get()["mode"] != "dry_run" and S.cfg.missing_flows():
            raise HTTPException(400, f"record these flows first: {', '.join(S.cfg.missing_flows())}")
        if S.b.recording:
            raise HTTPException(400, "stop recording first - automation and recorder share the browser")
    a = S.cfg.save_auto(ch, aid=aid)
    eng = _live()
    if a["enabled"] and eng:
        for s in a["symbols"]:
            try:
                eng.add_symbol(s)
            except Exception:
                pass
    executor.start(site, aid)
    try:
        S.pool.prune()                                     # tabs of symbols no running automation trades: closed
        if a["enabled"] and S.cfg.get()["mode"] != "dry_run":
            S.ex.warm()                                    # open the worker tabs now, not at the first order
    except Exception:
        pass
    db.log("admin", "openaiap_auto", {"site": site, "automation": aid, "enabled": a["enabled"], "symbols": len(a["symbols"]), "params": a["params"]})
    return a


class TestOrder(BaseModel):
    symbol: str
    side: str = "BUY"            # BUY | SELL | CLOSE
    dry: bool = True
    confirm: bool = False


@router.post("/test-order")
def test_order(b: TestOrder, site: str = "default"):
    """One order for a mapped symbol through the same path the automation uses (mapping -> variables -> flow).
    dry=True stops before the final step and books nothing; a real one needs confirm."""
    S = R(site)
    sym, side = b.symbol.strip().upper(), b.side.strip().upper()
    m = S.cfg.mapping().get(sym)
    if not m:
        raise HTTPException(400, f"{sym} is not mapped to a web symbol (Symbol mapping)")
    if side not in ("BUY", "SELL", "CLOSE"):
        raise HTTPException(400, "side must be BUY, SELL or CLOSE")
    if not b.dry and not b.confirm:
        raise HTTPException(400, "a real test order needs confirm")
    if S.b.recording:
        raise HTTPException(400, "stop recording first")
    if S.any_on():
        raise HTTPException(400, f"stop the automation ({S.any_on()}) first - tests use the same browser")
    ex = S.ex
    op = side.lower()
    f = {"symbol": m["web"], "volume": m["volume"], "magic": int(S.cfg.get()["magic"]), "comment": "YX test", "dry": b.dry}
    if op == "close":
        pos = next((p for p in ex.positions() if p["symbol"] == m["web"]), None)
        if not pos and not b.dry:
            raise HTTPException(400, f"no tracked position in {m['web']} to close")
        f.update(side="BUY" if (pos or {}).get("type", "buy") == "buy" else "SELL", volume=(pos or {}).get("volume") or m["volume"])
    r = ex.call(op, timeout=180, **f)
    S.log("test", sym, op, r.get("ok"), {"result": r, "dry": b.dry}, r.get("latency_ms"))
    db.log("admin", "openaiap_test_order", {"symbol": sym, "side": side, "dry": b.dry, "ok": r.get("ok")})
    return {**r, "symbol": sym, "web": m["web"], "qty": f["volume"], "dry": b.dry}


class NewAuto(BaseModel):
    name: str
    copy_from: str | None = None             # an automation of the same website whose strategy settings to start with


@router.post("/autos")
def autos_add(b: NewAuto, site: str = "default"):
    S = R(site)
    try:
        a = S.cfg.add_automation(b.name, b.copy_from)
    except ValueError as e:
        raise _err(e)
    executor.start(site, a["id"])
    db.log("admin", "openaiap_automation_add", {"site": site, "automation": a["id"], "name": a["name"]})
    return a


@router.delete("/autos/{aid}")
def autos_remove(aid: str, site: str = "default"):
    S = R(site)
    if aid not in S.cfg.automation_ids():
        raise HTTPException(404, f"no automation {aid!r}")
    t = executor.get_trader(site, aid)
    if S.cfg.auto(aid=aid)["enabled"] or (t and any(l["status"] in ("open", "sending") for st in t.syms.values() for l in st["legs"])):
        raise HTTPException(400, "stop it (and close its positions) first")
    try:
        S.cfg.remove_automation(aid)
    except ValueError as e:
        raise _err(e)
    executor.stop_automation(site, aid)
    db.log("admin", "openaiap_automation_remove", {"site": site, "automation": aid})
    return {"removed": aid}


@router.post("/auto/stop-close")
def auto_stop_close(site: str = "default", aid: str = "main"):
    """Stop one automation and close only its open lots."""
    S = R(site)
    S.cfg.save_auto({"enabled": False}, aid=aid)
    sent = executor.start(site, aid).close_all(why="stopped from the dashboard")
    db.log("admin", "openaiap_automation_stop_close", {"site": site, "automation": aid, "sent": bool(sent)})
    return {"stopped": True, "close_sent": bool(sent)}


@router.get("/overview")
def overview():
    """Every automation of every website (and the MT5 one): for the Automations tab."""
    from datetime import datetime
    now = datetime.now()
    market = now.weekday() < 5 and (9, 15) <= (now.hour, now.minute) <= (15, 30)
    eng = _live()
    rows = []
    for site in config.sites():
        S = R(site["id"])
        c, mp, pos = S.cfg.get(), S.cfg.mapping(), S.ex.positions()
        ws = S.pool.info()
        for a in S.cfg.automations():
            t = executor.get_trader(site["id"], a["id"])
            snap = t.snapshot() if t else {}
            webs = {mp[s]["web"] for s in a["symbols"] if s in mp}
            pnl = None
            if a["symbols"]:
                try:
                    from yonexis.mt5 import pnl as pnl_mod
                    pnl = pnl_mod.day_pnl(eng, mp, a, {}, int(c["magic"])).get("total_calls")
                except Exception:
                    pass
            legs = [l for st in (snap.get("symbols") or {}).values() for l in st.get("legs", [])]
            rows.append({"venue": "web", "site": site["id"], "site_name": site["name"], "id": a["id"], "name": a["name"],
                         "enabled": a["enabled"], "mode": c["mode"], "watchlist": a["watchlist"], "symbols": a["symbols"],
                         "unmapped": [s for s in a["symbols"] if s not in mp], "positions": [p for p in pos if p["symbol"] in webs],
                         "open_lots": sum(1 for l in legs if l["status"] == "open"), "entries": sum((st.get("cycles") or 0) for st in (snap.get("symbols") or {}).values()),
                         "pnl": pnl, "halted": snap.get("halted"), "error": snap.get("error"),
                         "events": (snap.get("events") or [])[-4:], "params": a["params"],
                         "workers": {"open": sum(1 for w in ws if w["open"]), "busy": sum(1 for w in ws if w["busy"]),
                                     "ready": sum(1 for w in ws if w.get("staged")), "errors": [w["error"] for w in ws if w["error"]][:3]}})
    try:                                                   # the MT5 automation, for the full picture
        from yonexis.mt5 import auto as mt5auto, config as mcfg
        a, snap = mcfg.auto(), (mt5auto.trader.snapshot() if mt5auto.trader else {})
        rows.append({"venue": "mt5", "site": None, "site_name": "MetaTrader 5", "id": "mt5", "name": "MT5 automation",
                     "enabled": a["enabled"], "mode": mcfg.get()["mode"], "watchlist": a.get("watchlist") or "", "symbols": a["symbols"],
                     "unmapped": [s for s in a["symbols"] if s not in mcfg.mapping()], "positions": [],
                     "open_lots": sum(1 for st in (snap.get("symbols") or {}).values() for l in st.get("legs", []) if l["status"] == "open"),
                     "entries": sum((st.get("cycles") or 0) for st in (snap.get("symbols") or {}).values()), "pnl": None,
                     "halted": snap.get("halted"), "error": snap.get("error"), "events": (snap.get("events") or [])[-4:],
                     "params": a["params"], "workers": None})
    except Exception:
        pass
    return {"market_open": market, "now": now.strftime("%H:%M:%S"), "automations": rows}


@router.post("/close-all")
def close_all(site: str = "default"):
    """Kill switch: stop the automation and close every position YONEXIS opened through the web flows."""
    S = R(site)
    cid = None
    for aid in S.cfg.automation_ids():
        S.cfg.save_auto({"enabled": False}, aid=aid)
        cid = executor.start(site, aid).close_all(why="admin kill switch") or cid
    db.log("admin", "openaiap_close_all", {"sent": bool(cid)})
    return {"stopped": True, "close_sent": bool(cid)}


@router.post("/positions/forget")
def positions_forget(site: str = "default"):
    """The positions were closed by hand in the web terminal: forget them (nothing is sent)."""
    S = R(site)
    S.ex.forget()
    db.log("admin", "openaiap_positions_forget", {})
    return {"positions": []}


@router.get("/log")
def log_get(limit: int = 200, site: str = "default"):
    S = R(site)
    t = db.openaiap_log
    where = (t.c.site == site) if site != config.DEFAULT_SITE else ((t.c.site == site) | (t.c.site.is_(None)))
    rows = db.fetch(t, where=where, order=t.c.id.desc(), limit=max(1, min(limit, 1000)))
    for r in rows:
        r["detail"] = db.uj(r["detail"], {})
    return rows
