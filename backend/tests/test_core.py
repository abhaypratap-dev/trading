import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from yonexis.backtest import BacktestConfig, CostModel, full_evaluation, monte_carlo, run_backtest  # noqa: E402
from yonexis.backtest.parity import compare  # noqa: E402
from yonexis.data.connectors.simulated import simulate  # noqa: E402
from yonexis.data.connectors.upload import parse_candles, parse_trades  # noqa: E402
from yonexis.sandbox import check, run  # noqa: E402
from yonexis.sdk import BUY, Signal, Strategy  # noqa: E402
from yonexis.strategies import FirstRedCandleZone  # noqa: E402

NO_COST = BacktestConfig(costs=CostModel(multiplier=0.0), slippage_bps=0.0, capital_per_trade=10_000)


def candles(rows):
    """rows: list of (HH:MM, o, h, l, c) on 2025-01-06."""
    idx = [pd.Timestamp(f"2025-01-06 {t}") for t, *_ in rows]
    return pd.DataFrame([r[1:] + (1000,) for r in rows], index=idx, columns=["open", "high", "low", "close", "volume"])


class BuyFirstCandle(Strategy):
    name = "buy_first"
    params = {}

    def on_candle(self, ctx, c):
        if ctx.state.get("done"):
            return None
        ctx.state["done"] = True
        return Signal(BUY, stop_loss=c.close - 5, target=c.close + 10)


def test_fills_next_open_and_target():
    df = candles([("09:15", 100, 101, 99, 100), ("09:20", 102, 104, 101, 103), ("09:25", 103, 113, 102, 112)])
    r = run_backtest(BuyFirstCandle, {"X": df}, config=NO_COST)
    t = r.trades.iloc[0]
    assert t.entry == 102                    # next candle open, not signal close
    assert t.exit_reason == "target" and t.exit == 110


def test_stop_first_when_both_hit():
    df = candles([("09:15", 100, 101, 99, 100), ("09:20", 100, 120, 90, 100)])
    t = run_backtest(BuyFirstCandle, {"X": df}, config=NO_COST).trades.iloc[0]
    assert t.exit_reason == "sl"


def test_gap_through_stop_fills_at_open():
    df = candles([("09:15", 100, 101, 99, 100), ("09:20", 101, 101, 100, 100), ("09:25", 90, 91, 89, 90)])
    t = run_backtest(BuyFirstCandle, {"X": df}, config=NO_COST).trades.iloc[0]
    assert t.exit_reason == "sl_gap" and t.exit == 90


def test_square_off():
    df = candles([("09:15", 100, 101, 99, 100), ("09:20", 100, 101, 99, 100), ("15:15", 101, 101, 100, 101)])
    t = run_backtest(BuyFirstCandle, {"X": df}, config=NO_COST).trades.iloc[0]
    assert t.exit_reason == "square_off"


def test_costs_positive_and_reasonable():
    c = CostModel()
    rt = c.round_trip(100_000, 100_500)
    assert 30 < rt < 120          # ~Rs 60-80 for a 1 lakh intraday round trip at a discount broker


def test_zone_strategy_rules():
    df = candles([("09:15", 100, 102, 99.5, 101), ("09:20", 101, 101.5, 100, 100.2),   # first red: zone 100-101.5
                  ("09:25", 100.5, 102, 100.4, 101.8), ("09:30", 101.9, 103, 101.8, 102.5)])
    r = run_backtest(FirstRedCandleZone, {"X": df}, config=NO_COST)
    assert r.signals[0]["side"] == "BUY" and r.signals[0]["ts"].endswith("09:25:00")
    assert r.signals[0]["tags"]["zone_low"] == 100 and r.signals[0]["tags"]["zone_high"] == 101.5


def test_no_lookahead_random_data_has_no_edge():
    data = {s: simulate(s, "2024-01-01", "2024-12-31", seed=i) for i, s in enumerate("ABC")}
    m = run_backtest(FirstRedCandleZone, data, config=BacktestConfig(costs=CostModel(multiplier=0), slippage_bps=0)).metrics
    assert abs(m["net_pnl"]) < 0.08 * 1_000_000 * 3     # no systematic gross edge on a random walk


def test_monte_carlo_shapes():
    t = pd.DataFrame({"net_pnl": [100, -50, 80, -40, 60] * 10})
    mc = monte_carlo(t, n_sims=200)
    assert mc["net_pnl_p5"] <= mc["net_pnl_p50"] <= mc["net_pnl_p95"]


