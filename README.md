# adaptive-btc-grid

Adaptive BTC/USDT spot grid trading bot for crypto.com Exchange, running on Oracle Cloud Always Free (ARM, Ubuntu 22.04).

Designed for consistent, low-risk returns through regime-aware grid placement, AI-driven weekly optimisation, and a Telegram control panel.

---

## How it works

```
Oracle Cloud VM (cron scheduler)
        │
        ├─► every 1 min   → src/trading/grid_trader.py
        │                     fill detection → risk check → order placement
        │                     (skips if manual pause or HMM trend pause active)
        │
        ├─► every 4 hrs   → src/trading/regime_classifier.py
        │                     HMM (3-state Gaussian) + ATR-14 + BBW
        │                     → data/regime.json + trend_pause.flag
        │
        ├─► daily 08:00   → src/trading/daily_reporter.py
        │                     P&L summary → Telegram
        │
        ├─► Saturday 22:30→ src/trading/optuna_optimizer.py
        │                     Bayesian sweep (300 trials) → top-3 param candidates
        │
        └─► Sunday 23:00  → src/trading/gemini_optimizer.py
                              multi-agent AI review → walk-forward validation
                              → config/grid_params.json
```

**Persistent process (systemd):**
```
src/trading/telegram_controller.py  — two-way Telegram control panel
```

---

## Key features

- **Asymmetric grid** — 4 buy / 2 sell levels in ranging markets; flips for trending regimes
- **Trend pause** — grid stands aside automatically when HMM confidence ≥ 70% in a trend, avoiding recenter fee drag
- **Live capital sizing** — fetches real USDT + BTC portfolio value from crypto.com on every run; no hardcoded capital
- **CDaR-based capital step-down** — reduces deployed capital before the kill switch fires
- **Weekly AI optimisation** — Groq (Qwen3-32B) primary + Cerebras fallback; 3 specialist agents debate params
- **Bayesian param search** — Optuna TPE sweep on Saturday feeds regime-aware candidates to Sunday AI review
- **Kill switch** — weekly −10% drawdown limit; auto-resets Monday 00:00 UTC
- **POST_ONLY only** — all orders are maker limit orders; no market orders ever

---

## Telegram commands

```
/status      — Live P&L, BTC price, portfolio, regime, heartbeat
/params      — Current grid config
/pause       — Manually pause the bot (open orders stay on exchange)
/resume      — Resume from manual pause
/trendresume — Override auto trend-pause
/recenter    — Force grid rebuild around current BTC price
/kills       — Kill switch status and week P&L
/optuna      — Last Saturday's Bayesian candidates
/help        — All commands + inline control panel
```

---

## Setup

See **[oracle_deployment.md](oracle_deployment.md)** for the full VM provisioning guide.

### 1. Clone and install

```bash
git clone https://github.com/qqww192/adaptive-btc-grid.git
cd adaptive-btc-grid
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements_trading.txt
```

### 2. Configure secrets

```bash
cp .env.example .env
nano .env   # fill in all values
```

| Variable | Where to get it |
|---|---|
| `CDX_API_KEY` | crypto.com Exchange → API Management (Trade permission only) |
| `CDX_API_SECRET` | same as above |
| `GROQ_API_KEY` | console.groq.com → API Keys (free) |
| `CEREBRAS_API_KEY` | cloud.cerebras.ai → API Keys (free, fallback) |
| `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → /newbot |
| `TELEGRAM_CHAT_ID` | Telegram → @userinfobot |
| `GBP_USD_RATE` | Current GBP/USD rate (e.g. 1.35) |

### 3. Deploy crontab

```bash
crontab oracle_setup/crontab.template
```

### 4. Start Telegram controller

```bash
sudo systemctl enable btcbot-controller
sudo systemctl start btcbot-controller
```

See `oracle_deployment.md` for the systemd service file.

---

## Project structure

```
src/
  trading/
    grid_trader.py          — 1-min grid orchestrator (main entry point)
    cdx_client.py           — crypto.com Exchange API (CCXT + live portfolio fetch)
    risk_manager.py         — kill switch, CDaR capital step-down
    regime_classifier.py    — HMM + ATR/BBW regime detection, trend pause flag
    ai_advisor.py           — Groq/Cerebras AI calls (recenter, regime override)
    gemini_optimizer.py     — Sunday multi-agent AI param optimisation
    optuna_optimizer.py     — Saturday Bayesian parameter sweep
    daily_reporter.py       — Daily Telegram P&L report
    telegram_controller.py  — Persistent Telegram bot (aiogram v3)
  backtesting/
    grid_backtest.py        — Fixed-level FIFO backtester (matches live topology)
    backtest_2020_2026.py   — 2020–2025 historical comparison (3 scenarios)
config/
  grid_params.json          — Live grid parameters (updated by AI every Sunday)
data/
  weekly_state.json         — Current week P&L + kill switch state (never delete)
  trades.json               — Append-only trade ledger
  regime.json               — Latest regime classification
oracle_setup/
  setup.sh                  — One-shot VM provisioning script
  crontab.template          — Production crontab
```

---

## Safety constraints

- All orders `POST_ONLY` — maker fee 0.25%, never taker
- `capital_pct` capped at 0.80 — minimum 20% reserve always maintained
- Kill switch at −10% weekly loss — auto-resets Monday
- Spot only — no leverage, no margin, no derivatives
- API key: Trade permission only, no withdrawal permission
- `data/weekly_state.json` and `data/trades.json` are never modified manually

---

## Backtested performance (Jan 2020 – May 2025, £150 capital)

| Config | Net P&L | Total return | Win rate | Max drawdown |
|---|---|---|---|---|
| Old (0.8% spacing, symmetric) | £+662 | +441% | 57.7% | £2.92 |
| **New (1.0% spacing, asymmetric + trend pause)** | **£+675** | **+450%** | **67.3%** | **£1.93** |

*OHLC daily model — real returns estimated at 40–60% of simulated due to price path and fill assumptions.*
