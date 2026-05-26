# BTCTradeBot — Claude Code Context

## Owner & portfolio context
- **John** — low-risk trader, prefers high success rate over high returns
- **Algo capital:** £150 BTC on crypto.com — the only money this bot controls

## Project overview
Automated BTC/USDT grid trading bot for John's crypto.com account.

**Grid Trading Bot** — adaptive BTC/USDT spot grid trader on crypto.com Exchange.
Runs continuously on an Oracle Cloud Always Free ARM VM (Ubuntu 22.04, UK Cardiff region).

## Stack
- Language: Python 3.11
- Key libs: `httpx`, `ccxt`, `python-dotenv`
- AI: Groq (Qwen3-32B) primary + Cerebras (gpt-oss-120b) fallback — weekly optimisation
- Infrastructure: Oracle Cloud Always Free ARM VM (Ubuntu 22.04, **UK Cardiff**)
- Delivery: Telegram bot
- Secrets: `.env` file on the Oracle VM — never committed to git

## Common commands
```bash
# Activate virtualenv (Oracle VM)
source .venv/bin/activate

# Run grid trader once (1-min job)
python3 src/trading/grid_trader.py

# Run regime classifier (4-hourly job)
python3 src/trading/regime_classifier.py

# Send daily report manually
python3 src/trading/daily_reporter.py

# Run weekly AI optimisation (Groq/Cerebras)
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
- `src/trading/cdx_client.py` — all crypto.com API calls via CCXT (handles HMAC-SHA256 signing).
- `src/trading/grid_trader.py` — 1-min orchestrator. Single point of entry.
- `src/trading/risk_manager.py` — kill switch guardian. Read this before touching P&L logic.
- `src/trading/regime_classifier.py` — 3-state Gaussian HMM (primary) + ATR-14/Bollinger Band Width fallback. Updates `data/regime.json`.
- `src/trading/gemini_optimizer.py` — Sunday AI review (Groq/Cerebras) with walk-forward validation.
- `config/grid_params.json` — live grid parameters. Updated by the AI optimiser; read by grid_trader.
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
- Default spacing: 1.0% (safe zone above break-even; ~0.50% net per fill)
- Weekly kill switch: -10% of total capital (£15 on £150)
- Warning threshold: -5% of total capital (£7.50)
- Grid recentres when BTC moves >5% from last calibration price (`range_pct`)
- Regime reclassification: every 4 hours via 3-state Gaussian HMM (ATR-14 + Bollinger Band Width fallback)
- **Capital viability at £150 with 6 levels:**
  - Safe up to BTC ~$224,000 (minimum capital formula: `0.0001 × BTC_price × levels ÷ capital_pct ÷ gbp_usd_rate`)
  - If BTC approaches $200k → drop to 5 levels or top up to £200
  - Bot enforces this at runtime and sends Telegram alert if breached

## Self-learning loop
- **Inner loop (every 1 min):** fill detection → risk check → order placement
- **Outer loop (Sunday 23:00 UTC):** the AI optimiser reviews 7-day metrics, proposes new params,
  validates against 30-day walk-forward simulation, updates `config/grid_params.json`

## Git workflow
- Branch from `main` for every feature or fix
- The Oracle VM runs `git pull --ff-only` daily at 01:00 UTC to pick up config changes
- Never push directly to `main`
- Commit `config/grid_params.json` after every manual param change so the VM picks it up

## Parked work — do not implement without discussion
- **Prediction markets module** (no code yet — earlier `betfair_client.py` draft was removed)
  - Polymarket: blocked — Oracle VM is in UK Cardiff, geo-blocked by Polymarket
  - Betfair: blocked — £299 live API activation fee, prohibitive for £150 capital
  - Matchbook: viable (free API, UK-regulated) but primarily sports markets
  - Smarkets: viable (£150 refundable deposit) but not yet decided
  - Decision: revisit when BTC grid is consistently profitable and capital grows
- **CFDs** — assessed and rejected; leverage risk incompatible with low-risk profile
