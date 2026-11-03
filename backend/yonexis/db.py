"""Persistence: candles, datasets, strategies, backtests, live signals, admin feedback, models, audit log.

Runs on TimescaleDB/Postgres in Docker, or SQLite for local development.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import pandas as pd
from sqlalchemy import (Column, DateTime, Float, Integer, MetaData, String, Table, Text, UniqueConstraint,
                        create_engine, insert, select, text, update)

from .config import settings

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True, future=True)
md = MetaData()

candles = Table(
    "candles", md,
    Column("symbol", String(32), primary_key=True),
    Column("interval", String(8), primary_key=True),
    Column("ts", DateTime, primary_key=True),
    Column("open", Float), Column("high", Float), Column("low", Float), Column("close", Float), Column("volume", Float),
    Column("source", String(32)),
)

datasets = Table(
    "datasets", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(200)), Column("kind", String(32)),          # candles | trades | labels
    Column("source", String(64)), Column("rows", Integer), Column("symbols", Text),
    Column("start", String(32)), Column("end", String(32)), Column("meta", Text),
    Column("created_at", DateTime, default=datetime.utcnow),
)

strategies = Table(
    "strategies", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(120)), Column("version", Integer), Column("origin", String(32)),  # builtin|upload|ai|import
    Column("status", String(24), default="candidate"),  # candidate|approved|paper|live_small|live|retired
    Column("code", Text), Column("spec", Text), Column("params", Text), Column("review", Text),
    Column("created_at", DateTime, default=datetime.utcnow),
    UniqueConstraint("name", "version"),
)

backtests = Table(
    "backtests", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("strategy", String(120)), Column("strategy_version", Integer), Column("kind", String(24)),  # quick|full|parity
    Column("params", Text), Column("symbols", Text), Column("start", String(32)), Column("end", String(32)),
    Column("result", Text), Column("created_at", DateTime, default=datetime.utcnow),
)

signals = Table(
    "signals", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime), Column("symbol", String(32)), Column("strategy", String(120)), Column("strategy_version", Integer),
    Column("side", String(8)), Column("price", Float), Column("stop_loss", Float), Column("target", Float),
    Column("ai_score", Float), Column("admin_score", Float), Column("score", Float), Column("model_version", String(40)),
    Column("reason", Text), Column("features", Text), Column("tags", Text),
    Column("status", String(64), default="open"),   # open|taken|closed|blocked: <reason>
    Column("outcome_pnl", Float), Column("outcome", String(24)),
    Column("source", String(16)),                   # live | simulated | invalid  (simulated/invalid are hidden everywhere)
    Column("created_at", DateTime, default=datetime.utcnow),
)

feedback = Table(
    "feedback", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("signal_id", Integer), Column("ts", DateTime, default=datetime.utcnow),
    Column("action", String(16)),           # agree | reject | modify | skip
    Column("new_sl", Float), Column("new_target", Float), Column("remark", Text),
    Column("reasons", Text),                # structured tags extracted from the remark
    Column("features", Text),               # snapshot at feedback time
)

models = Table(
    "models", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("kind", String(24)), Column("version", String(40)), Column("strategy", String(120)),
    Column("path", Text), Column("metrics", Text), Column("status", String(24), default="shadow"),  # shadow|champion|retired
    Column("created_at", DateTime, default=datetime.utcnow),
)

# ----------------------------------------------------------------- paper trading (demo money, never sent to a broker)
wallet = Table(
    "wallet", md,
    Column("id", Integer, primary_key=True),
    Column("balance", Float), Column("starting_balance", Float),
    Column("created_at", DateTime, default=datetime.utcnow), Column("updated_at", DateTime, default=datetime.utcnow),
)

wallet_ledger = Table(
    "wallet_ledger", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, default=datetime.utcnow), Column("kind", String(24)),   # reset | deposit | trade
    Column("amount", Float), Column("balance_after", Float), Column("ref", String(64)), Column("note", Text),
)

paper_trades = Table(
    "paper_trades", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("signal_id", Integer), Column("strategy", String(120)), Column("symbol", String(32)), Column("side", String(8)),
    Column("qty", Integer), Column("margin", Float),
    Column("ref_price", Float), Column("entry_price", Float), Column("entry_ts", DateTime),
    Column("stop_loss", Float), Column("target", Float),
    Column("status", String(16), default="open"),      # open | closed
    Column("exit_price", Float), Column("exit_ts", DateTime), Column("exit_reason", String(24)),
    Column("gross_pnl", Float), Column("costs", Float), Column("net_pnl", Float),
    Column("last_price", Float), Column("tag", String(80)),
    Column("init_stop", Float), Column("trail", Float), Column("max_minutes", Integer), Column("best", Float),
)

app_settings = Table(
    "app_settings", md,
    Column("key", String(64), primary_key=True), Column("value", Text),
    Column("updated_at", DateTime, default=datetime.utcnow),
)

watchlist = Table(
    "watchlist", md,
    Column("symbol", String(32), primary_key=True), Column("position", Integer),
    Column("added_at", DateTime, default=datetime.utcnow),
)

audit = Table(
    "audit", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, default=datetime.utcnow), Column("actor", String(32)), Column("action", String(64)),
    Column("detail", Text),
)


news = Table(
    "news", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime), Column("symbol", String(32)), Column("source", String(40)), Column("title", Text),
    Column("url", Text), Column("kind", String(24)),          # headline | announcement
    Column("sentiment", Float), Column("tags", Text), Column("fetched_at", DateTime, default=datetime.utcnow),
    UniqueConstraint("url", "symbol", name="uq_news_url_symbol"),
)

orderflow = Table(
    "orderflow", md,
    Column("ts", DateTime, primary_key=True), Column("symbol", String(32), primary_key=True),   # 1-minute buckets (IST)
    Column("bid_qty", Float), Column("ask_qty", Float), Column("imbalance", Float),             # top-5 depth, -1..+1
    Column("tbq", Float), Column("tsq", Float),                                                 # exchange total buy/sell qty
    Column("buy_vol", Float), Column("sell_vol", Float), Column("delta", Float),               # tick-rule aggressor volume
    Column("big_buys", Integer), Column("big_sells", Integer), Column("spread_bps", Float),
)

lab_trades = Table(
    "lab_trades", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", String(24)), Column("strategy", String(120)), Column("family", String(24)),
    Column("symbol", String(32)), Column("side", String(8)), Column("signal_ts", DateTime), Column("entry_ts", DateTime),
    Column("exit_ts", DateTime), Column("entry", Float), Column("exit", Float), Column("exit_reason", String(24)),
    Column("net_pnl", Float), Column("ret_pct", Float), Column("r_mult", Float),
)

mt5_log = Table(                       # every command sent to MetaTrader 5 and its result (audit + latency)
    "mt5_log", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, default=datetime.utcnow), Column("kind", String(16)), Column("symbol", String(40)),
    Column("op", String(16)), Column("ok", Integer), Column("latency_ms", Float), Column("detail", Text),
)

openaiap_log = Table(                  # every OpenAIAP web flow run for an order, and its result
    "openaiap_log", md,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ts", DateTime, default=datetime.utcnow), Column("site", String(40)), Column("kind", String(16)), Column("symbol", String(40)),
    Column("op", String(16)), Column("ok", Integer), Column("latency_ms", Float), Column("detail", Text),
)

MIGRATIONS = [
    "ALTER TABLE paper_trades ADD COLUMN init_stop FLOAT",
    "ALTER TABLE paper_trades ADD COLUMN trail FLOAT",
    "ALTER TABLE paper_trades ADD COLUMN max_minutes INTEGER",
    "ALTER TABLE paper_trades ADD COLUMN best FLOAT",
    "ALTER TABLE signals ALTER COLUMN status TYPE VARCHAR(64)",
    "ALTER TABLE signals ADD COLUMN source VARCHAR(16)",
    "ALTER TABLE signals ADD COLUMN created_at TIMESTAMP",
    "ALTER TABLE openaiap_log ADD COLUMN site VARCHAR(40)",
]


def init() -> None:
    md.create_all(engine)
    if True:   # idempotent: statements that do not apply simply fail and are skipped
        for sql in MIGRATIONS:
            try:
                with engine.begin() as cx:
                    cx.execute(text(sql))
            except Exception:
                pass
    if engine.dialect.name == "postgresql":
        with engine.begin() as cx:
            try:
                cx.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
                cx.execute(text("SELECT create_hypertable('candles', 'ts', if_not_exists => TRUE, migrate_data => TRUE)"))
            except Exception:
                pass   # plain Postgres works too


def j(x: Any) -> str:
    return json.dumps(x, default=str)


def uj(s: str | None, default=None):
    if not s:
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def log(actor: str, action: str, detail: Any = None) -> None:
    with engine.begin() as cx:
        cx.execute(insert(audit).values(actor=actor, action=action, detail=j(detail)))


# ----------------------------------------------------------------- candles

def save_candles(symbol: str, df: pd.DataFrame, interval: str, source: str) -> int:
    if df.empty:
        return 0
    d = df.copy()
    d.columns = [c.lower() for c in d.columns]
    d = d.reset_index().rename(columns={d.index.name or "index": "ts"})
    d["ts"] = pd.to_datetime(d["ts"])
    d["symbol"], d["interval"], d["source"] = symbol.upper(), interval, source
    rows = d[["symbol", "interval", "ts", "open", "high", "low", "close", "volume", "source"]].to_dict("records")
    # replace only the span of each session present in df. (Deleting the whole first..last span wiped every day in between, e.g. a
    # chart asking Upstox for two holidays weeks apart replaced the continuous futures history of the weeks between.)
    spans = d.groupby(d["ts"].dt.normalize())["ts"].agg(["min", "max"])
    with engine.begin() as cx:
        for _, sp in spans.iterrows():
            cx.execute(candles.delete().where(candles.c.symbol == symbol.upper(), candles.c.interval == interval,
                                              candles.c.ts >= sp["min"].to_pydatetime(), candles.c.ts <= sp["max"].to_pydatetime()))
        for k in range(0, len(rows), 5000):
            cx.execute(insert(candles), rows[k:k + 5000])
    return len(rows)


def square_off_time() -> str:
    """Admin's intraday square-off time (HH:MM) for the paper engine, rebuilds and backtests. Default 15:15."""
    import time as _tm
    if _SQ_CACHE and _tm.time() - _SQ_CACHE[0] < 20:
        return _SQ_CACHE[1]
    v = get_setting("square_off_time")
    v = v if isinstance(v, str) and len(v) == 5 else "15:15"
    _SQ_CACHE[:] = [_tm.time(), v]
    return v


