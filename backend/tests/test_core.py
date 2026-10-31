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


def test_zone_dashboard_rules():
    import pandas as pd
    from yonexis.zone_lab import run_day
    day = pd.Timestamp("2026-10-30")
    rows = [(100, 101, 99, 99.5), (99.5, 100.5, 99.2, 100), (100, 101.2, 99.8, 101), (101, 101.5, 98.8, 99),
            (99, 99.5, 96.5, 97), (97, 97.2, 95.8, 96), (96, 99.3, 95.9, 99.2), (99.2, 100, 99.1, 99.8), (99.8, 101.3, 99.7, 101.1)]
    idx = [day + pd.Timedelta(hours=9, minutes=15 + 5 * i) for i in range(len(rows))]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1.0
    t = run_day("X", df, {"max_cycles": 2, "target_mult": 1.5, "include_costs": False})["trades"]
    # zone 99-101 (range 2): BUY at 101, stopped at 99 (exact), relay SELL at 99, target 99 - 3 = 96 (exact)
    assert [(x["side"], x["entry"], x["exit"], x["exit_reason"]) for x in t] == [("BUY", 101, 99, "stop_loss"), ("SELL", 99, 96, "target")]
    t3 = run_day("X", df, {"max_cycles": 3, "target_mult": 1.5, "include_costs": False})["trades"]
    assert t3[2]["side"] == "BUY" and t3[2]["entry_ts"].endswith("09:55:00")     # a fresh break of the zone, not the 09:45 retest
    assert run_day("X", df, {"max_cycles": 1, "target_mult": 1.5})["trades"][0]["exit_reason"] == "stop_loss"


def test_zone_dashboard_lots_with_own_targets():
    import pandas as pd
    from yonexis.zone_lab import run, run_day
    day = pd.Timestamp("2026-10-30")
    rows = [(100, 101, 99, 99.5), (99.5, 100.5, 99.2, 100), (100, 101.2, 99.8, 101), (101, 103.2, 100.9, 103),
            (103, 105.5, 102.8, 105), (105, 105.2, 102, 102.5)]
    idx = [day + pd.Timedelta(hours=9, minutes=15 + 5 * i) for i in range(len(rows))]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1.0
    t = run_day("X", df, {"max_cycles": 1, "lot_targets": [1, 2], "include_costs": False})["trades"]
    # zone 99-101 range 2: BUY at 101; lot 1 target 103 (hit 09:30), lot 2 target 105 (hit 09:35)
    assert [(x["lot"], x["target"], x["exit"], x["exit_reason"], x["exit_ts"][11:16]) for x in t] == \
        [(1, 103, 103, "target", "09:30"), (2, 105, 105, "target", "09:35")]


def _zone_df(rows):
    import pandas as pd
    day = pd.Timestamp("2026-10-30")
    idx = [day + pd.Timedelta(hours=9, minutes=15 + 5 * i) for i in range(len(rows))]
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx)
    df["volume"] = 1.0
    return df


LOTS_ROWS = [(100, 101, 99, 99.5), (99.5, 100.5, 99.2, 100), (100, 101.2, 99.8, 101), (101, 103.2, 100.9, 103),
             (103, 105.5, 102.8, 105), (105, 105.2, 102, 102.5)]


def test_zone_target_base_iz():
    from yonexis.zone_lab import run_day
    df = _zone_df(LOTS_ROWS)
    # BUY at 101 in the 09:25 candle (high 101.2, low 99.8, range 1.4), multiplier 1.5: target = entry + range x 1.5
    iz = run_day("X", df, {"max_cycles": 1, "target_mult": 1.5, "target_base": "iz", "include_costs": False})["trades"][0]
    assert iz["target"] == round(101 + 1.4 * 1.5, 2) and iz["exit_ts"][11:16] == "09:30" and iz["iz_range"] == 1.4
    zn = run_day("X", df, {"max_cycles": 1, "target_mult": 1.5, "include_costs": False})["trades"][0]
    assert zn["target"] == 104 and zn["exit_ts"][11:16] == "09:35"            # 101 + zone range 2 x 1.5


def test_zone_spike_off_needs_open_or_close_outside():
    from yonexis.zone_lab import run_day
    rows = [(100, 101, 99, 99.5), (99.5, 100.5, 99.2, 100), (100, 101.2, 99.8, 101), (101, 101.5, 98.8, 99),
            (99, 99.5, 96.5, 97), (97, 97.2, 95.8, 96), (96, 99.3, 95.9, 99.2)]
    t = run_day("X", _zone_df(rows), {"max_cycles": 2, "target_mult": 1.5, "spike": False, "include_costs": False})["trades"]
    # wicks above 101 / below 99 are ignored; the 09:35 candle CLOSES at 97 below 99 -> SELL at 97, target 97 - 2 x 1.5 = 94
    assert [(x["side"], x["entry"], x["entry_ts"][11:16], x["target"]) for x in t][0] == ("SELL", 97, "09:35", 94)
    on = run_day("X", _zone_df(rows), {"max_cycles": 2, "target_mult": 1.5, "include_costs": False})["trades"]
    assert on[0]["side"] == "BUY" and on[0]["entry"] == 101


