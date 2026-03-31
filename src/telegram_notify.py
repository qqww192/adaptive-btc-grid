"""
telegram_notify.py
Send notifications via Telegram Bot API.

Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import json
import logging
import os
import urllib.request
import urllib.error

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Send a message via Telegram. Returns True on success."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        log.warning("Telegram not configured (missing BOT_TOKEN or CHAT_ID). Skipping.")
        return False

    url = API_URL.format(token=bot_token)
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text[:4090],
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }).encode("utf-8")

    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read())
            if body.get("ok"):
                log.info("Telegram message sent.")
                return True
            log.error("Telegram API error: %s", body)
            return False
    except urllib.error.URLError as e:
        log.error("Telegram unreachable: %s", e)
        return False


def notify_alert_triggered(alert: dict, current_value: float) -> None:
    """Send a Telegram notification for a triggered alert."""
    text = (
        f"<b>ALERT TRIGGERED</b>\n\n"
        f"<b>{alert['symbol']}</b> {alert['metric']}\n"
        f"Condition: {alert['condition']} {alert['threshold']}\n"
        f"Current: <b>{current_value:.2f}</b>\n\n"
        f"Type: {alert.get('type', 'recurring')}"
    )
    send_message(text)


def notify_high_risk(source: str, details: str) -> None:
    """Send a Telegram notification for high-risk detection."""
    text = (
        f"<b>HIGH RISK DETECTED</b>\n"
        f"Source: {source}\n\n"
        f"{details}"
    )
    send_message(text)


def notify_portfolio_risk(symbol: str, risk: str, verdict: str, key_note: str) -> None:
    """Send a Telegram notification when AI detects high risk on a stock."""
    text = (
        f"<b>PORTFOLIO RISK ALERT</b>\n\n"
        f"<b>{symbol}</b>\n"
        f"Verdict: {verdict}\n"
        f"Risk: {risk}/10\n"
        f"Note: {key_note}"
    )
    send_message(text)


def notify_scanner_matches(matches: list[dict], total: int) -> None:
    """Send a Telegram summary of top scanner matches."""
    if not matches:
        return
    lines = [f"<b>SCANNER: {total} match(es) found</b>\n"]
    for m in matches[:5]:
        sym = m.get("symbol", "?")
        name = m.get("name", "")
        price = m.get("price", "")
        pe = m.get("pe", "")
        score = m.get("score", "")
        line = f"<b>{sym}</b> {name[:25]}\n  Price: {price}"
        if pe:
            line += f" | P/E: {pe}"
        if score:
            line += f" | Score: {score}"
        lines.append(line)
    if total > 5:
        lines.append(f"\n<i>...and {total - 5} more. See Scanner Results tab.</i>")
    send_message("\n".join(lines))
