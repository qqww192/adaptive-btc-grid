"""
Daily reporter — runs at 08:00 UTC every day via crontab.

Reads yesterday's trades from the ledger, computes stats,
and sends a formatted Telegram message.
"""

import json
import os
import sys
from datetime import datetime, timezone, timedelta
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
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        print(f"[reporter] Telegram send failed: {e}")


INSTRUMENT = "BTC_USDT"


def format_report() -> str:
    yesterday = read_yesterday()
    risk      = get_risk_state()

    # When the local ledger has no entries for yesterday (e.g. fill detection failed),
    # fall back to the exchange API so the count is still correct.
    _ledger_fallback = False
    if not yesterday:
        try:
            from trading.cdx_client import CDXClient
            cdx     = CDXClient()
            now_utc = datetime.now(timezone.utc)
            ystart  = (now_utc - timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ).isoformat()
            yend    = now_utc.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            raw     = cdx.get_filled_orders_since(INSTRUMENT, ystart)
            yesterday = [
                {
                    "side":       o["side"],
                    "price_usdt": float(o.get("avg_price") or o["price"]),
                    "qty_btc":    float(o.get("cumulative_quantity") or o["qty"]),
                    "fee_usdt":   float(o.get("cumulative_fee") or 0),
                    "gross_gbp":  0.0,
                    "net_gbp":    0.0,
                    "ts":         o.get("ts", ""),
                }
                for o in raw
                if ystart <= o.get("ts", "")[:19] < yend
            ]
            _ledger_fallback = bool(yesterday)
        except Exception as e:
            print(f"[reporter] Live API fallback failed: {e}")

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
    stance      = ""
    sentiment   = {}
    if regime_file.exists():
        rd        = json.loads(regime_file.read_text())
        regime    = rd.get("regime", "unknown")
        stance    = rd.get("stance", "")
        sentiment = rd.get("sentiment") or {}

    config_file = ROOT / "config" / "grid_params.json"
    spacing     = "n/a"
    levels      = "n/a"
    if config_file.exists():
        cfg     = json.loads(config_file.read_text())
        spacing = f"{cfg.get('spacing_pct', '?')}%"
        levels  = str(cfg.get("levels", "?"))

    # Live portfolio + P&L snapshot (written every run by grid_trader).
    portfolio_file = ROOT / "data" / "portfolio.json"
    pf = {}
    if portfolio_file.exists():
        try:
            pf = json.loads(portfolio_file.read_text())
        except Exception:
            pf = {}
    # Fallback to the persisted capital figure if the snapshot is missing.
    if not pf and config_file.exists():
        pf = {"total_gbp": cfg.get("total_capital")}

    now = datetime.now(timezone.utc)
    date_str = (now.replace(hour=0) - __import__("datetime").timedelta(days=1)).strftime("%a %d %b %Y")

    lines = [
        f"📊 *BTCTradeBot — Daily Trade Report*",
        f"_{date_str} · BTC/USDT Grid_",
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━",
        f"📈 *Yesterday's trades*",
        f"• Orders filled: {len(yesterday)} ({len(buys)} buys · {len(sells)} sells)",
    ]

    if buys:
        lines.append(f"• Avg buy (yesterday): ${avg_buy:,.0f}")
    if sells:
        lines.append(f"• Avg sell (yesterday): ${avg_sell:,.0f}")

    lines += [
        f"• Gross P&L: {pnl_sign}£{gross:.2f}",
        f"• Fees paid: £{fees / float(os.environ.get('GBP_USD_RATE', '1.27')):.2f}",
        f"• Net P&L: *{pnl_sign}£{net:.2f}*",
    ]

    # ---- Portfolio (live whole-account value) ----
    lines += [f"", f"💼 *Portfolio*"]
    if pf.get("total_gbp") is not None:
        lines.append(f"• Total value: *£{pf['total_gbp']:.2f}*")
    if pf.get("usdt_total") is not None and pf.get("btc_value_gbp") is not None:
        lines.append(
            f"• Split: £{pf['usdt_total'] / float(os.environ.get('GBP_USD_RATE', '1.27')):.2f} cash "
            f"· £{pf['btc_value_gbp']:.2f} BTC"
        )
    if pf.get("btc_total"):
        lines.append(f"• BTC held: {pf['btc_total']:.6f} @ ${pf.get('btc_price_usdt', 0):,.0f}")
    if pf.get("avg_cost_btc"):
        lines.append(f"• Avg buy (held BTC): ${pf['avg_cost_btc']:,.0f}")

    # ---- Live P&L ----
    lines += [f"", f"📈 *Live P&L*"]
    unreal = pf.get("unrealised_gbp")
    if unreal is not None:
        u_sign = "+" if unreal >= 0 else ""
        u_pct  = ""
        if pf.get("btc_value_gbp"):
            base = pf["btc_value_gbp"] - unreal
            if base > 0:
                u_pct = f" ({u_sign}{unreal / base * 100:.1f}%)"
        lines.append(f"• Unrealised (held BTC): {u_sign}£{unreal:.2f}{u_pct}")
    lines.append(f"• Realised this week: {week_sign}£{risk['weekly_pnl_gbp']:.2f}")
    if pf.get("realised_alltime_gbp") is not None:
        a_sign = "+" if pf["realised_alltime_gbp"] >= 0 else ""
        lines.append(f"• Realised all-time: {a_sign}£{pf['realised_alltime_gbp']:.2f}")
    if pf.get("since_tracking_gbp") is not None:
        s_sign = "+" if pf["since_tracking_gbp"] >= 0 else ""
        lines.append(f"• Since tracking started: {s_sign}£{pf['since_tracking_gbp']:.2f}")

    lines += [
        f"",
        f"💰 *Week-to-date*",
        f"• Running P&L: *{week_sign}£{risk['weekly_pnl_gbp']:.2f}*",
        f"• Kill switch: {'🔴 ACTIVE' if risk['kill_switch_on'] else '🟢 Off'}",
        f"",
        f"⚙️ *Current grid*",
        f"• Regime: {regime}" + (f" · Stance: {stance}" if stance else ""),
        f"• Spacing: {spacing} · Levels: {levels}",
    ]

    # ---- Market sentiment ----
    if sentiment.get("fear_greed") is not None or sentiment.get("headlines"):
        lines += [f"", f"📰 *Market sentiment*"]
        if sentiment.get("fear_greed") is not None:
            lines.append(f"• Fear & Greed: {sentiment['fear_greed']} ({sentiment.get('fg_class', '')})")
        heads = sentiment.get("headlines") or []
        if heads:
            lines.append(f"• {heads[0]}")

    lines += [
        f"",
        f"━━━━━━━━━━━━━━━━━━━━━",
        f"_Next report: {now.strftime('%a')} 08:00 UTC_",
    ]

    if not yesterday:
        lines.insert(4, f"_No trades yesterday._")
        # Bot running but nothing filled — could be grid miscalibrated, spread too wide,
        # or POST_ONLY orders silently rejected. Flag it explicitly.
        lines.insert(5, f"⚠️ *Zero fills in 24h* — check grid positioning vs current BTC price")
    elif _ledger_fallback:
        lines.insert(4, f"⚠️ *Ledger gap* — counts from exchange API · P&L unavailable")

    # Heartbeat staleness check — warn if bot hasn't run in >25 minutes
    heartbeat_file = ROOT / "data" / "last_heartbeat.json"
    if heartbeat_file.exists():
        try:
            hb = json.loads(heartbeat_file.read_text())
            hb_ts = datetime.fromisoformat(hb["ts"])
            if datetime.now(timezone.utc) - hb_ts > timedelta(minutes=25):
                lines.insert(1, f"⚠️ *BOT MAY BE DOWN* — last heartbeat: {hb_ts.strftime('%H:%M UTC')} (>{25}min ago)")
        except Exception:
            pass
    else:
        lines.insert(1, "⚠️ *No heartbeat file — bot has never completed a run*")

    return "\n".join(lines)


def run() -> None:
    report = format_report()
    send(report)
    print("[reporter] Daily report sent.")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    run()
