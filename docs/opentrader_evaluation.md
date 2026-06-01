# OpenTrader evaluation + strategy / fee research

_Date: 2026-06-01. Context: John asked whether adopting **OpenTrader**
(github.com/Open-Trader/opentrader) would give higher win-rate/profits, with
Telegram as the main communicator, on crypto.com, running several bots at once,
starting from £170 with ~20% active per week and a pause-on-loss rule._

## TL;DR — OpenTrader is evaluated and parked (not adopted)

A public framework does **not** contain a magically "better" strategy. OpenTrader
ships the **same commodity strategies** this bot already has (grid / DCA / RSI).
Win-rate and profit come from **parameter tuning and regime fit**, not the
framework. Two hard mismatches sealed the decision:

1. **No native Telegram.** OpenTrader is web-UI driven (a tRPC control panel on
   `localhost:8000`). "Telegram as the main communicator" — John's hard
   requirement — would have to be **built from scratch** on top of its API.
   This bot already has a mature two-way aiogram controller.
2. **It would not fix the actual problem.** The real issue (see below) was that
   our own bot pauses itself out of the market during trends. OpenTrader's grid
   has *no* regime awareness, so it would trade *more* but simply swap "no
   income" for "trend losses." It does not raise win rate.

**Decision:** keep and fix our own Python bot. Revisit OpenTrader only if we ever
want a multi-exchange web dashboard.

## OpenTrader — factual assessment

| Dimension | Finding |
|---|---|
| Stack / licence | TypeScript/Node 22+, React UI, Prisma/SQLite, Apache-2.0, beta (`v1.0.0-beta.x`), ~2.7k★ |
| Telegram | **None built-in** — Telegram is only a community support channel |
| Control surface | **tRPC** on `:8000` (`bot.list/getOne/start/stop/backtest/completedSmartTrades`), auth-gated |
| Exchanges | CCXT-based; first-class OKX/Bybit/Binance/Kraken/Coinbase/Gate/Bitget. **crypto.com works via CCXT** (`cryptocom`) but is not first-class |
| Strategies | GRID, DCA, RSI, + custom TS strategy interface |
| Multi-bot | **Yes** — manage many bots/pairs at once from the UI |
| Paper / backtest | Backtest via CLI (`opentrader backtest grid --from … --to … -t 1h`); paper trading via UI |
| Deploy | `npm i -g opentrader` or Docker (Alpine, ARM-compatible → runs on the Oracle VM) |
| Orders / fees | Spot limit/market via CCXT, `postOnly` for maker-only; no bot fees (exchange fees only) |

If a hybrid is ever wanted, the cleanest seam is to read OpenTrader's SQLite DB
**read-only** from Python for status/PnL and call its tRPC `bot.start/stop` for
control — but this is **not** being built now.

## crypto.com Exchange fees (2026, verified)

- **Base tier: 0.25% maker / 0.50% taker** (your existing assumption still holds).
- Maker can reach 0% only at very high volume + CRO staking — irrelevant at £170.
- **Implication, unchanged:** all orders stay **POST_ONLY / maker-only**, and the
  **minimum profitable grid spacing is 0.55%** (covers the 0.25% maker leg each
  side); we run **1.0%** base for a comfortable net margin.

## Highest-volume non-stablecoin coins (2026)

BTC, ETH, **XRP, SOL, BNB** — all tradeable on crypto.com. Per the agreed scope we
**concentrate on BTC + ETH**: at £34 active, spreading across 5 coins pushes order
sizes to the exchange minimum and bleeds fees. BTC/ETH are the most liquid and the
cheapest to grid.

## "Top 5" strategies, ranked for £170 active, low-risk, maker-only spot

Honest framing first: a **high win-rate is not the same as profitability**. Grid
and mean-reversion show 80–90% "win rates" but carry tail risk; judge by
**expectancy + drawdown**, and expect live results **30–50% worse than backtest**.

1. **Grid (range-bound)** — best fit for BTC/ETH consolidation; high fill rate;
   tail risk on breakout (mitigated by our trend handling + kill switch). *In use.*
2. **DCA** — lowest risk, ideal for small capital, maker-friendly. Good complement.
3. **Mean-reversion (RSI/Bollinger)** — decent in chop; needs tight stops; higher
   tail risk on momentum breakouts.
4. **Trend-following** — fewer trades, lower fee drag; earns in exactly the regime
   where grids struggle. This is the **complementary sleeve** (see plan Step 3).
5. **Market-making / arbitrage** — **unsuitable at £170**: spreads evaporate after
   fees; needs $5k+ and multi-venue infra. Listed for completeness, not deployed.

## What was actually wrong with our bot (the real fix)

Reading the live code showed the loss path:

- The 3-state HMM on 30 daily candles reports ≥0.70 confidence almost always, so a
  trend triggered `trend_pause.flag` constantly.
- `grid_trader._run()` then **returned early — no orders replenished** → no income
  while "paused."
- The pause cancelled SELLs but kept BUYs → **BTC accumulated on dips with no
  take-profit**, and the fixed **3% trailing stop dumped the bag** on ordinary
  pullbacks → a buy-dip / sell-3%-lower whipsaw.
- The carefully-built `trending_up/dn` grid presets were **dead code**.

### Fixes shipped (see `docs/architecture.md` history and the commits)
- Confirmed-trend gating (HMM **and** rules agree, 2-read hysteresis) — stops the
  over-eager pause.
- Confirmed trends now **trade a wider, skewed, capital-reduced grid** instead of
  standing idle (the trend presets are live again).
- Trailing stop is **volatility-scaled** (`max(5%, 1.5×ATR%)`) — no more 3% whipsaw.
- Full stand-aside reserved for the explicit AI `STAND_ASIDE` stance.
- `risk_manager` CDaR step-down made proportional, and config set to **£170 / 20%
  active / 4 levels**, with the **weekly kill switch unchanged** (the pause rule
  John chose to keep).

### How it was proven
`src/backtesting/regime_grid_backtest.py` reproduces the *live* mechanics
(including the trailing-stop dump the old backtester ignored). Its `--selftest`
deterministically shows the current behaviour realising a loss that the fixed
behaviour avoids. Quantitative recent-weeks numbers are produced **on the Oracle
VM** with `--source binance` (the build sandbox blocks market-data hosts and lacks
`hmmlearn`).