def test_full_evaluation_runs():
    data = {"A": simulate("A", "2024-01-01", "2024-09-30", seed=3)}
    ev = full_evaluation(FirstRedCandleZone, data, {}, param_space={"rr": [1.5, 2.0]}, wf_train_days=60, wf_test_days=30)
    assert ev["verdict"] in ("PASS", "REVIEW", "FAIL") and "checks" in ev


def test_static_check_blocks_bad_code():
    bad = "import subprocess\nfrom yonexis.sdk import Strategy\nclass S(Strategy):\n    name='s'\n    def on_candle(self, ctx, c):\n        return eval('1')\n"
    r = check(bad)
    assert not r["ok"] and any("subprocess" in e for e in r["errors"]) and any("eval" in e for e in r["errors"])


def test_sandbox_runs_strategy():
    code = (Path(__file__).resolve().parents[1] / "yonexis/strategies/first_red_candle_zone.py").read_text()
    r = run(code, {"A": simulate("A", "2024-01-01", "2024-02-28", seed=1)}, {"kind": "quick"})
    assert r["ok"], r
    assert r["result"]["strategy"]["name"] == "first_red_candle_zone"


def test_upload_parse_and_parity():
    raw = pd.DataFrame({"Date": ["2025-01-06 09:15", "2025-01-06 09:20"], "Open": [1, 2], "High": [2, 3], "Low": [0.5, 1.5],
                        "Close": [1.5, 2.5], "Volume": [10, 20], "Ticker": ["RELIANCE.NS", "RELIANCE.NS"]})
    parsed, rep = parse_candles(raw)
    assert list(parsed) == ["RELIANCE"] and rep["symbols"]["RELIANCE"]["interval_minutes"] == 5
    ours = pd.DataFrame([{"symbol": "X", "side": "BUY", "entry_ts": "2025-01-06 09:25", "entry": 100.0, "net_pnl": 10.0}])
    theirs = parse_trades(pd.DataFrame({"Symbol": ["X"], "Type": ["Long"], "Entry Time": ["2025-01-06 09:25"], "Entry Price": [100.1]}))
    rep = compare(ours, theirs)
    assert rep["matched"] == 1 and rep["verdict"] == "PARITY"


def test_api_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("ENABLE_LIVE_LOOP", "false")
    if "yonexis.api.main" in sys.modules:
        pytest.skip("api already imported with other settings")
    os.environ["YONEXIS_DATA_DIR"] = str(tmp_path)
    os.environ["DATABASE_URL"] = f"sqlite:///{tmp_path / 't.db'}"
    os.environ["ADMIN_TOKEN"] = "t"
    import importlib

    import yonexis.config as cfg
    importlib.reload(cfg)
    import yonexis.db as db
    importlib.reload(db)
    from fastapi.testclient import TestClient

    from yonexis.api import main
    importlib.reload(main)
    with TestClient(main.app) as c:
        h = {"X-Admin-Token": "t"}
        assert c.get("/api/health").json()["ok"]
        assert c.get("/api/strategies").status_code == 401
        assert c.post("/api/data/simulate?start=2024-01-01&end=2024-03-31", json=["AAA"], headers=h).status_code == 200
        r = c.post("/api/backtests/quick", json={"strategy": "first_red_candle_zone", "symbols": ["AAA"]}, headers=h)
        assert r.status_code == 200, r.text
        assert r.json()["metrics"]["trades"] > 0


def test_touch_entry_fills_at_level_not_candle_close():
    # zone 100-101.5 from the 09:20 red candle; 09:25 trades up through 101.5 to 103 -> BUY fills at 101.5 in that candle
    df = candles([("09:15", 100, 102, 99.5, 101), ("09:20", 101, 101.5, 100, 100.2),
                  ("09:25", 100.5, 103, 100.4, 102.8), ("09:30", 102.8, 104, 102.5, 103.5)])
    r = run_backtest(FirstRedCandleZone, {"X": df}, config=NO_COST)
    t = r.trades.iloc[0]
    assert t.side == "BUY" and t.entry == 101.5 and str(t.entry_ts).endswith("09:25:00")
    assert r.signals[0]["tags"]["trigger"] == "touch" and r.signals[0]["close"] == 101.5
    # one cancels the other: no SELL afterwards even though the low is touched later
    assert len(r.trades) == 1


