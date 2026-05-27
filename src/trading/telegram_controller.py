"""
Telegram interactive controller — Skill 3 (aiogram v3).

Turns the one-way notification system into a two-way control panel.
Runs as a persistent async process (separate from the 1-min cron job).

Commands
--------
  /status   — show live P&L, regime, kill switch state, last heartbeat
  /params   — show current grid_params.json
  /pause    — write data/paused.flag (grid_trader.py checks this)
  /resume   — remove data/paused.flag
  /recenter — write data/force_recenter.flag (grid_trader picks it up)
  /kills    — show kill switch state + this week's trades
  /help     — list all commands

Pause mechanism
---------------
grid_trader.py checks for data/paused.flag at the start of each run.
This controller creates or removes that file. The flag is never committed
to git — it only exists on the Oracle VM while the bot is paused.

Running
-------
  # On Oracle VM (keep alive with nohup or systemd):
  python3 src/trading/telegram_controller.py

  # Or run as a cron job that keeps the bot alive:
  @reboot cd {BOT_DIR} && set -a && source .env && set +a && nohup {PYTHON} src/trading/telegram_controller.py >> {LOG_DIR}/controller.log 2>&1 &
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

# Paths
CONFIG_FILE      = ROOT / "config" / "grid_params.json"
REGIME_FILE      = ROOT / "data"   / "regime.json"
STATE_FILE       = ROOT / "data"   / "weekly_state.json"
HEARTBEAT_FILE   = ROOT / "data"   / "last_heartbeat.json"
PORTFOLIO_FILE   = ROOT / "data"   / "portfolio.json"
PAUSE_FLAG       = ROOT / "data"   / "paused.flag"
RECENTER_FLAG    = ROOT / "data"   / "force_recenter.flag"
TREND_PAUSE_FLAG = ROOT / "data"   / "trend_pause.flag"   # set by regime_classifier
CANDIDATES_FILE  = ROOT / "data"  / "optuna_candidates.json"


# ------------------------------------------------------------------ #
#  Data helpers                                                        #
# ------------------------------------------------------------------ #

def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def _heartbeat_age() -> str:
    hb = _read_json(HEARTBEAT_FILE)
    if not hb.get("ts"):
        return "never"
    try:
        ts  = datetime.fromisoformat(hb["ts"])
        age = datetime.now(timezone.utc) - ts
        mins = int(age.total_seconds() / 60)
        return f"{mins}m ago" if mins < 120 else f"{int(mins/60)}h ago"
    except Exception:
        return "unknown"


def _build_status() -> str:
    state  = _read_json(STATE_FILE)
    regime = _read_json(REGIME_FILE)
    hb     = _read_json(HEARTBEAT_FILE)
    cfg    = _read_json(CONFIG_FILE)
    pf     = _read_json(PORTFOLIO_FILE)
    sentiment = regime.get("sentiment") or {}
    paused       = PAUSE_FLAG.exists()
    trend_paused = TREND_PAUSE_FLAG.exists()

    kill   = state.get("kill_switch_on", False)
    pnl    = state.get("weekly_pnl_gbp", 0.0)
    trades = state.get("trades_this_week", 0)
    price  = pf.get("btc_price_usdt") or hb.get("price", 0)
    # Live whole-portfolio value (snapshot) preferred over the cached config figure.
    cap    = pf.get("total_gbp") if pf.get("total_gbp") is not None else cfg.get("total_capital", 0)

    if paused:
        status_icon = "⏸ PAUSED (manual)"
    elif trend_paused:
        status_icon = "📉 TREND PAUSE (auto — strong trend detected)"
    elif kill:
        status_icon = "🛑 KILL SWITCH ON"
    else:
        status_icon = "🟢 Running"

    gbp_usd = cfg.get("gbp_usd_rate", 1.27) or 1.27
    unreal  = pf.get("unrealised_gbp")
    alltime = pf.get("realised_alltime_gbp")
    since   = pf.get("since_tracking_gbp")

    hmm_conf      = float(regime.get("hmm_confidence", 0.0))
    regime_str    = regime.get("regime", "")
    resume_hint   = None
    if trend_paused:
        if regime_str in ("trending_up", "trending_dn"):
            resume_hint = f"▶️ Resumes when HMM conf drops below 70% (now {hmm_conf:.0%}) or regime → ranging"
        else:
            resume_hint = "▶️ Resumes at next regime reclassification (4-hourly)"

    lines = [
        f"📊 *Grid Bot Status*",
        f"",
        f"{status_icon}",
        resume_hint,
        f"",
        f"💼 Portfolio: *£{cap:.2f}*" if cap else "",
        (f"   £{pf['usdt_total'] / gbp_usd:.2f} cash · £{pf.get('btc_value_gbp', 0):.2f} BTC"
         if pf.get("usdt_total") is not None else ""),
        f"   Avg buy (held BTC): ${pf['avg_cost_btc']:,.0f}" if pf.get("avg_cost_btc") else "",
        f"₿ BTC price: ${price:,.0f}" if price else "",
        f"",
        f"📈 Unrealised (held BTC): {'%+.2f' % unreal} GBP" if unreal is not None else "",
        f"💰 Realised week: *{'%+.2f' % pnl} GBP*",
        f"📊 Realised all-time: {'%+.2f' % alltime} GBP" if alltime is not None else "",
        f"📦 Since tracking: {'%+.2f' % since} GBP" if since is not None else "",
        f"🔁 Trades this week: {trades}",
        f"",
        f"⚙️ Grid config:",
        f"  Regime: {regime.get('regime', 'unknown').replace('_', '-')} "
        f"(HMM conf={regime.get('hmm_confidence', 0):.2f})",
        f"  Stance: {regime.get('stance', 'NEUTRAL')}",
        f"  Spacing: {cfg.get('spacing_pct', '?')}% · "
        f"Levels: {cfg.get('levels', '?')} · "
        f"Capital: {int(cfg.get('capital_pct', 0)*100)}%",
        (f"  Fear & Greed: {sentiment['fear_greed']} ({sentiment.get('fg_class', '')})"
         if sentiment.get("fear_greed") is not None else ""),
        f"",
        f"🕐 Last heartbeat: {_heartbeat_age()}",
    ]
    return "\n".join(l for l in lines if l is not None)


# ------------------------------------------------------------------ #
#  Bot setup                                                           #
# ------------------------------------------------------------------ #

def create_bot():
    """Create and configure the aiogram bot. Returns (bot, dp) or raises."""
    from aiogram import Bot, Dispatcher
    from aiogram.enums import ParseMode
    from aiogram.client.default import DefaultBotProperties

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN not set")

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    dp  = Dispatcher()
    return bot, dp


def _register_handlers(dp, allowed_chat_id: str):
    """Register all command handlers on the dispatcher."""
    from aiogram import Router
    from aiogram.filters import Command
    from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
    from aiogram import F

    router = Router()

    def _allowed(msg: Message) -> bool:
        """Reject messages from anyone except the configured chat_id."""
        return str(msg.chat.id) == str(allowed_chat_id)

    def _main_keyboard() -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⏸ Pause", callback_data="pause"),
                InlineKeyboardButton(text="▶️ Resume", callback_data="resume"),
            ],
            [
                InlineKeyboardButton(text="🔄 Force Recentre", callback_data="recenter"),
                InlineKeyboardButton(text="📊 Status", callback_data="status"),
            ],
        ])

    @router.message(Command("start", "help"))
    async def cmd_help(msg: Message):
        if not _allowed(msg):
            return
        await msg.answer(
            "🤖 *BTCTradeBot Controller*\n\n"
            "/status      — live P\\&L, regime, heartbeat\n"
            "/params      — current grid parameters\n"
            "/pause       — pause the grid bot manually\n"
            "/resume      — resume manual pause\n"
            "/trendresume — override trend pause (force grid on)\n"
            "/recenter    — force grid recentre on next run\n"
            "/kills       — kill switch status\n"
            "/optuna      — last Optuna candidates",
            reply_markup=_main_keyboard(),
        )

    @router.message(Command("status"))
    async def cmd_status(msg: Message):
        if not _allowed(msg):
            return
        await msg.answer(_build_status(), reply_markup=_main_keyboard())

    @router.message(Command("params"))
    async def cmd_params(msg: Message):
        if not _allowed(msg):
            return
        cfg = _read_json(CONFIG_FILE)
        if cfg:
            text = "⚙️ *Current grid params:*\n```\n" + json.dumps(cfg, indent=2) + "\n```"
        else:
            text = "⚙️ No config file found."
        await msg.answer(text)

    @router.message(Command("pause"))
    async def cmd_pause(msg: Message):
        if not _allowed(msg):
            return
        PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        PAUSE_FLAG.write_text(f"Paused by Telegram at {datetime.now(timezone.utc).isoformat()}")
        await msg.answer(
            "⏸ *Bot paused.*\n"
            "The grid trader will skip its next run and all subsequent runs "
            "until you send /resume.\n\n"
            "_Existing open orders remain on the exchange._"
        )

    @router.message(Command("resume"))
    async def cmd_resume(msg: Message):
        if not _allowed(msg):
            return
        if PAUSE_FLAG.exists():
            PAUSE_FLAG.unlink()
            await msg.answer("▶️ *Bot resumed.* It will trade on the next 1-minute cron tick.")
        else:
            await msg.answer("ℹ️ Bot was not paused.")

    @router.message(Command("recenter"))
    async def cmd_recenter(msg: Message):
        if not _allowed(msg):
            return
        RECENTER_FLAG.parent.mkdir(parents=True, exist_ok=True)
        RECENTER_FLAG.write_text(datetime.now(timezone.utc).isoformat())
        await msg.answer(
            "🔄 *Force recentre scheduled.*\n"
            "The grid will cancel all orders and rebuild around the current "
            "BTC price on the next 1-minute run."
        )

    @router.message(Command("kills"))
    async def cmd_kills(msg: Message):
        if not _allowed(msg):
            return
        state    = _read_json(STATE_FILE)
        kill     = state.get("kill_switch_on", False)
        pnl      = state.get("weekly_pnl_gbp", 0.0)
        week_start = state.get("week_start", "unknown")
        trigger  = state.get("kill_trigger_at", "not triggered")
        text = (
            f"🔴 *Kill switch: {'ACTIVE' if kill else 'Off'}*\n"
            f"Week P&L: £{pnl:.2f}\n"
            f"Week start: {week_start[:10]}\n"
            f"Triggered at: {trigger}"
        )
        await msg.answer(text)

    @router.message(Command("trendresume"))
    async def cmd_trendresume(msg: Message):
        if not _allowed(msg):
            return
        if TREND_PAUSE_FLAG.exists():
            TREND_PAUSE_FLAG.unlink()
            await msg.answer(
                "▶️ *Trend pause overridden.*\n"
                "Grid will resume on the next 1-minute cron tick.\n"
                "_Note: the regime classifier may reinstate the pause in ≤4 hours "
                "if the trend persists._"
            )
        else:
            await msg.answer("ℹ️ No trend pause was active.")

    @router.message(Command("optuna"))
    async def cmd_optuna(msg: Message):
        if not _allowed(msg):
            return
        data = _read_json(CANDIDATES_FILE)
        if not data:
            await msg.answer("ℹ️ No Optuna candidates yet (runs Saturday 22:30 UTC).")
            return
        candidates = data.get("candidates", [])
        lines = [f"🔬 *Optuna candidates* ({data.get('regime', '?')} regime):"]
        for i, c in enumerate(candidates[:3], 1):
            lines.append(
                f"#{i}: spacing={c.get('spacing_pct')}% "
                f"levels={c.get('levels')} "
                f"capital={c.get('capital_pct')} "
                f"→ est. {c.get('estimated_return_pct')}%"
            )
        await msg.answer("\n".join(lines))

    # Inline keyboard callbacks
    from aiogram.types import CallbackQuery

    @router.callback_query(F.data == "status")
    async def cb_status(cb: CallbackQuery):
        if not _allowed(cb.message):
            return
        await cb.message.edit_text(_build_status(), reply_markup=_main_keyboard())
        await cb.answer()

    @router.callback_query(F.data == "pause")
    async def cb_pause(cb: CallbackQuery):
        if not _allowed(cb.message):
            return
        PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        PAUSE_FLAG.write_text(f"Paused at {datetime.now(timezone.utc).isoformat()}")
        await cb.message.edit_text("⏸ *Bot paused.* Send /resume to restart.")
        await cb.answer("Paused")

    @router.callback_query(F.data == "resume")
    async def cb_resume(cb: CallbackQuery):
        if not _allowed(cb.message):
            return
        if PAUSE_FLAG.exists():
            PAUSE_FLAG.unlink()
        await cb.message.edit_text("▶️ *Bot resumed.*", reply_markup=_main_keyboard())
        await cb.answer("Resumed")

    @router.callback_query(F.data == "recenter")
    async def cb_recenter(cb: CallbackQuery):
        if not _allowed(cb.message):
            return
        RECENTER_FLAG.parent.mkdir(parents=True, exist_ok=True)
        RECENTER_FLAG.write_text(datetime.now(timezone.utc).isoformat())
        await cb.message.edit_text("🔄 *Force recentre scheduled for next run.*")
        await cb.answer("Recentre scheduled")

    dp.include_router(router)


# ------------------------------------------------------------------ #
#  Main                                                                #
# ------------------------------------------------------------------ #

async def main() -> None:
    from aiogram import Bot
    bot, dp = create_bot()
    allowed_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    _register_handlers(dp, allowed_chat_id)
    print(f"[controller] Telegram controller started. Listening for commands...")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    asyncio.run(main())