def test_zone_spike_off_stop_on_close_and_relay():
    from yonexis.zone_lab import run_day
    rows = [(100, 101, 99, 99.5), (99.5, 101.5, 99.5, 101.4), (101.4, 101.6, 98.5, 100), (100, 100.2, 98.6, 98.7), (98.7, 98.8, 95, 95.5)]
    t = run_day("X", _zone_df(rows), {"max_cycles": 2, "target_mult": 1.0, "spike": False, "include_costs": False})["trades"]
    # BUY at the 09:20 close 101.4; 09:25 dips to 98.5 but closes 100 (inside): no stop; 09:30 closes 98.7 -> stop + relay SELL 98.7
    assert [(x["side"], x["entry"], x["exit"], x["exit_reason"]) for x in t] == [("BUY", 101.4, 98.7, "stop_loss"), ("SELL", 98.7, 96.7, "target")]


def test_zone_square_off_time_inside_a_candle():
    from yonexis.zone_lab import run_day
    df = _zone_df(LOTS_ROWS)
    p = {"max_cycles": 1, "target_mult": 10, "include_costs": False}
    assert run_day("X", df, {**p, "square_off": "09:37"})["trades"][0]["exit"] == 105        # 09:35 candle's close
    t = run_day("X", df, {**p, "square_off": "09:35"})["trades"][0]
    assert t["exit"] == 103 and t["exit_reason"] == "square_off"                               # 09:35 candle's open


def test_zone_ai_search_matches_plain_replays():
    import numpy as np
    import pandas as pd
    from yonexis import zone_ai
    from yonexis.zone_lab import config_label, run_day
    rng = np.random.default_rng(7)
    frames = []
    for d in pd.bdate_range("2026-03-02", periods=12):
        idx = pd.date_range(d + pd.Timedelta(hours=9, minutes=15), periods=75, freq="5min")
        c = 100 + np.cumsum(rng.normal(0, 0.35, 75))
        o = np.r_[100.2, c[:-1]]
        frames.append(pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.15, "low": np.minimum(o, c) - 0.15, "close": c, "volume": 5.0}, index=idx))
    df = pd.concat(frames)
    grid = {**zone_ai.DEFAULT_GRID, "lot1": [1.5, 3], "lot2": [3, 8], "entries": [1, 2], "sizes": [1, 2]}
    cfgs = zone_ai.build_configs(grid)
    assert (("zone", True, 1, 3.0, 3.0) in cfgs) and not any(a > b for *_, a, b in cfgs)
    r = zone_ai._symbol_job(("A-FUT", 50, df.copy(), {"include_costs": True}, grid, cfgs, False))
    for c in [("zone", True, 2, 1.5, 8.0), ("iz", False, 1, 3.0, 8.0), ("zone", False, 2, 3.0, 3.0)]:
        want = 0.0
        for d, g in df.groupby(df.index.normalize()):
            want += sum(t["net_pnl"] for t in run_day("A-FUT", g, {**zone_ai.cfg_dict(c), "include_costs": True}, 50)["trades"] if t["exit_reason"] != "open")
        assert abs(float(r["nets"][cfgs.index(c)].sum()) - want) < 0.05 * max(1, len(df) / 75)
    r2 = zone_ai._symbol_job(("B-FUT", 25, df.copy() * 1.01, {"include_costs": True}, grid, cfgs, False))
    out = zone_ai.analyse([r, r2], cfgs, grid, "2026-03-02", "2026-03-17", False, config_label)
    assert [s["n"] for s in out["sets"]] == [1, 2] and out["sets"][1]["honest"]["symbols"]
    assert out["split"] > "2026-03-02" and len(out["top_configs"]) <= 15
    h = out["sets"][1]["honest"]                                 # both futures: 2 lots x lot x last close x 15%
    want = sum(2 * lot * px for lot, px in ((50, float(df["close"].iloc[-1])), (25, float(df["close"].iloc[-1]) * 1.01))) * 0.15
    assert abs(h["investment"] - want) < 1 and h["return_pct"] == round(h["full_net"] / h["investment"] * 100, 2)


def test_llm_can_be_switched_off(monkeypatch):
    import pytest
    from yonexis.agents import llm, pipeline
    from yonexis.config import settings
    monkeypatch.setattr(settings, "LLM_ENABLED", False)
    assert llm.health()["disabled"] is True
    with pytest.raises(llm.LLMError):
        llm.ask("s", "u")
    assert pipeline.critic({"x": 1}).startswith("(critic unavailable")