def test_touch_entry_gap_fills_at_open_and_pending_is_reported():
    df = candles([("09:15", 100, 101, 99, 100.5), ("09:20", 100.5, 101.5, 100, 100.2), ("09:25", 103, 104, 102.5, 103.5)])
    r = run_backtest(FirstRedCandleZone, {"X": df}, config=NO_COST)
    assert r.trades.iloc[0].entry == 103        # gapped over the level: filled at the open
    df2 = candles([("09:15", 100, 101, 99, 100.5), ("09:20", 100.5, 101.5, 100, 100.2), ("09:25", 100.5, 101.2, 100.3, 100.9)])
    r2 = run_backtest(FirstRedCandleZone, {"X": df2}, config=NO_COST)
    assert r2.trades.empty and sorted(p["side"] for p in r2.pending) == ["BUY", "SELL"]
    assert {p["level"] for p in r2.pending} == {100.0, 101.5}


def test_close_mode_still_available():
    df = candles([("09:15", 100, 102, 99.5, 101), ("09:20", 101, 101.5, 100, 100.2),
                  ("09:25", 100.5, 102, 100.4, 101.8), ("09:30", 101.9, 103, 101.8, 102.5)])
    r = run_backtest(FirstRedCandleZone, {"X": df}, {"entry_mode": "close"}, config=NO_COST)
    assert r.trades.iloc[0].entry == 101.9     # next candle's open after the 09:25 close above the zone


def test_minute_execution_follows_the_real_path():
    # 5m: zone 100-101.5 (09:20 red). The 09:25 candle touches 101.5, runs to target, then falls through the stop.
    df5 = candles([("09:15", 100, 101, 99.5, 100.6), ("09:20", 100.6, 101.5, 100, 100.2), ("09:25", 100.3, 105, 99.0, 99.2)])
    one = pd.DataFrame([(100.3, 100.8, 100.2, 100.7), (100.7, 101.7, 100.6, 101.6), (101.6, 105.0, 101.5, 104.9),
                        (104.9, 104.9, 99.5, 99.6), (99.6, 99.7, 99.0, 99.2)],
                       index=pd.date_range("2025-01-06 09:25", periods=5, freq="1min"), columns=["open", "high", "low", "close"])
    one["volume"] = 1
    coarse = run_backtest(FirstRedCandleZone, {"X": df5}, config=NO_COST).trades.iloc[0]
    fine = run_backtest(FirstRedCandleZone, {"X": df5}, config=NO_COST, exec_bars={"X": one}).trades.iloc[0]
    assert coarse.exit_reason in ("sl", "reversed")                                    # 5m only: same-candle, assume the worst
    assert fine.entry == 101.5 and str(fine.entry_ts).endswith("09:26:00")
    assert fine.exit_reason == "target" and str(fine.exit_ts).endswith("09:27:00")   # 1m path: target came first


def test_hari_master_block_detects_block_and_buys_the_retest():
    import numpy as np
    import pandas as pd
    from yonexis.backtest import BacktestConfig, run_backtest
    from yonexis.strategies.hari_master_block import HariMasterBlock
    day = pd.Timestamp("2026-10-30")
    idx = pd.date_range(day + pd.Timedelta(hours=9, minutes=15), periods=60, freq="5min")
    # down-leg to a low on candle 10 with a volume spike, rally, then come back to the block
    c = np.r_[np.linspace(105, 100.5, 11), np.linspace(101, 106, 15), np.linspace(105.8, 100.6, 12), np.full(22, 104.0)]
    o = np.r_[c[0], c[:-1]]
    df = pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.1, "low": np.minimum(o, c) - 0.1, "close": c,
                       "volume": 1000.0}, index=idx)
    df.iloc[10, df.columns.get_loc("volume")] = 9000.0                 # volume pivot on the swing low
    df.iloc[10, df.columns.get_loc("low")] = 100.2                     # a clear swing low (strictly below its neighbours)
    s = HariMasterBlock()
    st = {}
    from yonexis.sdk import Candle, Context
    for i in range(16):
        r = df.iloc[i]
        s._update_blocks(st, Candle(idx[i], r.open, r.high, r.low, r.close, r.volume))
    b = st["bull"][0]
    lo, hi = df.iloc[10].low, df.iloc[10].high
    assert b["btm"] == pytest.approx(lo) and b["top"] == pytest.approx((hi + lo) / 2) and b["ts"] == str(idx[10])
    t = run_backtest(HariMasterBlock, {"X": df}, {"min_block_pct": 0.0}, BacktestConfig(slippage_bps=0)).trades
    assert len(t) >= 1 and t.iloc[0]["side"] == "BUY" and t.iloc[0]["entry"] == pytest.approx(round(b["top"], 2))
    assert t.iloc[0]["stop_loss"] < b["btm"]
