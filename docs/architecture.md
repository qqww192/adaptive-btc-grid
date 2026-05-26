# Architecture

## System Overview

```
Oracle Cloud VM  (Ubuntu 22.04 ARM, crontab-driven)
        │
        ├─ every 1 min ──► grid_trader.py   ← main bot loop
        │                       │
        │              ┌────────┴─────────┐
        │              ▼                  ▼
        │       cdx_client.py       risk_manager.py
        │    (crypto.com API)     (kill switch + P&L)
        │              │                  │
        │       trade_logger.py    data/weekly_state.json
        │       data/trades.json
        │
        ├─ every 4 hrs ──► regime_classifier.py
        │                       │
        │               cdx_client.py  (30 daily candles)
        │                       │
        │               data/regime.json
        │
        ├─ daily 08:00 ──► daily_reporter.py
        │                       │
        │               data/trades.json + weekly_state.json
        │                       │
        │               Telegram message
        │
        └─ Sunday 23:00 ──► gemini_optimizer.py
                                │
                        data/trades.json (last 7 days)
                        cdx_client.py    (30 daily candles)
                                │
                        Groq / Cerebras AI (walk-forward validated)
                                │
                        config/grid_params.json  ← only updated if improved
```

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Scheduler | Oracle VM crontab | Always-on, low-latency, free tier |
| All orders | `POST_ONLY` limit | Maker fee 0.25% vs taker 0.40%; profitable at 1.0% spacing (break-even 0.55%) |
| State persistence | JSON files | Simple, inspectable, easy to back up with `git` |
| Kill switch | Automatic, weekly reset | Hard stop at −10% weekly loss; resets Monday 00:00 UTC |
| Regime detection | 3-state Gaussian HMM (ATR-14 + BBW fallback) | Learns transition probabilities; classic indicators as fallback on 4-hour cadence |
| AI optimisation | Groq (Qwen3-32B) / Cerebras fallback + walk-forward | Sunday review only; walk-forward guard prevents overfitting |
| Capital reserve | ≥20% always | `capital_pct` capped at 0.80; buffer covers open BUY exposure |
| Concurrency guard | PID lock file | Prevents overlapping cron runs on slow API calls |

## Data Flow — 1-Minute Loop

1. **Lock** — write PID to `data/grid_trader.lock`; exit if lock already held
2. **Kill switch check** — read `data/weekly_state.json`; halt if active
3. **Load state** — `config/grid_params.json` + `data/regime.json` + `data/grid_state.json`
4. **Fetch price** — `GET /public/get-ticker` via `cdx_client.py`
5. **Detect fills** — diff open orders vs order history; log to `data/trades.json`
6. **Risk update** — `risk_manager.record_trade()` on each SELL fill; may trigger kill switch
7. **Recalibrate** — cancel all + reset grid if price moved >5% (`range_pct`) from last calibration
8. **Place grid** — `POST_ONLY` limit orders at each level not already occupied
9. **Save state** — write `data/grid_state.json`
10. **Unlock** — remove `data/grid_trader.lock`

## Grid Maths

```
levels          = config["levels"]          # e.g. 6
spacing_pct     = config["spacing_pct"]     # e.g. 1.0%
capital_usdt    = total_capital_gbp * capital_pct * gbp_usd_rate
per_level_usdt  = capital_usdt / levels

for i in range(levels):
    offset = (i - levels//2) * spacing_pct/100
    price  = center_price * (1 + offset)
    side   = SELL if i >= levels//2 else BUY
    qty    = per_level_usdt / price
```

## File Map

| Path | Purpose |
|---|---|
| `src/trading/cdx_client.py` | crypto.com Exchange client via CCXT (handles HMAC-SHA256 signing) |
| `src/trading/grid_trader.py` | 1-min orchestrator; single entry point |
| `src/trading/risk_manager.py` | Kill switch guardian; weekly P&L accounting |
| `src/trading/regime_classifier.py` | 3-state Gaussian HMM (+ ATR/BBW fallback); writes `data/regime.json` |
| `src/trading/gemini_optimizer.py` | Sunday AI review (Groq/Cerebras); updates `config/grid_params.json` |
| `src/trading/optuna_optimizer.py` | Saturday Optuna Bayesian sweep; writes `data/optuna_candidates.json` |
| `src/trading/ai_advisor.py` | Per-run AI trade advice (10s timeout, protects cron window) |
| `src/trading/news_sentiment.py` | Cached BTC news sentiment (FreeCryptoAPI); read by the 1-min loop |
| `src/trading/telegram_controller.py` | Interactive Telegram controller (persistent async process) |
| `src/trading/daily_reporter.py` | 08:00 UTC Telegram report |
| `src/trading/trade_logger.py` | Append-only JSON fill ledger |
| `config/grid_params.json` | Live grid parameters (committed; VM pulls nightly) |
| `data/weekly_state.json` | Weekly P&L + kill switch flag (runtime; not committed) |
| `data/trades.json` | Append-only trade ledger (runtime; not committed) |
| `data/grid_state.json` | Active order IDs + calibration price (runtime) |
| `data/regime.json` | Current market regime + indicator values (runtime) |
| `oracle_setup/setup.sh` | One-time Oracle VM setup script |
| `oracle_setup/crontab.template` | Cron job definitions with path placeholders |
