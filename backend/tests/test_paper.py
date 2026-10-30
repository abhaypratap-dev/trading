import importlib


def _iv(df, interval):
    """Fake Upstox intraday API: 1-minute candles, or the same resampled to 5 minutes."""
    if interval == "1m":
        return df
    return df.resample("5min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("YONEXIS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'p.db'}")
    monkeypatch.setenv("PAPER_STARTING_BALANCE", "500000")
    monkeypatch.setenv("PAPER_SLIPPAGE_BPS", "0")
    import yonexis.config as cfg
    importlib.reload(cfg)
    import yonexis.db as db
    importlib.reload(db)
    import yonexis.broker.paper as paper
    importlib.reload(paper)
    import yonexis.broker as broker
    importlib.reload(broker)
    db.init()
    yield cfg, db, paper, broker


def test_wallet_starts_with_demo_money(env):
    cfg, db, paper, broker = env
    b = paper.PaperBroker()
    s = b.summary()
    assert s["balance"] == 500000 and s["available"] == 500000 and s["real_orders"] == "LOCKED"


def test_target_stop_and_costs_hit_wallet(env):
    cfg, db, paper, broker = env
    b = paper.PaperBroker()
    o = b.place("RELIANCE", "BUY", 100, 1000.0, 990.0, 1020.0, "t")
    assert b.summary()["available"] == 500000 - 100 * 1000
    assert b.manage({"RELIANCE": {"ltp": 1010}}, square_off=False) == []
    r = b.manage({"RELIANCE": {"ltp": 1021, "high": 1021, "low": 1012}}, square_off=False)[0]
    assert r["exit"] == "target" and r["price"] == 1020.0
    t = db.fetch(db.paper_trades)[0]
    assert t["gross_pnl"] == 2000 and 0 < t["costs"] < 100 and t["net_pnl"] == round(2000 - t["costs"], 2)
    assert b.wallet.get()["balance"] == round(500000 + t["net_pnl"], 2)
    # short that gaps through its stop fills at the worse price
    b.place("SBIN", "SELL", 50, 800.0, 805.0, 790.0, "t")
    r = b.manage({"SBIN": {"ltp": 812}}, square_off=False)[0]
    assert r["exit"] == "stop_loss" and r["price"] == 812.0


def test_stop_checked_before_target_and_square_off(env):
    cfg, db, paper, broker = env
    b = paper.PaperBroker()
    b.place("INFY", "BUY", 10, 100.0, 98.0, 104.0, "t")
    r = b.manage({"INFY": {"ltp": 101, "high": 105, "low": 97}}, square_off=False)[0]
    assert r["exit"] == "stop_loss"
    b.place("TCS", "BUY", 10, 100.0, 98.0, 104.0, "t")
    assert b.manage({"TCS": {"ltp": 101}}, square_off=True)[0]["exit"] == "square_off"


def test_insufficient_demo_funds_and_qty_capped(env):
    cfg, db, paper, broker = env
    b = paper.PaperBroker()
    o = b.place("MARUTI", "BUY", 1000, 11000.0, 10900.0, None, "t")     # wants 1.1 crore, wallet has 5 lakh
    assert o["qty"] == 45
    b.place("X", "BUY", 1, 5000.0, 4900.0, None, "t")
    with pytest.raises(paper.InsufficientFunds):
        b.place("Y", "BUY", 1, 50000.0, 49000.0, None, "t")


def test_reset_requires_flat_book(env):
    cfg, db, paper, broker = env
    b = paper.PaperBroker()
    b.place("A", "BUY", 1, 100.0, 99.0, None, "t")
    with pytest.raises(ValueError):
        b.wallet.reset(200000)
    b.square_off("A", 100.0)
    assert b.wallet.reset(200000) == 200000 and b.summary()["starting_balance"] == 200000


def test_real_orders_are_locked(env, monkeypatch):
    cfg, db, paper, broker = env
    monkeypatch.setattr(cfg.settings, "TRADING_MODE", "live")
    monkeypatch.setattr(cfg.settings, "BROKER", "upstox")
    assert isinstance(broker.get_broker(), paper.PaperBroker)          # live mode alone is not enough
    from yonexis.data.connectors.upstox import UpstoxConnector
    with pytest.raises(broker.OrdersLocked):
        UpstoxConnector(token="x").place_order("RELIANCE", "BUY", 1)
    with pytest.raises(broker.OrdersLocked):
        UpstoxConnector(token="x").cancel_order("1")