_SQ_CACHE: list = []


def load_candles(symbol: str, interval: str = "5m", start: str | None = None, end: str | None = None,
                 real_only: bool = False) -> pd.DataFrame:
    q = select(candles.c.ts, candles.c.open, candles.c.high, candles.c.low, candles.c.close, candles.c.volume).where(
        candles.c.symbol == symbol.upper(), candles.c.interval == interval)
    if real_only:
        q = q.where(candles.c.source != "simulated")
    if start:
        q = q.where(candles.c.ts >= pd.Timestamp(start))
    if end:
        q = q.where(candles.c.ts < pd.Timestamp(end) + pd.Timedelta(days=1))
    with engine.connect() as cx:
        df = pd.read_sql(q.order_by(candles.c.ts), cx)
    return df.set_index("ts") if not df.empty else df


def candle_inventory() -> list[dict]:
    with engine.connect() as cx:
        rows = cx.execute(text("SELECT symbol, interval, COUNT(*) n, MIN(ts) first, MAX(ts) last, MIN(source) source "
                               "FROM candles GROUP BY symbol, interval ORDER BY symbol")).mappings().all()
    return [dict(r) for r in rows]


# ----------------------------------------------------------------- generic helpers

def insert_row(table: Table, **values) -> int:
    with engine.begin() as cx:
        r = cx.execute(insert(table).values(**values))
        return int(r.inserted_primary_key[0])


