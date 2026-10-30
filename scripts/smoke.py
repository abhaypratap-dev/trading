import sys, json, time
sys.path.insert(0, "backend")
from yonexis.data.connectors.simulated import simulate
from yonexis.strategies import FirstRedCandleZone
from yonexis.backtest import run_backtest, full_evaluation

data = {s: simulate(s, "2024-01-01", "2025-06-30", seed=i) for i, s in enumerate(["RELIANCE", "HDFCBANK", "INFY"])}
t = time.time()
r = run_backtest(FirstRedCandleZone, data)
print("backtest secs", round(time.time() - t, 2))
print(json.dumps({k: r.metrics[k] for k in ("trades", "net_pnl", "win_rate", "profit_factor", "sharpe", "max_drawdown_pct", "total_costs")}))
print(r.trades.head(3).to_string())
t = time.time()
ev = full_evaluation(FirstRedCandleZone, data, {}, param_space={"rr": [1.5, 2.0], "sl_mode": ["opposite", "mid"]}, wf_train_days=120, wf_test_days=40)
print("eval secs", round(time.time() - t, 2))
print(ev["verdict"], ev["score"], json.dumps(ev["checks"]))
print(json.dumps(ev["monte_carlo"]))
print(json.dumps(ev["walk_forward"]["oos_metrics"]["net_pnl"]), ev["walk_forward"]["pct_profitable_folds"])
print(json.dumps(ev["regimes"]))
