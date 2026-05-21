# FinancialAdvisor — Claude Code Context

## Project overview
Automated BTC/USDT grid trading bot for John's crypto.com account.

**Grid Trading Bot** — adaptive BTC/USDT spot grid trader on crypto.com Exchange.
Runs continuously on an Oracle Cloud Always Free ARM VM (Ubuntu 22.04).

## Stack
- Language: Python 3.11
- Key libs: `httpx`, `python-dotenv`, `google-generativeai`
- AI: Google Gemini 1.5 Flash (weekly optimisation + search grounding)
- Infrastructure: Oracle Cloud Always Free ARM VM (Ubuntu 22.04)
- Delivery: Telegram bot
- Secrets: `.env` file on the Oracle VM — never committed to git

## Common commands
```bash
# Activate virtualenv (Oracle VM)
source .venv/bin/activate

# Run grid trader once (5-min job)
python3 src/trading/grid_trader.py

# Run regime classifier (4-hourly job)
python3 src/trading/regime_classifier.py

# Send daily report manually
python3 src/trading/daily_reporter.py

# Run weekly Gemini optimisation
python3 src/trading/gemini_optimizer.py

# Check live cron jobs
crontab -l

# Tail live grid log
tail -f logs/grid_trader.log

# View current week P&L
python3 -c "import json; print(json.load(open('data/weekly_state.json')))"

# View recent trades
tail -20 data/trades.json | python3 -m json.tool
```

## Architecture
See `docs/architecture.md`. Key points:
- `src/trading/cdx_client.py` — all crypto.com API calls. Auth via HMAC-SHA256.
- `src/trading/grid_trader.py` — 5-min orchestrator. Single point of entry.
- `src/trading/risk_manager.py` — kill switch guardian. Read this before touching P&L logic.
- `src/trading/regime_classifier.py` — ATR-14 + Bollinger Band Width. Updates `data/regime.json`.
- `src/trading/gemini_optimizer.py` — Sunday AI review with walk-forward validation.
- `config/grid_params.json` — live grid parameters. Updated by Gemini; read by grid_trader.
- `data/weekly_state.json` — current week P&L + kill switch flag. NEVER delete this.
- `data/trades.json` — append-only trade ledger. One JSON object per line.

## Constraints — read these first
- **Never commit `.env`** — it contains live API keys with trading permissions.
- **Never disable the kill switch** — it is in `risk_manager.py:is_kill_switch_active()`.
- **Never use market orders** — all orders must be `POST_ONLY` limit orders (maker fee = 0.25%).
- **Never increase `capital_pct` above 0.80** — always keep ≥20% as reserve.
- **Never modify `data/weekly_state.json` manually** — it auto-resets on Monday; to force-reset, delete the file and let `risk_manager.get_state()` recreate it.
- **Never add leverage or margin** — spot only, no derivatives.
- The crypto.com API key has trade permission but NOT withdrawal permission. Keep it that way.

## What NOT to touch
- `data/trades.json` — append-only. Never edit existing lines.
- `data/weekly_state.json` — managed entirely by `risk_manager.py`.
- `oracle_setup/setup.sh` — only modify if changing VM setup procedure.

## Grid trading key numbers
- Maker fee: 0.25% per order → minimum profitable spacing: 0.55%
- Default spacing: 0.8% (safe zone above break-even)
- Weekly kill switch: -10% of total capital (£15 on £150)
- Warning threshold: -5% of total capital (£7.50)
- Grid recentres when BTC moves >3% from last calibration price
- Regime reclassification: every 4 hours via ATR-14 + Bollinger Band Width
- **Minimum capital:** `0.0001 BTC × BTC_price × levels ÷ capital_pct ÷ gbp_usd_rate`
  - At BTC $105k / 10 levels / 70%: **£118 minimum** (default config)
  - Starter option: 4 levels → **£50 minimum** (set `"levels": 4` in grid_params.json)
  - £15 is **not viable** — per-level qty falls 13× below crypto.com's 0.0001 BTC minimum

## Self-learning loop
- **Inner loop (every 5 min):** fill detection → risk check → order placement
- **Outer loop (Sunday 23:00 UTC):** Gemini reviews 7-day metrics, proposes new params,
  validates against 30-day walk-forward simulation, updates `config/grid_params.json`

## Git workflow
- Branch from `main` for every feature or fix
- The Oracle VM runs `git pull --ff-only` daily at 01:00 UTC to pick up config changes
- Never push directly to `main`
- Commit `config/grid_params.json` after every manual param change so the VM picks it up