def test_feed_keeps_previous_session_after_midnight(monkeypatch):
    from yonexis.data.connectors import upstox as ux
    feed = ux.UpstoxFeed.__new__(ux.UpstoxFeed)
    feed.symbols, feed.builder, feed.history, feed.interval = ["X"], ux.CandleBuilder(), {}, "5m"
    idx = pd.date_range("2026-10-28 09:15", periods=75, freq="5min")
    feed.history["X"] = pd.DataFrame({"open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10}, index=idx)
    one = pd.date_range("2026-10-29 09:15", periods=375, freq="1min")
    feed.builder.bars["X"] = {t: [1, 2, 0.5, 1.5, 1] for t in one}
    now = pd.Timestamp("2026-10-30 00:30") - ux.IST_OFFSET     # just after midnight IST
    monkeypatch.setattr(pd.Timestamp, "utcnow", staticmethod(lambda: now.tz_localize("UTC")))
    feed.builder.merge_rest("X", pd.DataFrame(columns=["open", "high", "low", "close", "volume"]))
    feed.roll()
    df = feed.candles("X", "5m")
    assert str(df.index[-1]) == "2026-10-29 15:25:00"         # the 29th survives the day change
    assert df.index.normalize().nunique() == 2 and df.index.is_unique


def test_engine_paper_trades_end_to_end(env, monkeypatch):
    """Simulated feed: strategy calls -> paper fills -> exits -> wallet and signal outcomes updated."""
    cfg, db, paper, broker = env
    monkeypatch.setattr(cfg.settings, "LIVE_CONNECTOR", "simulated")
    monkeypatch.setattr(cfg.settings, "WATCHLIST", ["AAA", "BBB", "CCC"])
    import yonexis.risk.engine as rk
    importlib.reload(rk)
    import yonexis.risk as risk_pkg
    importlib.reload(risk_pkg)
    import yonexis.strategies.loader as loader
    importlib.reload(loader)
    import yonexis.live.engine as le
    importlib.reload(le)
    import inspect
    from yonexis.strategies import first_red_candle_zone as frcz
    db.insert_row(db.strategies, name="first_red_candle_zone", version=1, origin="builtin", status="paper",
                  code=inspect.getsource(frcz), params="{}", spec="{}", review="{}")
    e = le.LiveEngine()
    assert isinstance(e.broker, paper.PaperBroker)
    for _ in range(75 * 2):                       # two simulated sessions
        e.step()
        if e.sim.advance():
            e.emitted = {k for k in e.emitted if pd.Timestamp(k[2]) >= e.sim.current_day}
    trades = db.fetch(db.paper_trades)
    assert trades, "no paper trades were made"
    closed = [t for t in trades if t["status"] == "closed"]
    assert closed and all(t["exit_reason"] in ("target", "stop_loss", "square_off", "reversed") for t in closed)
    w = e.broker.wallet.get()
    assert w["balance"] == pytest.approx(500000 + sum(t["net_pnl"] for t in closed), abs=0.05)
    taken = [s for s in db.fetch(db.signals) if s["status"] == "closed"]
    assert taken and all(s["outcome_pnl"] is not None for s in taken)


def test_watchlist_seed_add_remove(env):
    cfg, db, paper, broker = env
    assert db.get_watchlist(["RELIANCE", "TATAMOTORS"]) == ["RELIANCE", "TMPV"]     # renamed NSE symbol fixed on seed
    db.add_to_watchlist("TMCV")
    db.add_to_watchlist("TMCV")
    assert db.get_watchlist([]) == ["RELIANCE", "TMPV", "TMCV"]
    db.remove_from_watchlist("RELIANCE")
    assert db.get_watchlist([]) == ["TMPV", "TMCV"]


def test_entry_past_target_is_rejected(env):
    cfg, db, paper, broker = env
    b = paper.PaperBroker()
    with pytest.raises(ValueError):
        b.place("SBIN", "BUY", 10, 972.0, 960.0, 968.0, "t")     # yesterday's target already below today's price


def test_void_out_of_hours_trades(env):
    cfg, db, paper, broker = env
    from datetime import datetime
    b = paper.PaperBroker()
    early = datetime(2026, 10, 30, 9, 4)
    b.place("HDFCBANK", "BUY", 10, 683.5, 677.0, 691.0, "t", ts=early)           # stale call replayed before the open
    b.place("SBIN", "BUY", 10, 972.0, 960.0, 980.0, "t", ts=early)
    b.close(2, 968.0, "stop_loss", ts=early)
    b.place("INFY", "BUY", 10, 1166.0, 1159.0, 1177.0, "t", ts=datetime(2026, 10, 30, 9, 35))   # legit
    bal_before = b.wallet.get()["balance"]
    loss = db.fetch(db.paper_trades, db.paper_trades.c.id == 2)[0]["net_pnl"]
    assert sorted(paper.void_out_of_hours_trades()) == [1, 2]
    assert b.wallet.get()["balance"] == round(bal_before - loss, 2)              # SBIN loss refunded
    t = {x["id"]: x for x in db.fetch(db.paper_trades)}
    assert t[1]["exit_reason"] == "void" and t[1]["status"] == "closed" and t[3]["status"] == "open"
    assert paper.void_out_of_hours_trades() == []                                # idempotent


def test_risk_settings_validation():
    from yonexis.risk.engine import apply_limits, risk
    assert apply_limits({"max_open_positions": 9})["max_open_positions"] == 9 and risk.limits.max_open_positions == 9
    with pytest.raises(ValueError):
        apply_limits({"max_open_positions": 0})
    with pytest.raises(ValueError):
        apply_limits({"no_entry_after": "16:00"})
    apply_limits({"max_open_positions": 5})


def test_live_calls_only_when_fresh(monkeypatch):
    import yonexis.live.engine as le
    e = le.LiveEngine.__new__(le.LiveEngine)
    e.sim = None
    monkeypatch.setattr(pd.Timestamp, "now", staticmethod(lambda *a, **k: pd.Timestamp("2026-10-30 10:12")))
    assert e.staleness("2026-10-30 10:05") is None                                # closed 10:10, 2 min old
    assert "old" in e.staleness("2026-10-29 15:20")                               # yesterday's call
    monkeypatch.setattr(pd.Timestamp, "now", staticmethod(lambda *a, **k: pd.Timestamp("2026-10-30 09:04")))
    assert "market closed" in e.staleness("2026-10-29 15:20")


def test_live_touch_trigger_fires_on_tick(env, monkeypatch):
    cfg, db, paper, broker = env
    import inspect
    import yonexis.live.engine as le
    from yonexis.data.connectors import upstox as ux
    from yonexis.strategies import first_red_candle_zone as frcz
    db.insert_row(db.strategies, name="first_red_candle_zone", version=1, origin="builtin", status="paper",
                  code=inspect.getsource(frcz), params="{}", spec="{}", review="{}")
    monkeypatch.setattr(cfg.settings, "LIVE_CONNECTOR", "simulated")
    e = le.LiveEngine()
    e.sim, e.symbols = None, ["X"]
    day = pd.Timestamp.now().normalize()
    idx = [day + pd.Timedelta(hours=9, minutes=15), day + pd.Timedelta(hours=9, minutes=20)]
    df = pd.DataFrame({"open": [100, 101], "high": [101, 101.5], "low": [99, 100], "close": [100.5, 100.2], "volume": [1, 1]}, index=idx)
    feed = ux.UpstoxFeed.__new__(ux.UpstoxFeed)
    feed.symbols, feed.builder, feed.history, feed.interval, feed.status = ["X"], ux.CandleBuilder(), {"X": df}, "5m", "connected"
    feed.error, feed.ticks, feed.last_msg_at, feed._keymap = None, 0, None, {}
    e.feed, e.ensure_feed = feed, (lambda: None)
    import yonexis.risk as rk
    rk.risk.ignore_clock = True
    t = day + pd.Timedelta(hours=9, minutes=26)
    monkeypatch.setattr(pd.Timestamp, "now", staticmethod(lambda *a, **k: t))
    e.step()                                                         # zone found, BUY/SELL stops armed
    assert sorted(o["side"] for o in e.armed[("first_red_candle_zone", "X")][1]) == ["BUY", "SELL"]
    feed.builder.last["X"] = {"ltp": 101.0, "ts": str(t)}
    assert e.check_triggers() == []                                  # inside the zone: nothing
    feed.builder.last["X"] = {"ltp": 101.55, "ts": str(t)}
    ev = e.check_triggers()                                          # touched the zone high -> BUY now
    assert ev and ev[0]["side"] == "BUY" and ev[0]["order"] and ev[0]["order"]["price"] == pytest.approx(101.55, abs=0.05)
    feed.builder.last["X"] = {"ltp": 99.0, "ts": str(t)}
    assert e.check_triggers() == []                                  # the SELL leg was cancelled (one cancels the other)


def test_rebuild_today_follows_the_real_minute_path(env, monkeypatch):
    cfg, db, paper, broker = env
    import inspect
    import types
    import numpy as np
    import yonexis.live.engine as le
    import yonexis.risk as rk
    from yonexis import today
    from yonexis.strategies import first_red_candle_zone as frcz
    db.insert_row(db.strategies, name="first_red_candle_zone", version=1, origin="builtin", status="paper",
                  code=inspect.getsource(frcz), params="{}", spec="{}", review="{}")
    day = pd.Timestamp.now().normalize()
    while day.dayofweek >= 5:
        day -= pd.Timedelta(days=1)
    now = day + pd.Timedelta(hours=12)
    monkeypatch.setattr(pd.Timestamp, "now", staticmethod(lambda *a, **k: now))
    idx = pd.date_range(day + pd.Timedelta(hours=9, minutes=15), now - pd.Timedelta(minutes=1), freq="1min")

    def bars(path):
        c = np.interp(np.arange(len(idx)), *zip(*path))
        o = np.r_[c[0], c[:-1]]
        wick = np.where(np.arange(len(idx)) < 5, 0.02, 0.0)          # only the zone candle has wicks
        return pd.DataFrame({"open": o, "high": np.maximum(o, c) + wick, "low": np.minimum(o, c) - wick, "close": c, "volume": 100.0}, index=idx)
    # AAA: red first candle 100->99.5, breaks up at ~09:40, runs to the 2R target by ~10:30  -> closed at target
    aaa = bars([(0, 100), (4, 99.5), (10, 100.4), (25, 101.6), (75, 108), (len(idx) - 1, 108)])
    # BBB: red first candle, breaks up, still running                                         -> open
    bbb = bars([(0, 200), (4, 199.4), (20, 200.5), (40, 200.7), (len(idx) - 1, 200.9)])
    # CCC: red first candle, then stays inside the zone                                       -> waiting
    ccc = bars([(0, 300), (4, 299.4), (len(idx) - 1, 299.7)])
    one = {"AAA": aaa, "BBB": bbb, "CCC": ccc}
    monkeypatch.setattr(cfg.settings, "LIVE_CONNECTOR", "simulated")
    e = le.LiveEngine()
    e.sim, e.symbols = None, list(one)
    e.feed = types.SimpleNamespace(conn=types.SimpleNamespace(intraday=lambda s, i: _iv(one[s], i)), session_1m=lambda s: pd.DataFrame(),
                                   history={}, quote=lambda s: {"ltp": float(one[s]["close"].iloc[-1])}, status="connected")
    # leftovers of the old book: a voided trade and a what-if trade must disappear
    for tag, reason in (("t", "void"), ("backfill-1", "target")):
        tid = db.insert_row(db.paper_trades, symbol="OLD", side="BUY", qty=1, entry_price=1, entry_ts=(day - pd.Timedelta(days=1)).to_pydatetime(),
                            status="closed", exit_reason=reason, net_pnl=5.0, tag=tag)
        paper.Wallet().book(5.0, tid, "old")
    rk.risk.limits.max_open_positions = 20
    out = today.rebuild(e)
    rows = {r["symbol"]: r for r in out["rows"]}
    assert rows["AAA"]["result"] == "target" and rows["AAA"]["side"] == "BUY" and rows["AAA"]["net_pnl"] > 0
    assert rows["AAA"]["entry_time"] < rows["AAA"]["exit_time"] <= "11:00"
    assert rows["BBB"]["result"] == "open" and "mtm" in rows["BBB"]
    assert rows["CCC"]["result"].startswith("waiting")
    trades = db.fetch(db.paper_trades)
    assert {t["symbol"] for t in trades} == {"AAA", "BBB"} and not any(t["exit_reason"] == "void" for t in trades)
    assert not any(str(t["tag"]).startswith("backfill") for t in trades)
    closed = [t for t in trades if t["status"] == "closed"][0]
    w = paper.Wallet().get()
    assert w["balance"] == pytest.approx(500000 + closed["net_pnl"], abs=0.01)     # old rows reversed, today's P&L booked
    assert "BBB" in rk.risk.state.open_positions
    sigs = db.fetch(db.signals)
    assert len(sigs) == 2 and all(s["source"] == "live" for s in sigs)
    assert ("first_red_candle_zone", "AAA") in {(k[0], k[1]) for k in e.triggered}   # the live engine will not fire again
    again = today.rebuild(e)                                                          # idempotent
    assert again["trades"] == out["trades"] and len(db.fetch(db.paper_trades)) == 2
    rk.risk.limits.max_open_positions = 5
    rk.risk.state.open_positions.clear()


def test_integrity_marks_simulated_calls_and_voids_bad_trades(env):
    cfg, db, paper, broker = env
    from datetime import datetime
    from yonexis import integrity
    importlib.reload(integrity)
    day = pd.Timestamp("2026-10-29")
    idx = pd.date_range(day + pd.Timedelta(hours=9, minutes=15), periods=120, freq="1min")
    real = pd.DataFrame({"open": 1160.0, "high": 1166.0, "low": 1155.0, "close": 1162.0, "volume": 10}, index=idx)
    db.save_candles("RELIANCE", real, "1m", "upstox_live")
    fake = db.insert_row(db.signals, ts=datetime(2026, 10, 29, 9, 35), symbol="RELIANCE", strategy="s", strategy_version=1,
                         side="BUY", price=741.0, stop_loss=735.0, target=755.0, status="blocked: max open positions (5)")
    future = db.insert_row(db.signals, ts=datetime(2027, 3, 1, 9, 35), symbol="RELIANCE", strategy="s", strategy_version=1,
                           side="BUY", price=741.0, stop_loss=735.0, target=755.0, status="open")
    good = db.insert_row(db.signals, ts=datetime(2026, 10, 29, 9, 40), symbol="RELIANCE", strategy="s", strategy_version=1,
                         side="BUY", price=1161.0, stop_loss=1150.0, target=1180.0, status="open")
    b = paper.PaperBroker()
    b.place("RELIANCE", "BUY", 100, 741.0, 735.0, 1200.0, "backfill:x", signal_id=fake, ts=datetime(2026, 10, 29, 9, 40))
    r = b.close(1, 1164.0, "manual", ts=datetime(2026, 10, 29, 10, 30))
    assert r["net_pnl"] > 40000                                        # the corrupted +42k trade
    rep = integrity.run(repair=True)
    assert rep["calls"]["invalid"] == 1 and rep["calls"]["simulated"] == 1 and rep["calls"]["verified"] == 1
    assert rep["trades"]["voided"] == 1
    assert b.wallet.get()["balance"] == 500000                          # P&L reversed, wallet back to the start
    assert rep["wallet"]["balanced"]
    src = {s["id"]: s["source"] for s in db.fetch(db.signals)}
    assert src == {fake: "invalid", future: "simulated", good: "live"}
    assert [s["id"] for s in db.fetch(db.signals, db.real_signals())] == [good]


def test_phase4_setup_on_stored_history(env, monkeypatch):
    cfg, db, paper, broker = env
    import inspect
    import yonexis.phase4 as p4
    importlib.reload(p4)
    from yonexis.data.connectors.simulated import simulate
    from yonexis.strategies import first_red_candle_zone as frcz
    db.insert_row(db.strategies, name="first_red_candle_zone", version=1, origin="builtin", status="paper",
                  code=inspect.getsource(frcz), params="{}", spec="{}", review="{}")
    for i, s in enumerate(["AAA", "BBB"]):                       # stand-in for downloaded history
        db.save_candles(s, simulate(s, "2024-01-01", "2024-12-31", seed=i), "5m", "upstox")
    monkeypatch.setattr(p4, "MIN_CANDLES", 1000)
    class E:  # minimal engine stub
        symbols = ["AAA", "BBB"]
    out = p4.run(E(), lambda *a: None, start="2024-01-01")
    ev = out["steps"][1]["strategies"]["first_red_candle_zone"]
    assert ev.get("verdict") in ("PASS", "REVIEW", "FAIL"), ev
    assert "oos_auc" in ev["model"] or "error" in ev["model"]
    assert db.fetch(db.backtests)[0]["kind"] == "full"


def test_sandbox_queue_falls_back_when_worker_is_down(tmp_path, monkeypatch):
    import inspect
    import yonexis.sandbox.runner as rn
    from yonexis.data.connectors.simulated import simulate
    from yonexis.strategies import first_red_candle_zone as frcz
    monkeypatch.setattr(rn, "MODE", "queue")
    monkeypatch.setattr(rn, "QUEUE_DIR", tmp_path / "q")
    r = rn.run(inspect.getsource(frcz), {"SIM": simulate("SIM", "2024-01-01", "2024-01-31", seed=1)}, {"kind": "quick"}, timeout=120)
    assert r["ok"] and rn.status()["mode"] == "local" and "not answering" in rn.status()["note"]


def test_sandbox_full_evaluation_streams_progress():
    import inspect
    import yonexis.sandbox.runner as rn
    from yonexis.data.connectors.simulated import simulate
    from yonexis.strategies import first_red_candle_zone as frcz
    seen = []
    data = {"SIM": simulate("SIM", "2024-01-01", "2024-06-30", seed=1)}
    r = rn.run(inspect.getsource(frcz), data, {"kind": "full", "param_space": {"rr": [1.5, 2.0]}, "wf_train_days": 40, "wf_test_days": 20},
               timeout=600, progress=lambda m, i: seen.append(m))
    assert r["ok"], r.get("error")
    text = "\n".join(seen)
    assert "1/6 development backtest" in text and "walk-forward window 1/" in text and "6/6 hold-out" in text


def test_relay_reverses_at_the_opposite_zone_edge():
    """HINDUNILVR 30-Oct pattern: SELL at zone low, price climbs back to the zone high -> close SELL, BUY there."""
    import numpy as np
    from yonexis.backtest import BacktestConfig, run_backtest
    from yonexis.strategies.first_red_candle_zone import FirstRedCandleZone
    day = pd.Timestamp("2026-10-30")
    idx = pd.date_range(day + pd.Timedelta(hours=9, minutes=15), day + pd.Timedelta(hours=15, minutes=29), freq="1min")
    path = [(0, 1858), (4, 1852), (50, 1846), (55, 1843.0), (70, 1850), (200, 1858.5), (230, 1863), (300, 1890), (len(idx) - 1, 1890)]
    c = np.interp(np.arange(len(idx)), *zip(*path))
    o = np.r_[c[0], c[:-1]]
    w = np.where(np.arange(len(idx)) < 5, 0.0, 0.0)
    one = pd.DataFrame({"open": o, "high": np.maximum(o, c) + w, "low": np.minimum(o, c) - w, "close": c, "volume": 1.0}, index=idx)
    one.iloc[0, one.columns.get_loc("low")] = 1843.7       # zone 1843.70 - 1858.00 like the chart
    one.iloc[0, one.columns.get_loc("high")] = 1858.0
    five = one.resample("5min").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
    cfg = BacktestConfig(slippage_bps=0)
    t = run_backtest(FirstRedCandleZone, {"X": five}, {}, cfg, exec_bars={"X": one}).trades
    assert list(t["side"]) == ["SELL", "BUY"]
    assert t.iloc[0]["exit_reason"] == "reversed" and t.iloc[0]["exit"] == pytest.approx(1858.0)      # closed at zone high, no buffer
    assert t.iloc[1]["entry"] == pytest.approx(1858.0) and t.iloc[1]["entry_ts"] == t.iloc[0]["exit_ts"]
    assert t.iloc[1]["stop_loss"] == pytest.approx(1843.7) and t.iloc[1]["exit_reason"] == "target"
    # relay off -> the old behaviour: one SELL stopped just above the zone high
    t2 = run_backtest(FirstRedCandleZone, {"X": five}, {"reverse": False}, cfg, exec_bars={"X": one}).trades
    assert list(t2["side"]) == ["SELL"] and t2.iloc[0]["exit"] > 1858.0


def test_futures_contracts_lots_margin_and_costs(env, monkeypatch, tmp_path):
    cfg, db, paper, broker = env
    import json as _json
    from datetime import date, timedelta
    from yonexis.data.connectors import upstox as ux
    from yonexis.backtest import BacktestConfig, run_backtest
    from yonexis.backtest.costs import CostModel
    from yonexis.data.connectors.simulated import simulate
    from yonexis.strategies.first_red_candle_zone import FirstRedCandleZone
    ms = lambda d: int(pd.Timestamp(d).tz_localize("Asia/Kolkata").timestamp() * 1000)
    near, nxt, old = date.today() + timedelta(days=10), date.today() + timedelta(days=40), date.today() - timedelta(days=5)
    rows = [{"s": "RELIANCE", "k": "NSE_EQ|INE002A01018", "n": "RELIANCE INDUSTRIES", "t": 0.05, "l": 1}]
    raw = [{"segment": "NSE_FO", "instrument_type": "FUT", "underlying_symbol": "NIFTY", "trading_symbol": "NIFTY FUT X",
            "instrument_key": k, "lot_size": 75, "expiry": ms(d)} for k, d in (("NSE_FO|1", nxt), ("NSE_FO|2", near), ("NSE_FO|0", old))]
    rows += [{"s": ux._fut_underlying(x) + "-FUT", "k": x["instrument_key"], "n": x["trading_symbol"], "t": 0.05,
              "l": x["lot_size"], "e": ux._expiry_date(x["expiry"]), "f": 1} for x in raw]
    p = tmp_path / "inst.json"
    p.write_text(_json.dumps(rows))
    inst = ux.Instruments(p)
    c = inst.contract("NIFTY-FUT")
    assert c["instrument_key"] == "NSE_FO|2" and c["lot_size"] == 75 and c["expiry"] == str(near)   # near month, expired one skipped
    assert inst.key("nifty-fut") == "NSE_FO|2" and inst.symbol("NSE_FO|1") == "NIFTY-FUT"
    assert inst.futures_for(["NIFTY", "RELIANCE"]) == ["NIFTY-FUT"]
    assert inst.search("NIFTY")[0]["symbol"] == "NIFTY-FUT"
    # futures costs: STT 0.05% only on the sell side
    fc = CostModel().for_futures()
    assert fc.order_cost(1e6, False) - fc.order_cost(1e6, True) == pytest.approx(500 - 20, abs=1)
    # backtest: futures trade whole lots regardless of capital per trade
    data = {"NIFTY-FUT": simulate("NIFTY-FUT", "2024-01-01", "2024-02-29", seed=3, base_price=22000)}
    t = run_backtest(FirstRedCandleZone, data, {}, BacktestConfig(lot_sizes={"NIFTY-FUT": 75})).trades
    assert len(t) and set(t["qty"]) == {75}
    # paper wallet blocks futures margin, not the full value
    b = paper.PaperBroker()
    o = b.place("NIFTY-FUT", "BUY", 75, 22000.0, 21900.0, 22200.0, "t")
    assert o["qty"] == 75 and o["margin"] == pytest.approx(75 * 22000 * cfg.settings.FUT_MARGIN_PCT / 100, rel=1e-3)


def test_futures_follow_the_same_relay_as_stocks(env, monkeypatch):
    cfg, db, paper, broker = env
    import inspect
    import types
    import numpy as np
    import yonexis.live.engine as le
    import yonexis.risk as rk
    from yonexis import today
    from yonexis.data.connectors import upstox as ux
    from yonexis.strategies import first_red_candle_zone as frcz
    db.insert_row(db.strategies, name="first_red_candle_zone", version=1, origin="builtin", status="paper",
                  code=inspect.getsource(frcz), params="{}", spec="{}", review="{}")
    monkeypatch.setattr(ux.instruments, "lot_size", lambda s: 75 if s.endswith("-FUT") else 1)
    monkeypatch.setattr(cfg.settings, "PAPER_STARTING_BALANCE", 5_000_000)
    day = pd.Timestamp.now().normalize()
    while day.dayofweek >= 5:
        day -= pd.Timedelta(days=1)
    now = day + pd.Timedelta(hours=15, minutes=40)
    monkeypatch.setattr(pd.Timestamp, "now", staticmethod(lambda *a, **k: now))
    idx = pd.date_range(day + pd.Timedelta(hours=9, minutes=15), day + pd.Timedelta(hours=15, minutes=29), freq="1min")
    path = [(0, 26000), (4, 25950), (50, 25890), (55, 25870), (70, 25930), (200, 26002), (230, 26040), (300, 26200), (len(idx) - 1, 26200)]
    c = np.interp(np.arange(len(idx)), *zip(*path))
    o = np.r_[c[0], c[:-1]]
    df = pd.DataFrame({"open": o, "high": np.maximum(o, c), "low": np.minimum(o, c), "close": c, "volume": 1.0}, index=idx)
    df.iloc[0, 1], df.iloc[0, 2] = 26000.0, 25940.0
    one = {"NIFTY-FUT": df, "STOCK": df.copy()}
    monkeypatch.setattr(cfg.settings, "LIVE_CONNECTOR", "simulated")
    e = le.LiveEngine()
    e.sim, e.symbols = None, list(one)
    e.feed = types.SimpleNamespace(conn=types.SimpleNamespace(intraday=lambda s, i: _iv(one[s], i)), session_1m=lambda s: pd.DataFrame(),
                                   history={}, quote=lambda s: {"ltp": float(one[s]["close"].iloc[-1])}, status="connected")
    paper.Wallet().reset(5_000_000) if hasattr(paper.Wallet(), "reset") else None
    rk.risk.limits.max_open_positions = 20
    out = today.rebuild(e)
    seq = {s: [(r["side"], r["result"]) for r in out["rows"] if r["symbol"] == s] for s in one}
    assert seq["NIFTY-FUT"] == seq["STOCK"] == [("SELL", "reversed"), ("BUY", "target")]
    fut = [t for t in db.fetch(db.paper_trades) if t["symbol"] == "NIFTY-FUT"]
    assert all(t["qty"] == 75 for t in fut) and fut[0]["exit_ts"] == fut[1]["entry_ts"]
    rk.risk.limits.max_open_positions = 5
    rk.risk.state.open_positions.clear()


def test_rebuild_scope_and_clear_history(env, monkeypatch):
    cfg, db, paper, broker = env
    import inspect
    import types
    import numpy as np
    import yonexis.live.engine as le
    import yonexis.risk as rk
    from yonexis import today
    from yonexis.strategies import first_red_candle_zone as frcz, hari_master_block as hmb
    from yonexis.strategies import loader
    for name, mod in (("first_red_candle_zone", frcz), ("hari_master_block", hmb)):
        db.insert_row(db.strategies, name=name, version=1, origin="builtin", status="paper", code=inspect.getsource(mod),
                      params="{}", spec="{}", review="{}")
    day = pd.Timestamp.now().normalize()
    while day.dayofweek >= 5:
        day -= pd.Timedelta(days=1)
    now = day + pd.Timedelta(hours=12)
    monkeypatch.setattr(pd.Timestamp, "now", staticmethod(lambda *a, **k: now))
    idx = pd.date_range(day + pd.Timedelta(hours=9, minutes=15), now - pd.Timedelta(minutes=1), freq="1min")
    c = np.interp(np.arange(len(idx)), [0, 4, 25, 75, len(idx) - 1], [100, 99.5, 101.6, 108, 108])
    o = np.r_[c[0], c[:-1]]
    w = np.where(np.arange(len(idx)) < 5, 0.02, 0.0)
    bars = pd.DataFrame({"open": o, "high": np.maximum(o, c) + w, "low": np.minimum(o, c) - w, "close": c, "volume": 100.0}, index=idx)
    one = {"AAA": bars, "BBB": bars.copy()}
    monkeypatch.setattr(cfg.settings, "LIVE_CONNECTOR", "simulated")
    e = le.LiveEngine()
    e.sim, e.symbols = None, list(one)
    e.feed = types.SimpleNamespace(conn=types.SimpleNamespace(intraday=lambda s, i: _iv(one[s], i)), session_1m=lambda s: pd.DataFrame(),
                                   history={}, quote=lambda s: {"ltp": float(one[s]["close"].iloc[-1])}, status="connected")
    rk.risk.limits.max_open_positions = 20
    today.rebuild(e)                                                       # unscoped: both stocks
    assert {t["symbol"] for t in db.fetch(db.paper_trades)} == {"AAA", "BBB"}
    out = today.rebuild(e, scope={"symbols": ["AAA"], "strategy": "first_red_candle_zone", "version": 1})
    assert {t["symbol"] for t in db.fetch(db.paper_trades)} == {"AAA"}     # only the selection is kept
    assert {t["strategy"] for t in db.fetch(db.paper_trades)} == {"first_red_candle_zone"} and "v1" in out["strategy"]
    assert loader.get_scope()["symbols"] == ["AAA"] and loader.in_scope("AAA") and not loader.in_scope("BBB")
    assert [r["name"] for r, _ in loader.trading_strategies()] == ["first_red_candle_zone"]
    r = paper.clear_history(700000, include_calls=True)
    assert r["removed_trades"] >= 1 and not db.fetch(db.paper_trades) and not db.fetch(db.signals)
    assert paper.Wallet().get()["balance"] == 700000
    rk.risk.limits.max_open_positions = 5


def test_rebuild_a_past_date(env, monkeypatch):
    cfg, db, paper, broker = env
    import inspect
    import types
    import numpy as np
    import yonexis.live.engine as le
    import yonexis.risk as rk
    from yonexis import today
    from yonexis.strategies import first_red_candle_zone as frcz, loader
    db.insert_row(db.strategies, name="first_red_candle_zone", version=1, origin="builtin", status="paper",
                  code=inspect.getsource(frcz), params="{}", spec="{}", review="{}")
    now = pd.Timestamp.now().normalize() + pd.Timedelta(hours=11)
    past = now.normalize() - pd.Timedelta(days=1)
    while past.dayofweek >= 5:
        past -= pd.Timedelta(days=1)
    monkeypatch.setattr(pd.Timestamp, "now", staticmethod(lambda *a, **k: now))
    idx = pd.date_range(past + pd.Timedelta(hours=9, minutes=15), past + pd.Timedelta(hours=15, minutes=29), freq="1min")
    c = np.interp(np.arange(len(idx)), [0, 4, 25, 75, 200, len(idx) - 1], [100, 99.5, 101.6, 104, 103, 103.5])
    o = np.r_[c[0], c[:-1]]
    w = np.where(np.arange(len(idx)) < 5, 0.02, 0.0)
    bars = pd.DataFrame({"open": o, "high": np.maximum(o, c) + w, "low": np.minimum(o, c) - w, "close": c, "volume": 100.0}, index=idx)
    calls = []
    conn = types.SimpleNamespace(intraday=lambda s, i: pd.DataFrame(),
                                 fetch_history=lambda s, a, b, i: (calls.append((s, a, b, i)), _iv(bars, i))[1])
    monkeypatch.setattr(cfg.settings, "LIVE_CONNECTOR", "simulated")
    e = le.LiveEngine()
    e.sim, e.symbols = None, ["AAA"]
    e.feed = types.SimpleNamespace(conn=conn, session_1m=lambda s: pd.DataFrame(), history={}, quote=lambda s: {}, status="connected")
    keep = db.insert_row(db.paper_trades, symbol="TODAY", side="BUY", qty=1, entry_price=1, entry_ts=now.to_pydatetime(), status="open")
    rk.risk.limits.max_open_positions = 20
    out = today.rebuild(e, scope={"symbols": ["AAA"], "strategy": "first_red_candle_zone", "version": 1}, date=str(past.date()))
    assert out["date"] == str(past.date()) and not out["is_today"] and out["zone_candles"] == "Upstox 5-minute"
    assert ("AAA", str(past.date()), str(past.date()), "5m") in calls and ("AAA", str(past.date()), str(past.date()), "1m") in calls
    trades = [t for t in db.fetch(db.paper_trades) if t["symbol"] == "AAA"]
    assert trades and all(pd.Timestamp(t["entry_ts"]).normalize() == past and t["status"] == "closed" for t in trades)
    assert db.fetch(db.paper_trades, db.paper_trades.c.id == keep)            # today's trade untouched
    assert loader.get_scope() is None                                          # a past date never changes live trading
    with pytest.raises(RuntimeError, match="weekend"):
        sat = past - pd.Timedelta(days=(past.dayofweek - 5) % 7)              # the Saturday before
        today.rebuild(e, date=str(sat.date()))
    rk.risk.limits.max_open_positions = 5


def test_strategy_lab_trains_ai_picker_and_ai_pick_rebuild(env, monkeypatch):
    cfg, db, paper, broker = env
    import types
    from yonexis import lab, today
    from yonexis.api.main import seed_builtins
    from yonexis.data.connectors.simulated import simulate
    seed_builtins()
    syms = ["AAA", "BBB", "CCC"]
    for i, s in enumerate(syms):
        db.save_candles(s, simulate(s, "2024-01-01", "2024-04-30", seed=i), "5m", "upstox")
    monkeypatch.setattr(cfg.settings, "WATCHLIST", syms)
    logs = []
    out = lab.run(None, lambda m, i: logs.append(m), start="2024-01-01", include_pairs=True)
    names = {b["strategy"] for b in out["leaderboard"]}
    assert {"first_red_candle_zone", "hari_master_block", "momentum", "orb", "pair_trading"} <= names
    assert all("error" not in b for b in out["leaderboard"]), [b for b in out["leaderboard"] if "error" in b]
    assert out["selector"] and out["selector"]["oos_rows"] > 0 and len(db.fetch(db.lab_trades)) > 100
    model, row = lab.champion_selector()
    assert model is not None and row["kind"] == "selector"
    assert any("AI picker" in m for m in logs)
    # AI pick on a past day: every strategy proposes, the picker keeps the best per stock
    import yonexis.live.engine as le
    day = pd.Timestamp("2024-04-29")
    def hist(s, a, b, i):
        df = db.load_candles(s, "5m", a, b, real_only=True)
        return df
    conn = types.SimpleNamespace(intraday=lambda s, i: pd.DataFrame(), fetch_history=hist)
    monkeypatch.setattr(cfg.settings, "LIVE_CONNECTOR", "simulated")
    e = le.LiveEngine()
    e.sim, e.symbols = None, syms
    e.feed = types.SimpleNamespace(conn=conn, session_1m=lambda s: pd.DataFrame(), history={}, quote=lambda s: {}, status="connected")
    r = today.rebuild(e, scope={"symbols": syms, "strategy": "ai_pick", "version": 1}, date=str(day.date()))
    assert r["strategy"] == "AI pick" and r["ai"]["candidates"] >= r["ai"]["picked"]
    for t in db.fetch(db.paper_trades):
        assert pd.Timestamp(t["entry_ts"]).normalize() == day
    r2 = today.rebuild(e, scope={"symbols": syms, "strategy": "pair_trading", "version": 1}, date=str(day.date()))
    assert r2["strategy"] == "pair trading"


def test_ai_selectivity_threshold_is_validated_on_later_data():
    import numpy as np
    from yonexis.ml import selector
    rng = np.random.default_rng(0)
    n = 4000
    ts = pd.date_range("2025-01-01 09:30", periods=n, freq="37min")
    p = rng.uniform(0.2, 0.7, n)
    win = rng.uniform(size=n) < p                                     # higher p really wins more often
    test = pd.DataFrame({"_ts": ts, "_entry_ts": ts, "_exit_ts": ts + pd.Timedelta(minutes=5), "_symbol": [f"S{i % 40}" for i in range(n)],
                         "_strategy": "x", "p": p, "ev": p * 2 - (1 - p), "label": win.astype(int),
                         "_net_pnl": np.where(win, 900.0, -500.0)})
    sel = selector.selectivity(test)
    assert len(sel["table"]) == len(selector.THRESHOLDS)
    strict = [r for r in sel["table"] if r["min_p"] == 0.6][0]["all"]
    loose = [r for r in sel["table"] if r["min_p"] == 0.35][0]["all"]
    assert loose["trades"] > strict["trades"] > 0 and strict["win_rate"] > loose["win_rate"]
    assert sel["validation"]["passed"] and sel["recommended_min_p"] in selector.THRESHOLDS
