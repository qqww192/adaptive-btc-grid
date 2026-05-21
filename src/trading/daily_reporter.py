"""
Daily reporter — runs at 08:00 UTC every day via crontab.

Reads yesterday's trades from the ledger, computes stats,
and sends a formatted Telegram message.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import httpx
from trading.trade_logger import read_yesterday, read_this_week
from trading.risk_manager  import get_state as get_risk_state


def send(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print(f"[reporter] Telegram not configured — message: {message}")
        return
    httpx.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
        timeout=10,
    )


def format_report() -> str:
    yesterday = read_yesterday()
    risk      = get_risk_state()

    # ---- Yesterday stats ----
    buys   = [t for t in yesterday if t["side"] == "BUY"]
    sells  = [t for t in yesterday if t["side"] == "SELL"]
    gross  = sum(t["gross_gbp"] for t in yesterday)
    fees   = sum(t["fee_usdt"]  for t in yesterday)
    net    = sum(t["net_gbp"]   for t in yesterday)

    avg_buy  = (sum(t["price_usdt"] for t in buys)  / len(buys))  if buys  else 0
    avg_sell = (sum(t["price_usdt"] for t in sells) / len(sells)) if sells else 0

    pnl_sign  = "+" if net >= 0 else ""
    week_sign = "+" if risk["weekly_pnl_gbp"] >= 0 else ""

    # Load regime
    regime_file = ROOT / "data" / "regime.json"
    regime      = "unknown"
    grid_range  = "n/a"
    if regime_file.exists():
        rd     = json.loads(regime_file.read_text())
        regime = rd.get("regime", "unknown")

    config_file = ROOT / "config" / "grid_params.json"
    spacing     = "n/a"
    levels      = "n/a"
    if config_file.exists():
        cfg     = json.loads(config_file.read_text())
        spacing = f"{cfg.get('spacing_pct', '?')}%"
        levels  = str(cfg.get("levels", "?"))

    now = datetime.now(timezone.utc)
    date_str = (now.replace(hour=0) - __import__("datetime").timedelta(days=1)).strftime("%a %d %b %Y")

    lines = [
        f"📊 *FinancialAdvisor — Daily Trade Report*",
        f"_{date_str} · BTC/USDT Grid_",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━",
        f"📈 *Yesterday's trades*",
        f"• Orders filled: {len(yesterday)} ({len(buys)} buys · {len(sells)} sells)",
    ]

    if buys:
        lines.append(f"• Avg buy price: ${avg_buy:,.0f}")
    if sells:
        lines.append(f"• Avg sell price: ${avg_sell:,.0f}")

    lines += [
        f"• Gross P&L: {pnl_sign}£{gross:.2f}",
        f"• Fees paid: £{fees / float(os.environ.get('GBP_USD_RATE', '1.27')):.2f}",
        f"• Net P&L: *{pnl_sign}£{net:.2f}*",
        f"",
        f"💰 *Week-to-date*",
        f"• Running P&L: *{week_sign}£{risk['weekly_pnl_gbp']:.2f}*",
        f"• Kill switch: {'🔴 ACTIVE' if risk['kill_switch_on'] else '🟢 Off'}",
        f"",
        f"⚙️ *Current grid*",
        f"• Regime: {regime}",
        f"• Spacing: {spacing} · Levels: {levels}",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━",
        f"_Next report: {now.strftime('%a')} 08:00 UTC_",
    ]

    if not yesterday:
        lines.insert(4, f"_No trades yesterday._")

    return "\n".join(lines)


def run() -> None:
    report = format_report()
    send(report)
    print("[reporter] Daily report sent.")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    run()
