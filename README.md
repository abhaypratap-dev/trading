# YONEXIS — AI trading platform for NSE

Self-hosted platform that turns the admin's trading knowledge into tested strategies and AI models, runs them on live data with human review, and learns from the admin's feedback.

Full design: see the spec doc ("YONEXIS AI Trading Platform — Technical Spec").

## Quick start (macOS)

```bash
cd yonexis
bash scripts/setup_mac.sh      # Docker check, Ollama + model, .env, build, start, demo data
```

Then open **http://localhost:8000** and paste the admin token printed by the script (also in `.env`).

Requirements: Docker Desktop running, ~10 GB free disk (model + images), 16 GB RAM for the default 7B model.

Manual alternative:

```bash
cp .env.example .env            # edit ADMIN_TOKEN
brew install ollama && brew services start ollama && ollama pull qwen2.5-coder:7b
docker compose up -d --build
```

## What runs

| Service | What it does |
|---|---|
| `api` | FastAPI backend + dashboard on :8000, live engine, risk engine, paper broker |
| `sandbox` | Executes uploaded / AI-written strategy code, **no network**, read-only FS, memory + CPU limits |
| `scheduler` | Nightly retrain at 4:15 pm IST; new models start in shadow, auto-promoted only if out-of-sample AUC improves |
| `db` | TimescaleDB (Postgres) for candles, strategies, backtests, signals, feedback, models, audit |
| Ollama (on the Mac) | Local open-source LLM for the agents. `--profile ollama-docker` runs it in Docker instead (CPU-only on Mac) |

## Dashboard tabs

- **Live** — watchlist with first-red-candle zone status, chart with zone lines and signals, live AI calls with Agree / Reject / Modify / Skip + remark.
- **Backtest** — quick backtest, or full evaluation: walk-forward, locked holdout, Monte Carlo, ±10% sensitivity, cost/slippage stress, historical stress windows, regime split, deflated Sharpe, scorecard verdict, Report + Critic agents.
- **AI Builder** — explain an idea → Spec agent writes a rule card (with questions) → you confirm → Coder / Reviewer / Fixer / Optimizer agents build, test in the sandbox and evaluate.
- **Import & Parity** — paste Pine Script / AFL / Python / Excel rules → Converter agent → SDK code; upload the admin's own backtest trades → trade-by-trade parity report.
- **Data** — fetch from Yahoo / Kite / simulated, upload Excel / CSV / JSON / Parquet with auto column mapping and validation.
- **Strategies & Models** — promotion path (candidate → approved → paper → live small → live), train the meta-label model and the admin-imitation model, promote champions, backtest history.
- **Feedback** — AI alone vs admin vs combined P&L, admin accuracy by remark tag, Feedback analyst suggestions that go straight into the AI Builder.
- **Risk** — kill switch, limits, audit log.

## Data sources

| Connector | Use |
|---|---|
| `simulated` | Default live feed: replays synthetic sessions so everything works when the market is closed. **No edge — never judge a strategy on it.** |
| `yahoo` | Free, delayed. 5m history ~60 days only; daily for decades. |
| `kite` | Zerodha Kite Connect: live, history, orders. Needs `KITE_API_KEY` + daily `KITE_ACCESS_TOKEN`, static IP whitelisted. |
| upload | Admin's own data: Excel / CSV / JSON / Parquet. |
| `nse` | Bhavcopy (EOD + delivery %) and corporate actions helpers (`yonexis/data/connectors/nse.py`). |

Add a source by implementing `Connector.fetch_history` in `backend/yonexis/data/connectors/`.

## Upstox setup (live feed + orders)

1. Create an app at https://account.upstox.com/developer/apps. Set **Redirect URL** to `http://localhost:8000/api/upstox/callback`.
2. Put its API key and secret in `.env` as `UPSTOX_API_KEY` / `UPSTOX_API_SECRET` (with `LIVE_CONNECTOR=upstox`), then re-run **Start YONEXIS.command**.
3. Every trading morning: dashboard → **Data** → **Connect Upstox** and log in. Upstox tokens last one trading day.

What you get:
- **Live feed** over Upstox WebSocket v3 (`full` mode). Ticks become 1-minute candles, then the `LIVE_INTERVAL` bars the strategies use. Every 5 minutes, completed minutes are checked against Upstox's own intraday candles so reconnect gaps get filled.
- **History:** the Data tab fetches 1–15 minute candles back to Jan 2022 (one month per request) for backtests and model training.
- **Orders** only when `TRADING_MODE=live` and `BROKER=upstox`. Each entry is a MARKET order with market protection, plus an SL-M stop at Upstox. The engine closes at target or at 15:15 by cancelling the stop and exiting at market, so a stop and a target can never both fill.

## Writing strategies

```python
from yonexis.sdk import BUY, SELL, Signal, Strategy

class MyStrategy(Strategy):
    name = "my_strategy"
    params = {"rr": 2.0}

    def on_candle(self, ctx, c):
        # ctx.history / ctx.today: candles up to now only (no look-ahead possible)
        ...
        return Signal(BUY, stop_loss=..., target=...)
```

Fill rules: entry at the next candle open with slippage; stop checked before target inside a candle; gaps fill at the open; square-off 15:15; full Indian cost model (brokerage, STT, exchange, SEBI, stamp, GST — rates editable in `backtest/costs.py`, verify against your contract note).

## Going live (deliberately)

1. Strategy passes full evaluation and parity, admin sets it to **paper**, ≥ 20 sessions paper.
2. Set `TRADING_MODE=live`, `BROKER=kite`, keys in `.env`; run the stack from the static IP whitelisted with Kite.
3. Move the strategy to **live_small** (¼ size), then **live**.

SEBI's retail algo framework (in force from 1 April 2026) requires static-IP API access with daily 2FA and exchange registration of algos above the order-rate threshold; offering signals to others needs registration. v1 is built for the admin's own account.

## Development

```bash
make test      # 13 tests: fill rules, costs, no look-ahead, sandbox, parity, uploads, API
make dev       # API with SQLite + simulated feed, no Docker
```

Not financial advice. Backtests are not guarantees.