def update_row(table: Table, row_id: int, **values) -> None:
    with engine.begin() as cx:
        cx.execute(update(table).where(table.c.id == row_id).values(**values))


def fetch(table: Table, where=None, order=None, limit: int | None = None) -> list[dict]:
    q = select(table)
    if where is not None:
        q = q.where(where)
    if order is not None:
        q = q.order_by(order)
    if limit:
        q = q.limit(limit)
    with engine.connect() as cx:
        return [dict(r) for r in cx.execute(q).mappings().all()]


# ----------------------------------------------------------------- watchlist
RENAMED = {"TATAMOTORS": "TMPV"}   # NSE symbol changes (Tata Motors demerger: passenger vehicles now trade as TMPV)


def get_watchlist(default: list[str]) -> list[str]:
    rows = fetch(watchlist, order=watchlist.c.position)
    if not rows:
        seed = []
        for sym in default:
            sym = RENAMED.get(sym.upper(), sym.upper())
            if sym not in seed:
                seed.append(sym)
        with engine.begin() as cx:
            for i, sym in enumerate(seed):
                cx.execute(insert(watchlist).values(symbol=sym, position=i))
        return seed
    return [r["symbol"] for r in rows]


def add_to_watchlist(symbol: str) -> None:
    rows = fetch(watchlist)
    if any(r["symbol"] == symbol for r in rows):
        return
    with engine.begin() as cx:
        cx.execute(insert(watchlist).values(symbol=symbol, position=max([r["position"] for r in rows] or [-1]) + 1))


def remove_from_watchlist(symbol: str) -> None:
    with engine.begin() as cx:
        cx.execute(watchlist.delete().where(watchlist.c.symbol == symbol))


def real_signals():
    """Condition that hides simulated / invalid calls from every live view and statistic."""
    return (signals.c.source.is_(None)) | (signals.c.source == "live")


def get_setting(key: str, default=None):
    rows = fetch(app_settings, app_settings.c.key == key)
    return uj(rows[0]["value"], default) if rows else default


def set_setting(key: str, value) -> None:
    with engine.begin() as cx:
        cx.execute(app_settings.delete().where(app_settings.c.key == key))
        cx.execute(insert(app_settings).values(key=key, value=j(value), updated_at=datetime.utcnow()))
