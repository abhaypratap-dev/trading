"""YONEXIS API: dashboards, data, strategies, backtests, agents, live signals, feedback, models, risk."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from yonexis import db, feedback
from yonexis.agents import llm, pipeline
from yonexis.backtest import BacktestConfig, CostModel, full_evaluation, normalize_candles, run_backtest
from yonexis.backtest.parity import compare
from yonexis.config import settings
from yonexis.data.connectors import AVAILABLE, get_connector
from yonexis.data.connectors.simulated import simulate
from yonexis.data.connectors.upload import interval_label, parse_candles, parse_trades, preview, read_any
from yonexis.live import engine as live
from yonexis.ml import TrainedModel, train_imitation, train_meta
from yonexis.risk import risk
from yonexis.sandbox import check as static_check
from yonexis.sandbox import run as sandbox_run
from yonexis.strategies import REGISTRY
from yonexis.strategies.loader import latest_versions

from . import jobs

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
try:   # also log to data/logs so problems can be diagnosed from the project folder
    from logging.handlers import RotatingFileHandler
    (settings.DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    _fh = RotatingFileHandler(settings.DATA_DIR / "logs" / "yonexis.log", maxBytes=5_000_000, backupCount=3)
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    _fh.setLevel(logging.INFO)
    logging.getLogger().addHandler(_fh)
    logging.getLogger("uvicorn.access").addHandler(_fh)
except Exception:  # pragma: no cover
    pass
WEB_DIR = Path(__file__).resolve().parents[3] / "web"


def seed_builtins() -> None:
    """Register built-in strategies; when their code changes in an update, add a new version that keeps the status/params."""
    import inspect
    have = latest_versions()
    for name, cls in REGISTRY.items():
        code = inspect.getsource(inspect.getmodule(cls))
        cur = have.get(name)
        if cur is None:
            # the intraday library joins as "approved" (backtest / day test / AI pick only); it never starts trading by itself
            first = "paper" if name in ("first_red_candle_zone", "hari_master_block") else "approved"
            db.insert_row(db.strategies, name=name, version=1, origin="builtin", status=first, code=code,
                          params=db.j({}), spec=db.j({"summary": cls.description}), review=db.j({"static_check": static_check(code)}))
        elif cur["origin"] == "builtin" and cur["code"] != code:
            db.insert_row(db.strategies, name=name, version=cur["version"] + 1, origin="builtin", status=cur["status"], code=code,
                          params=cur["params"], spec=db.j({"summary": cls.description}), review=db.j({"static_check": static_check(code)}))
            db.update_row(db.strategies, cur["id"], status="retired")
            db.log("system", "builtin_strategy_updated", {"name": name, "version": cur["version"] + 1})


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    seed_builtins()
    try:
        from yonexis.risk.engine import apply_limits
        apply_limits(db.get_setting("risk_limits", {}) or {})
        from yonexis.broker.paper import void_out_of_hours_trades
        void_out_of_hours_trades()
        from yonexis import integrity
        integrity.run(repair=True)
    except Exception:
        logging.exception("startup settings / paper correction failed")
    task = None
    if settings.ENABLE_LIVE_LOOP:
        live.engine = live.LiveEngine()
        task = asyncio.create_task(live.engine.run())
        if settings.LIVE_CONNECTOR == "upstox" and not db.get_setting("futures_seeded_v1"):
            # one-time: add index futures + futures of the watchlist stocks (admin asked for both)
            asyncio.get_running_loop().run_in_executor(None, _seed_futures_once)
    yield
    if live.engine:
        live.engine.stop()
    if task:
        task.cancel()


app = FastAPI(title="YONEXIS AI Trading Platform", version="0.1.0", lifespan=lifespan)


def admin(x_admin_token: str | None = Header(default=None)) -> None:
    if settings.ADMIN_TOKEN and x_admin_token != settings.ADMIN_TOKEN:
        raise HTTPException(401, "missing or wrong X-Admin-Token")


A = [Depends(admin)]


# ====================================================================== helpers

def load_data(symbols: list[str], interval: str, start: str | None, end: str | None, source: str = "db") -> dict[str, pd.DataFrame]:
    out = {}
    for s in symbols:
        if source == "simulated":
            df = simulate(s, start or "2024-01-01", end or "2025-06-30", int(interval.rstrip("m")) if interval.endswith("m") else 5)
        else:
            df = db.load_candles(s, interval, start, end)
        if not df.empty:
            out[s.upper()] = df
    if not out:
        raise HTTPException(400, f"no {interval} candles found for {symbols} in {start}..{end}. Load data first (Data tab).")
    return out


def strategy_code(name: str, version: int | None = None) -> dict:
    rows = db.fetch(db.strategies, db.strategies.c.name == name, order=db.strategies.c.version)
    if version is not None:
        rows = [r for r in rows if r["version"] == version]
    if not rows:
        raise HTTPException(404, f"strategy {name} not found")
    return rows[-1]


def save_strategy(name: str, code: str, origin: str, spec: dict | None = None, review: dict | None = None,
                  params: dict | None = None) -> dict:
    prev = db.fetch(db.strategies, db.strategies.c.name == name, order=db.strategies.c.version)
    version = (prev[-1]["version"] + 1) if prev else 1
    sid = db.insert_row(db.strategies, name=name, version=version, origin=origin, status="candidate", code=code,
                        spec=db.j(spec or {}), review=db.j(review or {}), params=db.j(params or {}))
    db.log("admin", "strategy_saved", {"name": name, "version": version, "origin": origin})
    return {"id": sid, "name": name, "version": version}


def config_from(body: dict | None) -> dict:
    body = body or {}
    return {"capital_per_trade": body.get("capital_per_trade", 100000), "slippage_bps": body.get("slippage_bps", 2.0),
            "fill": body.get("fill", "next_open"), "costs": body.get("costs", {})}


def _cfg(c: dict) -> BacktestConfig:
    c = dict(c)
    return BacktestConfig(costs=CostModel(**c.pop("costs", {})), **c)


# ====================================================================== health / meta

@app.get("/api/health")
def health():
    return {"ok": True, "db": settings.DATABASE_URL.split(":")[0], "llm": llm.health(), "connector": settings.LIVE_CONNECTOR,
            "trading_mode": "live" if settings.live_orders_allowed else "paper", "real_orders": "ENABLED" if settings.live_orders_allowed else "LOCKED", "live_engine": bool(live.engine and live.engine.running),
            "live_error": live.engine.last_error if live.engine else None, "sandbox": _sandbox_status()}


def _sandbox_status():
    from yonexis.sandbox.runner import status
    return status()


@app.get("/api/connectors", dependencies=A)
def connectors():
    return AVAILABLE


# ====================================================================== data

class FetchBody(BaseModel):
    symbols: list[str]
    source: str = "yahoo"
    interval: str = "5m"
    start: str
    end: str


@app.post("/api/data/fetch", dependencies=A)
def data_fetch(b: FetchBody):
    conn = get_connector(b.source)
    out = {}
    for s in b.symbols:
        try:
            df = conn.fetch_history(s, b.start, b.end, b.interval)
            out[s] = db.save_candles(s, df, b.interval, b.source)
        except Exception as e:
            out[s] = f"error: {e}"
    total = sum(v for v in out.values() if isinstance(v, int))
    db.insert_row(db.datasets, name=f"{b.source} {b.interval} {b.start}..{b.end}", kind="candles", source=b.source,
                  rows=total, symbols=db.j(b.symbols), start=b.start, end=b.end, meta=db.j(out))
    return {"saved_rows": out}


class HistBody(BaseModel):
    start: str = "2024-01-01"
    end: str | None = None
    interval: str = "5m"
    symbols: list[str] | None = None


@app.post("/api/data/download-watchlist", dependencies=A)
def download_watchlist(b: HistBody):
    """Pull real history from Upstox for every watchlist stock (or the given ones) into the database."""
    ux = _upstox()
    if not ux.tokens.token:
        raise HTTPException(400, "log in to Upstox first (Data tab)")
    syms = b.symbols or (live.engine.symbols if live.engine else db.get_watchlist(settings.WATCHLIST))
    end = b.end or str((pd.Timestamp.now() - pd.Timedelta(days=1)).date())

    def work(progress):
        c, out = ux.UpstoxConnector(), {}
        for i, sym in enumerate(syms):
            progress(f"{sym} ({i + 1}/{len(syms)})", {"done": i, "total": len(syms)})
            try:
                df = c.fetch_history(sym, b.start, end, b.interval)
                out[sym] = db.save_candles(sym, df, b.interval, "upstox") if not df.empty else 0
            except Exception as e:
                out[sym] = f"error: {e}"
        total = sum(v for v in out.values() if isinstance(v, int))
        db.insert_row(db.datasets, name=f"upstox {b.interval} {b.start}..{end} ({len(syms)} stocks)", kind="candles",
                      source="upstox", rows=total, symbols=db.j(syms), start=b.start, end=end, meta=db.j(out))
        return {"rows": out, "total": total}

    return {"job_id": jobs.submit("download_history", work)}


@app.post("/api/data/upload/preview", dependencies=A)
async def upload_preview(file: UploadFile = File(...)):
    return preview(file.filename, await file.read())


@app.post("/api/data/upload", dependencies=A)
async def upload(file: UploadFile = File(...), kind: str = Form("candles"), mapping: str = Form("{}"),
                 symbol: str = Form(""), interval: str = Form(""), date_col: str = Form(""), time_col: str = Form("")):
    content = await file.read()
    (settings.uploads_dir / file.filename).write_bytes(content)
    df = read_any(file.filename, content)
    m = db.uj(mapping, {}) or {}
    if kind == "trades":
        t = parse_trades(df, m)
        did = db.insert_row(db.datasets, name=file.filename, kind="trades", source="upload", rows=len(t),
                            symbols=db.j(sorted(t["symbol"].unique().tolist())), start=str(t["entry_ts"].min()),
                            end=str(t["entry_ts"].max()), meta=db.j({"file": file.filename, "mapping": m}))
        return {"dataset_id": did, "rows": len(t), "sample": t.head(5).astype(str).to_dict("records")}
    parsed, report = parse_candles(df, m, symbol or None, date_col or None, time_col or None)
    saved = {}
    for s, g in parsed.items():
        iv = interval or interval_label(report["symbols"][s]["interval_minutes"])
        saved[s] = {"interval": iv, "rows": db.save_candles(s, g, iv, "upload")}
    did = db.insert_row(db.datasets, name=file.filename, kind="candles", source="upload", rows=report["output_rows"],
                        symbols=db.j(sorted(parsed)), start=min((v["start"] for v in report.get("symbols", {}).values()), default=""),
                        end=max((v["end"] for v in report.get("symbols", {}).values()), default=""), meta=db.j(report))
    db.log("admin", "upload", {"file": file.filename, "dataset_id": did})
    return {"dataset_id": did, "report": report, "saved": saved}


@app.post("/api/data/simulate", dependencies=A)
def data_simulate(symbols: list[str], start: str = "2024-01-01", end: str = "2025-06-30"):
    out = {s: db.save_candles(s, simulate(s, start, end), "5m", "simulated") for s in symbols}
    db.insert_row(db.datasets, name=f"simulated {start}..{end}", kind="candles", source="simulated", rows=sum(out.values()),
                  symbols=db.j(symbols), start=start, end=end, meta=db.j({}))
    return out


@app.get("/api/data/inventory", dependencies=A)
def inventory():
    return {"candles": db.candle_inventory(), "datasets": db.fetch(db.datasets, order=db.datasets.c.id.desc(), limit=100)}


@app.get("/api/candles", dependencies=A)
def get_candles(symbol: str, interval: str = "5m", start: str | None = None, end: str | None = None, live_view: bool = False):
    if live_view and live.engine and symbol.upper() in live.engine.cache:
        df = live.engine.cache[symbol.upper()]
        day = df.index[-1].normalize()
        f = live.engine.forming(symbol.upper())
        if f is not None and f["ts"].normalize() > day:
            day = f["ts"].normalize()                          # new session started: no closed candle yet
        df = df[df.index >= day]                               # current session only (forming candle is added by the UI)
        if df.empty:
            return []
    else:
        df = db.load_candles(symbol, interval, start, end)
    if df.empty:
        return []
    d = df.reset_index()
    d["ts"] = d["ts"].astype(str)
    return d.to_dict("records")


# ====================================================================== indicators, lab, news, order flow

@app.get("/api/indicators", dependencies=A)
def get_indicators(symbol: str, names: str, interval: str = "5m", start: str | None = None, end: str | None = None,
                   live_view: bool = False):
    """Indicator lines for the chart. Computed on enough earlier candles to warm up, returned for the shown range."""
    from yonexis.sdk.indicators import CHART
    sym = symbol.upper()
    if live_view and live.engine and sym in live.engine.cache:
        df = live.engine.cache[sym]
        shown_from = df.index[-1].normalize() if len(df) else None
    else:
        s0 = str((pd.Timestamp(start) - pd.Timedelta(days=12)).date()) if start else None
        df = db.load_candles(sym, interval, s0, end, real_only=True)
        if df.empty and interval != "5m":
            df = db.load_candles(sym, "5m", s0, end, real_only=True)
        shown_from = pd.Timestamp(start) if start else None
    if df is None or df.empty:
        return {"series": {}}
    df = normalize_candles(df)
    out = {}
    for n in [x.strip() for x in names.split(",") if x.strip()]:
        if n not in CHART:
            continue
        fn, overlay = CHART[n]
        try:
            r = fn(df)
        except Exception as e:
            out[n] = {"error": str(e)}
            continue
        r = r.to_frame(n) if isinstance(r, pd.Series) else r
        if shown_from is not None:
            r = r[r.index >= shown_from]
        lines = {}
        for col in r.columns:
            v = r[col].dropna()
            lines[col] = [[str(t), round(float(x), 4)] for t, x in v.items()]
        out[n] = {"overlay": overlay, "lines": lines}
    return {"series": out}


@app.get("/api/strategy-indicators", dependencies=A)
def strategy_indicators():
    from yonexis.sdk.indicators import CHART
    from yonexis.strategies import REGISTRY
    m = {n: list(getattr(c, "chart_indicators", []) or []) for n, c in REGISTRY.items()}
    m["first_red_candle_zone"] = m.get("first_red_candle_zone") or ["vwap"]
    m["hari_master_block"] = m.get("hari_master_block") or ["vwap", "ema21"]
    m["pair_trading"], m["ai_pick"] = ["vwap"], ["vwap", "ema9", "ema21"]
    return {"by_strategy": m, "available": list(CHART),
            "descriptions": {n: (c.description or "") for n, c in REGISTRY.items()},
            "families": {n: getattr(c, "family", "custom") for n, c in REGISTRY.items()}}


class LabBody(BaseModel):
    start: str = "2024-01-01"
    include_pairs: bool = True


@app.post("/api/lab/run", dependencies=A)
def lab_run(b: LabBody):
    from yonexis import lab
    j = jobs.latest("lab")
    if j and j.get("status") == "running":
        return {"job_id": j["id"]}
    return {"job_id": jobs.submit("lab", lambda progress: lab.run(live.engine, progress, b.start, b.include_pairs))}


@app.get("/api/lab/last", dependencies=A)
def lab_last():
    return db.get_setting("lab_last") or {}


class AiThresholdBody(BaseModel):
    min_p: float | None = None        # None = use the threshold the Strategy Lab validated


@app.get("/api/lab/ai-threshold", dependencies=A)
def lab_ai_threshold():
    from yonexis.lab import champion_selector
    from yonexis.ml import selector
    model, _ = champion_selector()
    return {"override": db.get_setting("ai_min_p"), "validated": (model.meta or {}).get("min_p") if model else None,
            "in_use": selector.min_p_for(model) if model else None}


@app.put("/api/lab/ai-threshold", dependencies=A)
def lab_ai_threshold_set(b: AiThresholdBody):
    if b.min_p is not None and not (0.2 <= b.min_p <= 0.9):
        raise HTTPException(400, "min_p must be between 0.2 and 0.9")
    db.set_setting("ai_min_p", b.min_p)
    db.log("admin", "ai_min_p", {"min_p": b.min_p})
    return lab_ai_threshold()


@app.get("/api/lab/trades", dependencies=A)
def lab_trades(strategy: str | None = None, symbol: str | None = None, limit: int = 500):
    w = None
    if strategy:
        w = db.lab_trades.c.strategy == strategy
    if symbol:
        c = db.lab_trades.c.symbol == symbol.upper()
        w = c if w is None else (w & c)
    return db.fetch(db.lab_trades, w, order=db.lab_trades.c.entry_ts.desc(), limit=min(limit, 5000))


@app.get("/api/news", dependencies=A)
def get_news(symbol: str | None = None, hours: int = 72, limit: int = 100):
    from yonexis import news
    return {"items": news.recent(symbol, hours, limit), "last": db.get_setting("news_last") or {}}


@app.get("/api/news/summary", dependencies=A)
def news_summary(hours: int = 24):
    from yonexis import news
    syms = live.engine.symbols if live.engine else db.get_watchlist(settings.WATCHLIST)
    return news.summary(syms, hours)


@app.post("/api/news/refresh", dependencies=A)
def news_refresh():
    from yonexis import news
    syms = live.engine.symbols if live.engine else db.get_watchlist(settings.WATCHLIST)
    return {"job_id": jobs.submit("news", lambda progress: news.collect(syms))}


@app.get("/api/orderflow/{symbol}", dependencies=A)
def get_orderflow(symbol: str):
    from yonexis.data.orderflow import flow
    sym = symbol.upper()
    snap = flow.snapshot(sym)
    if not snap["minutes"]:                       # not streaming (market closed / restart): today's stored minutes
        day0 = pd.Timestamp.now().normalize().to_pydatetime()
        rows = db.fetch(db.orderflow, (db.orderflow.c.symbol == sym) & (db.orderflow.c.ts >= day0), order=db.orderflow.c.ts)
        cum = 0.0
        for r in rows:
            cum += r["delta"] or 0
            snap["minutes"].append({**r, "ts": str(r["ts"]), "cum_delta": cum})
    return snap


# ====================================================================== strategies

@app.get("/api/strategies", dependencies=A)
def list_strategies():
    rows = db.fetch(db.strategies, order=db.strategies.c.id)
    for r in rows:
        r["spec"], r["review"], r["params"] = db.uj(r["spec"], {}), db.uj(r["review"], {}), db.uj(r["params"], {})
    return rows


class CodeBody(BaseModel):
    name: str | None = None
    code: str
    origin: str = "upload"
    spec: dict | None = None


@app.post("/api/strategies", dependencies=A)
def upload_strategy(b: CodeBody):
    sc = static_check(b.code)
    if not sc["ok"]:
        raise HTTPException(400, {"static_check": sc})
    d = sandbox_run(b.code, {"SIM": simulate("SIM", "2024-01-01", "2024-02-28")}, {"kind": "describe"}, timeout=60)
    if not d.get("ok"):
        raise HTTPException(400, {"error": d.get("error"), "trace": d.get("trace")})
    name = b.name or d["result"]["strategy"]["name"]
    return {**save_strategy(name, b.code, b.origin, b.spec, {"static_check": sc}), "static_check": sc,
            "described": d["result"]["strategy"]}


class StatusBody(BaseModel):
    status: str
    params: dict | None = None


@app.get("/api/strategies/{name}/{version}/params", dependencies=A)
def strategy_params(name: str, version: int):
    row = strategy_code(name, version)
    if row["origin"] == "builtin" and name in REGISTRY:
        defaults = REGISTRY[name].params
    else:
        d = sandbox_run(row["code"], {"SIM": simulate("SIM", "2024-01-01", "2024-01-31")}, {"kind": "describe"}, timeout=60)
        defaults = d["result"]["strategy"]["params"] if d.get("ok") else {}
    return {"defaults": defaults, "saved": db.uj(row["params"], {}) or {}, "status": row["status"]}


@app.post("/api/strategies/{name}/{version}/status", dependencies=A)
def set_status(name: str, version: int, b: StatusBody):
    allowed = ("candidate", "approved", "paper", "live_small", "live", "retired")
    if b.status not in allowed:
        raise HTTPException(400, f"status must be one of {allowed}")
    row = strategy_code(name, version)
    # live / live_small are allowed as labels, but while real orders are locked they still trade the demo wallet only.
    upd = {"status": b.status}
    if b.params is not None:
        upd["params"] = db.j(b.params)
    db.update_row(db.strategies, row["id"], **upd)
    db.log("admin", "strategy_status", {"name": name, "version": version, "status": b.status})
    return {"ok": True}


# ====================================================================== backtests

class BacktestBody(BaseModel):
    strategy: str
    version: int | None = None
    symbols: list[str]
    interval: str = "5m"
    start: str | None = None
    end: str | None = None
    source: str = "db"
    params: dict = Field(default_factory=dict)
    config: dict = Field(default_factory=dict)
    param_space: dict | None = None
    baseline_net_pnl: float | None = None


@app.post("/api/backtests/quick", dependencies=A)
def quick_backtest(b: BacktestBody):
    row = strategy_code(b.strategy, b.version)
    data = load_data(b.symbols, b.interval, b.start, b.end, b.source)
    r = sandbox_run(row["code"], data, {"kind": "quick", "params": b.params, "config": config_from(b.config)})
    if not r.get("ok"):
        raise HTTPException(400, {"error": r.get("error"), "trace": r.get("trace")})
    res = r["result"]
    bid = db.insert_row(db.backtests, strategy=b.strategy, strategy_version=row["version"], kind="quick", params=db.j(b.params),
                        symbols=db.j(b.symbols), start=b.start, end=b.end,
                        result=db.j({"metrics": res["metrics"], "trades": res["trades"][:3000]}))
    return {"id": bid, **res}


@app.post("/api/backtests/full", dependencies=A)
def full_backtest(b: BacktestBody):
    row = strategy_code(b.strategy, b.version)
    data = load_data(b.symbols, b.interval, b.start, b.end, b.source)

    def work(progress):
        progress(f"loaded {len(data)} stocks, {sum(len(v) for v in data.values()):,} {b.interval} candles ({b.source}); "
                 f"starting full evaluation of {b.strategy} v{row['version']}", {})
        r = sandbox_run(row["code"], data, {"kind": "full", "params": b.params, "config": config_from(b.config),
                                            "param_space": b.param_space, "baseline_net_pnl": b.baseline_net_pnl}, timeout=3600,
                        progress=lambda m, i: progress(m.strip(), i))
        if not r.get("ok"):
            raise RuntimeError(r.get("error"))
        ev = r["result"]
        progress(f"evaluation done: verdict {ev.get('verdict')} (score {ev.get('score')}); AI critic is reviewing it (local LLM)", {})
        ev["critic"] = pipeline.critic(ev)
        progress("writing the report (local LLM)", {})
        ev["report"] = pipeline.report(ev)
        progress("saved", {})
        ev["id"] = db.insert_row(db.backtests, strategy=b.strategy, strategy_version=row["version"], kind="full",
                                 params=db.j({**b.params, "_source": b.source}), symbols=db.j(b.symbols), start=b.start, end=b.end, result=db.j(ev))
        return ev

    return {"job_id": jobs.submit("full_backtest", work)}


@app.get("/api/backtests", dependencies=A)
def list_backtests(limit: int = 50):
    rows = db.fetch(db.backtests, order=db.backtests.c.id.desc(), limit=limit)
    for r in rows:
        res = db.uj(r.pop("result"), {})
        r["summary"] = res.get("metrics") or {"verdict": res.get("verdict"), "score": res.get("score"), **(res.get("development") or {})}
        r["summary"] = {k: r["summary"].get(k) for k in ("verdict", "score", "trades", "net_pnl", "win_rate", "profit_factor", "sharpe", "max_drawdown_pct")}
    return rows


@app.get("/api/backtests/{bid}", dependencies=A)
def get_backtest(bid: int):
    rows = db.fetch(db.backtests, db.backtests.c.id == bid)
    if not rows:
        raise HTTPException(404)
    r = rows[0]
    r["result"] = db.uj(r["result"], {})
    return r


# ====================================================================== parity (import admin scripts)

class ConvertBody(BaseModel):
    source: str
    language: str = "auto"
    notes: str = ""
    name: str | None = None


@app.post("/api/import/convert", dependencies=A)
def convert(b: ConvertBody):
    def work(progress):
        progress("converting", {})
        code = pipeline.convert_script(b.source, b.language, b.notes)
        progress("review", {})
        rv = pipeline.review(code, {"original_language": b.language, "notes": b.notes})
        for i in range(2):
            problems = rv["static_check"]["errors"] + [f"{x.get('problem')} -> {x.get('fix')}" for x in rv.get("issues", []) if x.get("severity") == "high"]
            if not problems:
                break
            progress("fixing", {"round": i, "problems": problems[:5]})
            code = pipeline.fix(code, problems)
            rv = pipeline.review(code, {"original_language": b.language})
        saved = None
        if rv["static_check"]["ok"]:
            d = sandbox_run(code, {"SIM": simulate("SIM", "2024-01-01", "2024-02-28")}, {"kind": "describe"}, timeout=60)
            if d.get("ok"):
                saved = save_strategy(b.name or d["result"]["strategy"]["name"], code, "import",
                                      {"original_language": b.language, "original": b.source[:20000]}, rv)
        return {"code": code, "review": rv, "saved": saved}

    return {"job_id": jobs.submit("convert", work)}


@app.post("/api/import/parity", dependencies=A)
async def parity(strategy: str = Form(...), version: int | None = Form(None), interval: str = Form("5m"),
                 params: str = Form("{}"), config: str = Form("{}"), mapping: str = Form("{}"),
                 time_tolerance_min: int = Form(10), file: UploadFile = File(...)):
    theirs = parse_trades(read_any(file.filename, await file.read()), db.uj(mapping, {}))
    if theirs.empty:
        raise HTTPException(400, "no trades parsed from the file; check the column mapping")
    syms = sorted(s for s in theirs["symbol"].unique() if s != "UNKNOWN")
    start, end = str(theirs["entry_ts"].min().date()), str(theirs["entry_ts"].max().date())
    data = load_data(syms, interval, start, end)
    row = strategy_code(strategy, version)
    r = sandbox_run(row["code"], data, {"kind": "quick", "params": db.uj(params, {}), "config": config_from(db.uj(config, {}))})
    if not r.get("ok"):
        raise HTTPException(400, {"error": r.get("error"), "trace": r.get("trace")})
    ours = pd.DataFrame(r["result"]["trades"])
    rep = compare(ours, theirs, time_tolerance_min)
    rep["our_metrics"] = r["result"]["metrics"]
    rep["id"] = db.insert_row(db.backtests, strategy=strategy, strategy_version=row["version"], kind="parity", params=params,
                              symbols=db.j(syms), start=start, end=end, result=db.j(rep))
    return rep


# ====================================================================== AI builder

class PromptBody(BaseModel):
    prompt: str
    context: str = ""


@app.post("/api/agents/spec", dependencies=A)
def agent_spec(b: PromptBody):
    try:
        return pipeline.spec_from_prompt(b.prompt, b.context)
    except llm.LLMError as e:
        raise HTTPException(503, str(e))


class BuildBody(BaseModel):
    spec: dict
    symbols: list[str]
    interval: str = "5m"
    start: str | None = None
    end: str | None = None
    source: str = "db"
    config: dict = Field(default_factory=dict)
    baseline_net_pnl: float | None = None
    save: bool = True


@app.post("/api/agents/build", dependencies=A)
def agent_build(b: BuildBody):
    data = load_data(b.symbols, b.interval, b.start, b.end, b.source)
    n_prev = len(db.fetch(db.backtests))

    def work(progress):
        out = pipeline.build_strategy(b.spec, data, baseline_net_pnl=b.baseline_net_pnl, n_trials_so_far=max(1, n_prev),
                                      progress=progress, config=config_from(b.config))
        if out.get("ok") and b.save:
            out["saved"] = save_strategy(out["name"], out["code"], "ai", b.spec, out["review"])
            out["backtest_id"] = db.insert_row(db.backtests, strategy=out["name"], strategy_version=out["saved"]["version"],
                                               kind="full", params=db.j({"_source": b.source}), symbols=db.j(b.symbols), start=b.start, end=b.end,
                                               result=db.j({**out["evaluation"], "critic": out["critic"], "report": out["report"]}))
        return out

    return {"job_id": jobs.submit("agent_build", work)}


@app.get("/api/jobs/latest/{kind}", dependencies=A)
def job_latest(kind: str):
    return jobs.latest(kind) or {}


@app.get("/api/jobs/{jid}", dependencies=A)
def job(jid: str):
    j = jobs.get(jid)
    if not j:
        raise HTTPException(404)
    return j


# ====================================================================== live

@app.get("/api/live/status", dependencies=A)
def live_status():
    e = live.engine
    if not e:
        return {"running": False}
    return {"running": e.running, "connector": e.connector_name, "rows": e.status, "error": e.last_error,
            "sim_day": str(e.sim.current_day.date()) if e.sim else None, "risk": risk.snapshot()}


@app.get("/api/signals", dependencies=A)
def list_signals(limit: int = 300, since: str | None = None, until: str | None = None, symbol: str | None = None,
                 side: str | None = None, strategy: str | None = None):
    c = db.signals.c
    cond = db.real_signals()
    if since:
        cond = cond & (c.ts >= pd.Timestamp(since).to_pydatetime())
    if until:
        cond = cond & (c.ts < (pd.Timestamp(until) + pd.Timedelta(days=1)).to_pydatetime())
    if symbol:
        cond = cond & (c.symbol == symbol.upper())
    if side in ("BUY", "SELL"):
        cond = cond & (c.side == side)
    if strategy:
        cond = cond & (c.strategy == strategy)
    rows = db.fetch(db.signals, cond, order=c.id.desc(), limit=min(limit, 5000))
    fb = {}
    for f in db.fetch(db.feedback, order=db.feedback.c.id):
        fb[f["signal_id"]] = {"action": f["action"], "remark": f["remark"]}
    for r in rows:
        r.pop("features", None)
        r["tags"] = db.uj(r["tags"], {})
        r["feedback"] = fb.get(r["id"])
    return rows


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket):
    if settings.ADMIN_TOKEN and ws.query_params.get("token") != settings.ADMIN_TOKEN:
        await ws.close(code=4401)
        return
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue(maxsize=200)
    if live.engine:
        live.engine.subscribers.add(q)
    try:
        while True:
            await ws.send_text(await q.get())
    except WebSocketDisconnect:
        pass
    finally:
        if live.engine:
            live.engine.subscribers.discard(q)


# ====================================================================== feedback

class FeedbackBody(BaseModel):
    action: str
    remark: str = ""
    new_sl: float | None = None
    new_target: float | None = None


@app.post("/api/signals/{sid}/feedback", dependencies=A)
def give_feedback(sid: int, b: FeedbackBody):
    try:
        fid = feedback.record(sid, b.action, b.remark, b.new_sl, b.new_target)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))
    if b.remark.strip():
        tags = pipeline.tag_remarks([{"id": fid, "remark": b.remark}])
        db.update_row(db.feedback, fid, reasons=db.j(tags.get(fid, [])))
    return {"id": fid}


@app.get("/api/feedback/scoreboard", dependencies=A)
def scoreboard():
    return feedback.scoreboard()


@app.post("/api/feedback/suggestions", dependencies=A)
def suggestions():
    return {"suggestions": pipeline.suggest_from_feedback(feedback.scoreboard())}


# ====================================================================== models

class TrainMetaBody(BaseModel):
    strategy: str
    version: int | None = None
    symbols: list[str]
    interval: str = "5m"
    start: str | None = None
    end: str | None = None
    source: str = "db"
    params: dict = Field(default_factory=dict)


@app.post("/api/models/train-meta", dependencies=A)
def train_meta_ep(b: TrainMetaBody):
    row = strategy_code(b.strategy, b.version)
    data = load_data(b.symbols, b.interval, b.start, b.end, b.source)

    def work(progress):
        from yonexis.strategies.loader import load_code
        progress("backtest", {})
        cls = load_code(row["code"]) if row["origin"] != "builtin" else REGISTRY[row["name"]]
        res = run_backtest(cls, data, b.params)
        progress("training", {"trades": len(res.trades)})
        m = train_meta(res, data)
        m.meta.update({"strategy": b.strategy, "strategy_version": row["version"], "symbols": b.symbols})
        p = m.save(settings.models_dir)
        mid = db.insert_row(db.models, kind="meta", version=m.version, strategy=b.strategy, path=str(p), metrics=db.j(m.metrics),
                            status="shadow")
        return {"model_id": mid, "version": m.version, "metrics": m.metrics}

    return {"job_id": jobs.submit("train_meta", work)}


@app.post("/api/models/train-imitation", dependencies=A)
def train_imitation_ep(strategy: str | None = None):
    rows = feedback.rows_with_outcomes()
    rows = [r for r in rows if strategy in (None, r["strategy"])]
    try:
        m = train_imitation([{"features": r["features"], "action": r["action"], "ts": r["signal_ts"]} for r in rows])
    except ValueError as e:
        raise HTTPException(400, str(e))
    p = m.save(settings.models_dir)
    mid = db.insert_row(db.models, kind="imitation", version=m.version, strategy=strategy, path=str(p),
                        metrics=db.j(m.metrics), status="shadow")
    return {"model_id": mid, "metrics": m.metrics}


@app.get("/api/models", dependencies=A)
def list_models():
    rows = db.fetch(db.models, order=db.models.c.id.desc())
    for r in rows:
        r["metrics"] = db.uj(r["metrics"], {})
    return rows


@app.post("/api/models/{mid}/promote", dependencies=A)
def promote(mid: int):
    rows = db.fetch(db.models, db.models.c.id == mid)
    if not rows:
        raise HTTPException(404)
    m = rows[0]
    for other in db.fetch(db.models):
        if other["kind"] == m["kind"] and other["strategy"] == m["strategy"] and other["status"] == "champion":
            db.update_row(db.models, other["id"], status="retired")
    db.update_row(db.models, mid, status="champion")
    db.log("admin", "model_promoted", {"id": mid})
    return {"ok": True}



# ====================================================================== paper wallet (demo money)

def _paper():
    from yonexis.broker import PaperBroker
    b = live.engine.broker if live.engine else None
    return b if isinstance(b, PaperBroker) else PaperBroker()


def _quotes() -> dict:
    e = live.engine
    return {s: e.quote(s) for s in e.symbols} if e else {}


@app.get("/api/wallet", dependencies=A)
def wallet():
    return _paper().summary(_quotes())


@app.get("/api/wallet/trades", dependencies=A)
def wallet_trades(limit: int = 300):
    return db.fetch(db.paper_trades, order=db.paper_trades.c.id.desc(), limit=limit)


@app.get("/api/wallet/ledger", dependencies=A)
def wallet_ledger(limit: int = 200):
    return db.fetch(db.wallet_ledger, order=db.wallet_ledger.c.id.desc(), limit=limit)


class AmountBody(BaseModel):
    amount: float | None = None


@app.post("/api/wallet/reset", dependencies=A)
def wallet_reset(b: AmountBody):
    try:
        bal = _paper().wallet.reset(b.amount)
    except ValueError as e:
        raise HTTPException(400, str(e))
    risk.state.realized_pnl = 0.0
    db.log("admin", "wallet_reset", {"amount": bal})
    return {"balance": bal}


@app.post("/api/wallet/deposit", dependencies=A)
def wallet_deposit(b: AmountBody):
    try:
        bal = _paper().wallet.deposit(float(b.amount or 0))
    except ValueError as e:
        raise HTTPException(400, str(e))
    db.log("admin", "wallet_deposit", {"amount": b.amount})
    return {"balance": bal}


class RebuildBody(BaseModel):
    symbols: list[str] = []
    strategy: str | None = None
    version: int | None = None
    date: str | None = None          # YYYY-MM-DD; empty = today


@app.get("/api/wallet/test-scope", dependencies=A)
def test_scope():
    """What the Paper Wallet selector needs: watchlist split into stocks/futures, strategies with versions, saved choice."""
    from yonexis.strategies.loader import get_scope
    syms = live.engine.symbols if live.engine else db.get_watchlist(settings.WATCHLIST)
    strategies = {}
    for r in db.fetch(db.strategies, order=db.strategies.c.version):
        strategies.setdefault(r["name"], []).append({"version": r["version"], "status": r["status"], "origin": r["origin"]})
    from yonexis.lab import champion_selector
    _, sel = champion_selector()
    strategies["ai_pick"] = [{"version": 1, "status": (sel["status"] + " · model " + sel["version"]) if sel else "not trained (run Strategy Lab)",
                              "origin": "ai"}]
    strategies["pair_trading"] = [{"version": 1, "status": "built-in", "origin": "builtin"}]
    all_fut = []
    if settings.LIVE_CONNECTOR == "upstox":
        try:
            all_fut = _upstox().instruments.all_futures()
        except Exception:
            all_fut = []
    return {"stocks": [x for x in syms if not x.endswith("-FUT")], "futures": [x for x in syms if x.endswith("-FUT")],
            "all_futures": all_fut, "strategies": strategies, "scope": get_scope()}


class MarginBody(BaseModel):
    equity_leverage: float
    fut_margin_pct: float


@app.get("/api/wallet/margin", dependencies=A)
def wallet_margin():
    from yonexis.broker.paper import margins
    lev, fut = margins()
    return {"equity_leverage": lev, "fut_margin_pct": fut}


@app.put("/api/wallet/margin", dependencies=A)
def wallet_margin_set(b: MarginBody):
    if not (1 <= b.equity_leverage <= 10) or not (1 <= b.fut_margin_pct <= 100):
        raise HTTPException(400, "leverage must be 1-10x and futures margin 1-100%")
    db.set_setting("paper_margin", {"equity_leverage": b.equity_leverage, "fut_margin_pct": b.fut_margin_pct})
    db.log("admin", "paper_margin", b.model_dump())
    return wallet_margin()


@app.post("/api/wallet/rebuild-today", dependencies=A)
def wallet_rebuild_today(b: RebuildBody | None = None):
    """Today's live test: replay today's real 1-minute market for the selected stocks/futures with the selected
    strategy and rebuild today's paper book (all of today's other trades and calls are removed). The selection is
    saved and the live engine trades exactly that for the rest of the day."""
    from yonexis import today
    j = jobs.latest("today_rebuild")
    if j and j.get("status") == "running":
        return {"job_id": j["id"]}
    day = b.date if b and b.date else None
    scope = {k: v for k, v in b.model_dump().items() if k != "date"} if b and b.strategy else None
    if scope and not scope["symbols"]:
        raise HTTPException(400, "select at least one stock or future")
    if scope:                          # futures picked from the full F&O list join the watchlist first
        syms = live.engine.symbols if live.engine else db.get_watchlist(settings.WATCHLIST)
        new = [x.upper() for x in scope["symbols"] if x.upper() not in syms]
        if len(syms) + len(new) > MAX_WATCHLIST:
            raise HTTPException(400, f"watchlist is limited to {MAX_WATCHLIST} symbols")
        for x in new:
            if live.engine:
                r = live.engine.add_symbol(x)
                if x in r.get("invalid", {}):
                    raise HTTPException(400, f"{x}: {r['invalid'][x]}")
            db.add_to_watchlist(x)
        if new:
            db.log("admin", "watchlist_add", {"symbols": new, "via": "paper test"})
    return {"job_id": jobs.submit("today_rebuild", lambda progress: today.rebuild(live.engine, progress, scope, day))}


class ClearBody(BaseModel):
    amount: float | None = None
    include_calls: bool = False


@app.post("/api/wallet/clear-history", dependencies=A)
def wallet_clear_history(b: ClearBody):
    from yonexis.broker.paper import clear_history
    out = clear_history(b.amount, b.include_calls)
    if live.engine:
        live.engine.reload_today()
    return out


@app.post("/api/wallet/close/{trade_id}", dependencies=A)
def wallet_close(trade_id: int):
    rows = db.fetch(db.paper_trades, db.paper_trades.c.id == trade_id)
    if not rows or rows[0]["status"] != "open":
        raise HTTPException(404, "no open paper trade with that id")
    q = _quotes().get(rows[0]["symbol"], {})
    r = _paper().close(trade_id, q.get("ltp") or rows[0]["last_price"] or rows[0]["entry_price"], "manual")
    db.log("admin", "paper_manual_close", r)
    return r

# ====================================================================== phase 4: paper vs backtest, gates

@app.get("/api/performance", dependencies=A)
def performance():
    from yonexis import performance as perf
    rows = [perf.compare(r["name"]) | {"status": r["status"], "version": r["version"]}
            for r in latest_versions().values() if r["status"] != "retired"]
    return {"strategies": rows, "min_sessions": perf.MIN_SESSIONS}


@app.post("/api/phase4/setup", dependencies=A)
def phase4_setup(start: str = "2024-01-01"):
    from yonexis import phase4
    j = jobs.latest("phase4")
    if j and j.get("status") == "running":
        return {"job_id": j["id"]}
    return {"job_id": jobs.submit("phase4", lambda progress: phase4.run(live.engine, progress, start))}


@app.get("/api/phases", dependencies=A)
def phases():
    from yonexis import performance as perf
    return perf.phases()


@app.post("/api/integrity", dependencies=A)
def integrity_check(repair: bool = False):
    from yonexis import integrity
    return integrity.run(repair=repair)


# ====================================================================== system check

@app.post("/api/system/check", dependencies=A)
def system_check(deep: bool = False):
    from yonexis import syscheck
    return {"job_id": jobs.submit("system_check", lambda progress: syscheck.run(deep, progress))}


@app.get("/api/system/check/last", dependencies=A)
def system_check_last():
    p = settings.DATA_DIR / "system_check.json"
    return db.uj(p.read_text(), {}) if p.exists() else {}


# ====================================================================== risk

@app.get("/api/risk", dependencies=A)
def get_risk():
    return risk.snapshot()


@app.get("/api/settings/risk", dependencies=A)
def get_risk_settings():
    from yonexis.risk.engine import EDITABLE
    return {"values": {k: getattr(risk.limits, k) for k in EDITABLE},
            "labels": {k: v[3] for k, v in EDITABLE.items()},
            "paper": {"slippage_bps": settings.PAPER_SLIPPAGE_BPS, "leverage": settings.PAPER_LEVERAGE}}


@app.put("/api/settings/risk", dependencies=A)
def put_risk_settings(values: dict):
    from yonexis.risk.engine import apply_limits
    try:
        clean = apply_limits(values)
    except (ValueError, TypeError) as e:
        raise HTTPException(400, str(e))
    saved = {**(db.get_setting("risk_limits", {}) or {}), **clean}
    db.set_setting("risk_limits", saved)
    db.log("admin", "risk_settings", clean)
    return get_risk_settings()


@app.post("/api/risk/kill", dependencies=A)
def kill(reason: str = "manual kill switch"):
    risk.kill(reason)
    db.log("admin", "kill_switch", {"reason": reason})
    return risk.snapshot()


@app.post("/api/risk/reset", dependencies=A)
def reset():
    risk.reset_kill()
    db.log("admin", "kill_reset", {})
    return risk.snapshot()


@app.get("/api/audit", dependencies=A)
def audit(limit: int = 200):
    return db.fetch(db.audit, order=db.audit.c.id.desc(), limit=limit)



# ====================================================================== Upstox

def _upstox():
    from yonexis.data.connectors import upstox as ux
    return ux


@app.get("/api/upstox/status", dependencies=A)
def upstox_status():
    ux = _upstox()
    st = {"configured": bool(settings.UPSTOX_API_KEY and settings.UPSTOX_API_SECRET),
          "redirect_uri": settings.UPSTOX_REDIRECT_URI, "logged_in": bool(ux.tokens.token),
          "session": {k: v for k, v in ux.tokens.load().items() if k != "access_token"},
          "live_connector": settings.LIVE_CONNECTOR, "trading_mode": settings.TRADING_MODE, "broker": settings.BROKER,
          "feed": live.engine.feed.info() if live.engine and getattr(live.engine, "feed", None) else None}
    if st["logged_in"]:
        st["account"] = ux.UpstoxConnector().health()
    return st


@app.post("/api/upstox/login-url", dependencies=A)
def upstox_login_url():
    ux = _upstox()
    try:
        return {"url": ux.login_url(ux.tokens.new_state())}
    except ux.UpstoxError as e:
        raise HTTPException(400, str(e))


@app.get("/api/upstox/callback", include_in_schema=False)
def upstox_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    """Upstox redirects the browser here after login. Protected by the one-time `state` value."""
    ux = _upstox()
    if error or not code:
        return HTMLResponse(f"<h3>Upstox login failed: {error or 'no code'}</h3><a href='/'>Back</a>", status_code=400)
    if not ux.tokens.check_state(state):
        return HTMLResponse("<h3>Login link expired or invalid. Start again from the Data tab.</h3><a href='/'>Back</a>", status_code=400)
    try:
        who = ux.exchange_code(code)
    except Exception as e:
        return HTMLResponse(f"<h3>Upstox token exchange failed</h3><pre>{e}</pre><a href='/'>Back</a>", status_code=400)
    db.log("admin", "upstox_login", who)
    if live.engine:
        live.engine.on_login()
    return RedirectResponse("/?upstox=connected")


class TokenBody(BaseModel):
    access_token: str


@app.post("/api/upstox/token", dependencies=A)
def upstox_manual_token(b: TokenBody):
    """Paste a token generated in Upstox Developer Apps (alternative to the login button)."""
    ux = _upstox()
    ux.tokens.save(b.access_token.strip(), {"source": "manual"})
    h = ux.UpstoxConnector().health()
    if not h.get("ok"):
        ux.tokens.clear()
        raise HTTPException(400, f"token rejected by Upstox: {h.get('error')}")
    db.log("admin", "upstox_login", {"source": "manual", "user": h.get("user")})
    if live.engine:
        live.engine.on_login()
    return h


@app.post("/api/upstox/logout", dependencies=A)
def upstox_logout():
    _upstox().tokens.clear()
    db.log("admin", "upstox_logout", {})
    return {"ok": True}


@app.get("/api/upstox/search", dependencies=A)
def upstox_search(q: str):
    try:
        return _upstox().instruments.search(q)
    except Exception as e:
        raise HTTPException(503, f"symbol search unavailable: {e}")


MAX_WATCHLIST = 300


@app.get("/api/watchlist", dependencies=A)
def get_watchlist():
    syms = live.engine.symbols if live.engine else db.get_watchlist(settings.WATCHLIST)
    strategies = [r["name"] for r in latest_versions().values() if r["status"] in ("paper", "live_small", "live")]
    return {"symbols": syms, "max": MAX_WATCHLIST, "strategies_making_calls": strategies}


class SymbolBody(BaseModel):
    symbol: str


@app.post("/api/watchlist", dependencies=A)
def add_watchlist(b: SymbolBody):
    sym = b.symbol.strip().upper().replace(".NS", "")
    if not sym:
        raise HTTPException(400, "empty symbol")
    syms = live.engine.symbols if live.engine else db.get_watchlist(settings.WATCHLIST)
    if len(syms) >= MAX_WATCHLIST:
        raise HTTPException(400, f"watchlist is limited to {MAX_WATCHLIST} symbols")
    if settings.LIVE_CONNECTOR == "upstox":
        try:
            _upstox().instruments.key(sym)
        except Exception as e:
            raise HTTPException(400, str(e))
    if live.engine:
        r = live.engine.add_symbol(sym)
        if sym in r.get("invalid", {}):
            raise HTTPException(400, r["invalid"][sym])
    db.add_to_watchlist(sym)
    db.log("admin", "watchlist_add", {"symbol": sym})
    return get_watchlist()


def add_futures() -> dict:
    from yonexis.data.connectors.upstox import INDEX_FUTURES, instruments, is_future
    syms = live.engine.symbols if live.engine else db.get_watchlist(settings.WATCHLIST)
    want = INDEX_FUTURES + [s for s in syms if not is_future(s) and s not in INDEX_FUTURES]
    have = instruments.futures_for(want)
    added = []
    for f in have:
        if f in syms or len(syms) + len(added) >= MAX_WATCHLIST:
            continue
        if live.engine:
            r = live.engine.add_symbol(f)
            if f in r.get("invalid", {}):
                continue
        db.add_to_watchlist(f)
        added.append(f)
    missing = [u for u in want if u + "-FUT" not in have]
    db.log("admin", "watchlist_add_futures", {"added": added, "missing": missing})
    return {"added": added, "missing": missing, **get_watchlist()}


def _seed_futures_once() -> None:
    try:
        r = add_futures()
        db.set_setting("futures_seeded_v1", True)
        logging.getLogger("yonexis").info("futures added to the watchlist: %s", r["added"])
    except Exception:
        logging.exception("adding futures to the watchlist failed (will retry on next start)")


@app.post("/api/watchlist/futures", dependencies=A)
def watchlist_add_futures():
    try:
        return add_futures()
    except Exception as e:
        raise HTTPException(503, f"futures list unavailable: {e}")


@app.delete("/api/watchlist/{symbol}", dependencies=A)
def remove_watchlist(symbol: str):
    sym = symbol.upper()
    if db.fetch(db.paper_trades, (db.paper_trades.c.symbol == sym) & (db.paper_trades.c.status == "open")):
        raise HTTPException(400, f"{sym} has an open paper position - close it in Paper Wallet first")
    if live.engine:
        live.engine.remove_symbol(sym)
    db.remove_from_watchlist(sym)
    db.log("admin", "watchlist_remove", {"symbol": sym})
    return get_watchlist()


@app.get("/api/quotes", dependencies=A)
def quotes(symbols: str):
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    e = live.engine
    if e and getattr(e, "feed", None):
        out = {s: e.feed.quote(s) for s in syms}
        if all(out.values()):
            return out
    try:
        return _upstox().UpstoxConnector().ltp(syms)
    except Exception as ex:
        raise HTTPException(400, str(ex))


# ====================================================================== chart (live + past prices)

CHART_INTERVALS = {"1m": 1, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}


def _resample_any(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    from yonexis.data.connectors.upstox import _resample
    if df.empty:
        return df
    if interval == "1d":
        g = df.groupby(df.index.normalize())
        out = pd.DataFrame({"open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
                            "close": g["close"].last(), "volume": g["volume"].sum()})
        out.index.name = "ts"
        return out
    return _resample(df, CHART_INTERVALS[interval])


_CHART_TRIED: dict[tuple, set] = {}


@app.get("/api/chart", dependencies=A)
def chart(symbol: str, start: str, end: str | None = None, interval: str = "5m"):
    """Candles for any past range. Order: stored real data -> Upstox (then stored) -> resampled 1m."""
    if interval not in CHART_INTERVALS:
        raise HTTPException(400, f"interval must be one of {list(CHART_INTERVALS)}")
    sym, end = symbol.upper(), end or start
    if pd.Timestamp(end) < pd.Timestamp(start):
        raise HTTPException(400, "end date is before start date")
    days = pd.bdate_range(start, end)
    df, source = db.load_candles(sym, interval, start, end, real_only=True), "database"
    have = set(df.index.normalize()) if not df.empty else set()
    today = pd.Timestamp.now().normalize()
    tried = _CHART_TRIED.setdefault((sym, interval), set())
    missing = [d for d in days if d not in have and d <= today and d not in tried]
    if missing:
        ux = _upstox()
        if ux.tokens.token:
            try:
                fetched = ux.UpstoxConnector().fetch_history(sym, str(missing[0].date()), str(missing[-1].date()), interval)
                got = set(fetched.index.normalize()) if not fetched.empty else set()
                tried.update(d for d in missing if d not in got and d < today)   # holidays: do not ask Upstox again
                if not fetched.empty:
                    today = pd.Timestamp.now().normalize()
                    db.save_candles(sym, fetched[fetched.index < today], interval, "upstox")   # never store a forming candle
                    df = fetched if df.empty else pd.concat([df, fetched])
                    df = df[~df.index.duplicated(keep="last")].sort_index()
                    source = "upstox"
            except Exception as e:
                source = f"database (Upstox fetch failed: {e})"
        if df.empty:
            target = CHART_INTERVALS[interval]
            for finer in ("5m", "1m"):
                f = CHART_INTERVALS[finer]
                if f < target and (interval == "1d" or target % f == 0):
                    base = db.load_candles(sym, finer, start, end, real_only=True)
                    if not base.empty:
                        df, source = _resample_any(normalize_candles(base), interval), f"database (built from {finer})"
                        break
    if df.empty:
        hint = "log in to Upstox on the Data tab to fetch it" if not _upstox().tokens.token else "no trading on these dates?"
        raise HTTPException(404, f"no {interval} candles for {sym} {start}..{end} ({hint})")
    df = df[(df.index >= pd.Timestamp(start)) & (df.index < pd.Timestamp(end) + pd.Timedelta(days=1))]
    zone = None
    if interval != "1d":
        last_day = df.index[-1].normalize()
        five = db.load_candles(sym, "5m", str(last_day.date()), str(last_day.date()), real_only=True)
        if five.empty:
            five = _resample_any(df[df.index >= last_day], "5m") if interval in ("1m",) else (df[df.index >= last_day] if interval == "5m" else five)
        if not five.empty:
            z = live.zone_status(normalize_candles(five))
            zone = {k: z.get(k) for k in ("zone_low", "zone_high", "zone_ts", "zone_size", "zone_pct")} if z.get("zone_high") else None
    sigs = [{"ts": str(r["ts"]), "side": r["side"], "price": r["price"], "strategy": r["strategy"]}
            for r in db.fetch(db.signals, db.real_signals() & (db.signals.c.symbol == sym) & (db.signals.c.ts >= pd.Timestamp(start).to_pydatetime())
                              & (db.signals.c.ts < (pd.Timestamp(end) + pd.Timedelta(days=1)).to_pydatetime()))]
    d = df.reset_index()
    d["ts"] = d["ts"].astype(str)
    return {"symbol": sym, "interval": interval, "source": source, "candles": d.to_dict("records"), "zone": zone, "signals": sigs}

# ====================================================================== web

if WEB_DIR.exists():
    import hashlib

    def _build_id() -> str:
        h = hashlib.sha1()
        for f in sorted(WEB_DIR.glob("*")):
            h.update(f.name.encode()); h.update(str(f.stat().st_mtime_ns).encode())
        return h.hexdigest()[:10]

    @app.middleware("http")
    async def no_stale_assets(request, call_next):
        resp = await call_next(request)
        if request.url.path.startswith("/static/") or request.url.path == "/":
            resp.headers["Cache-Control"] = "no-cache"          # always revalidate: no old dashboard after an update
        return resp

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/")
    def index():
        v = _build_id()
        html = (WEB_DIR / "index.html").read_text()
        html = html.replace('/static/style.css"', f'/static/style.css?v={v}"').replace('/static/app.js"', f'/static/app.js?v={v}"')
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    class ClientLog(BaseModel):
        message: str
        where: str = ""

    @app.post("/api/client-log", include_in_schema=False)
    def client_log(b: ClientLog):
        logging.getLogger("yonexis.web").warning("browser: %s (%s)", b.message[:2000], b.where[:300])
        return {"ok": True}


@app.exception_handler(Exception)
async def on_error(request, exc):  # pragma: no cover
    logging.exception("unhandled")
    return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})


def _unused(*_: Any) -> None:  # keep imports referenced for linters
    _ = (full_evaluation, TrainedModel)
