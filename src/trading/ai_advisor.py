"""
AI advisor — lightweight Groq/Cerebras calls for four high-value decision points.

1. Smart recenter  — "Is this price move a real trend or a temporary spike?"
2. Regime override — "What regime is this when HMM confidence is low?"
3. Monday briefing — "What should we watch for this week?" (Telegram only, no param changes)
4. Kill switch guardian — "Should we reduce capital deployment or hold?"

Design principles:
  - Every call has a 10s timeout so it cannot block the 90s cron window.
  - Responses are cached in data/ai_cache.json for 20 minutes — one AI call
    per price event, not one per 2-minute run.
  - Every function has a safe fallback: if AI is unavailable, the bot uses
    its existing mechanical logic unchanged.
  - Prompts are kept under 300 tokens. Responses are capped at 15–80 tokens.
"""

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Optional

import httpx

ROOT       = Path(__file__).resolve().parents[2]
CACHE_FILE = ROOT / "data" / "ai_cache.json"

GROQ_URL      = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL    = "qwen/qwen3-32b"
CEREBRAS_URL  = "https://api.cerebras.ai/v1/chat/completions"
CEREBRAS_MODEL = "gpt-oss-120b"

AI_TIMEOUT = 10       # seconds — hard ceiling to protect cron window
CACHE_TTL  = 1200     # 20 minutes — one decision per price event


# ------------------------------------------------------------------ #
#  Internal helpers                                                    #
# ------------------------------------------------------------------ #

def _call_ai(prompt: str, max_tokens: int = 15) -> Optional[str]:
    """Call Groq then Cerebras. Return response text or None on failure."""
    providers = [
        {"url": GROQ_URL,     "model": GROQ_MODEL,     "key": os.environ.get("GROQ_API_KEY", "")},
        {"url": CEREBRAS_URL, "model": CEREBRAS_MODEL, "key": os.environ.get("CEREBRAS_API_KEY", "")},
    ]
    for p in providers:
        if not p["key"]:
            continue
        try:
            r = httpx.post(
                p["url"],
                headers={"Authorization": f"Bearer {p['key']}"},
                json={
                    "model":       p["model"],
                    "messages":    [{"role": "user", "content": prompt}],
                    "max_tokens":  max_tokens,
                    "temperature": 0.1,
                },
                timeout=AI_TIMEOUT,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"[ai] {p['model']} failed: {e}")
    return None


def _cache_key(tag: str, *values) -> str:
    payload = tag + ":" + ":".join(str(v) for v in values)
    return hashlib.md5(payload.encode()).hexdigest()


def _cache_get(key: str) -> Optional[str]:
    try:
        if not CACHE_FILE.exists():
            return None
        cache = json.loads(CACHE_FILE.read_text())
        entry = cache.get(key, {})
        if entry and time.time() - entry.get("ts", 0) < CACHE_TTL:
            return entry["value"]
    except Exception:
        pass
    return None


def _cache_set(key: str, value: str) -> None:
    try:
        cache = json.loads(CACHE_FILE.read_text()) if CACHE_FILE.exists() else {}
        cache[key] = {"value": value, "ts": time.time()}
        cache = {k: v for k, v in cache.items()
                 if time.time() - v.get("ts", 0) < CACHE_TTL * 2}
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(cache))
    except Exception:
        pass


# ------------------------------------------------------------------ #
#  1. Smart recenter decision                                          #
# ------------------------------------------------------------------ #

def ask_recenter(
    current_price:     float,
    calibration_price: float,
    recent_candles:    list[dict],
    regime:            str,
) -> bool:
    """
    Ask AI whether to recenter the grid or wait for a price reversion.
    Returns True = recenter now, False = hold the current grid.
    Falls back to True (immediate recenter) if AI is unavailable.
    """
    move_pct = (current_price - calibration_price) / calibration_price * 100

    closes = [c["close"] for c in recent_candles[-6:]] if len(recent_candles) >= 6 else []
    price_trail = " → ".join(f"${p:,.0f}" for p in closes) if closes else f"${current_price:,.0f}"

    key = _cache_key("recenter", f"{current_price:.0f}", f"{calibration_price:.0f}")
    cached = _cache_get(key)
    if cached is not None:
        decision = cached == "RECENTER"
        print(f"[ai] Recenter (cached): {cached}")
        return decision

    prompt = (
        f"BTC grid bot. Grid calibrated at ${calibration_price:,.0f}. "
        f"Price now ${current_price:,.0f} ({move_pct:+.1f}%). "
        f"Recent closes: {price_trail}. Regime: {regime}. "
        f"Is this a real trend shift requiring grid recentering, or a temporary spike likely to revert? "
        f"Reply with one word only: RECENTER or HOLD."
    )

    response = _call_ai(prompt, max_tokens=5)
    if response is None:
        print("[ai] Recenter fallback → RECENTER (AI unavailable)")
        return True

    decision = "recenter" in response.lower()
    result   = "RECENTER" if decision else "HOLD"
    print(f"[ai] Recenter: {result}  (raw: '{response}')")
    _cache_set(key, result)
    return decision


# ------------------------------------------------------------------ #
#  2. Regime override when HMM is uncertain                            #
# ------------------------------------------------------------------ #

def ask_regime(
    recent_candles: list[dict],
    hmm_regime:     str,
    hmm_confidence: float,
) -> str:
    """
    When HMM confidence is below threshold, ask AI to classify the regime.
    Returns 'ranging', 'trending', or 'volatile'.
    Falls back to hmm_regime if AI is unavailable.
    """
    if len(recent_candles) < 4:
        return hmm_regime

    closes = [c["close"] for c in recent_candles[-8:]]
    highs  = [c["high"]  for c in recent_candles[-8:]]
    lows   = [c["low"]   for c in recent_candles[-8:]]
    range_pct    = (max(highs) - min(lows)) / closes[0] * 100
    net_move_pct = (closes[-1] - closes[0])  / closes[0] * 100

    key    = _cache_key("regime", f"{closes[-1]:.0f}", f"{closes[0]:.0f}")
    cached = _cache_get(key)
    if cached is not None:
        print(f"[ai] Regime (cached): {cached}")
        return cached

    prompt = (
        f"BTC/USDT last 8 candles: range={range_pct:.1f}%, net move={net_move_pct:+.1f}%. "
        f"HMM says '{hmm_regime}' but confidence is only {hmm_confidence:.0%}. "
        f"Classify the market regime. Reply with one word: RANGING, TRENDING, or VOLATILE."
    )

    response = _call_ai(prompt, max_tokens=5)
    if response is None:
        print(f"[ai] Regime fallback → {hmm_regime} (AI unavailable)")
        return hmm_regime

    r = response.lower()
    if   "trend"  in r: regime = "trending"
    elif "volat"  in r: regime = "volatile"
    else:               regime = "ranging"

    if regime != hmm_regime:
        print(f"[ai] Regime override: {hmm_regime} → {regime}  "
              f"(HMM conf={hmm_confidence:.0%}, raw: '{response}')")
    else:
        print(f"[ai] Regime confirmed: {regime}  (HMM conf={hmm_confidence:.0%})")

    _cache_set(key, regime)
    return regime


# ------------------------------------------------------------------ #
#  3. Monday morning briefing (Telegram only — no param changes)       #
# ------------------------------------------------------------------ #

def send_monday_briefing(
    last_week_pnl:    float,
    last_week_trades: int,
    current_regime:   str,
    config:           dict,
) -> None:
    """
    On Monday weekly reset, ask AI for a brief outlook and send via Telegram.
    Does NOT change any parameters — observation only.
    """
    prompt = (
        f"BTC/USDT grid trading weekly briefing. "
        f"Last week: net P&L £{last_week_pnl:.2f}, {last_week_trades} trades. "
        f"Current regime: {current_regime}. "
        f"Grid: {config.get('levels', 6)} levels, {config.get('spacing_pct', 0.8)}% spacing. "
        f"In 2 sentences: what should a conservative BTC grid trader watch for this week, "
        f"and should spacing be tightened or widened?"
    )

    response = _call_ai(prompt, max_tokens=80)
    if response is None:
        return

    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print(f"[ai] Monday briefing: {response}")
        return

    message = (
        f"📅 *Monday AI Briefing*\n\n"
        f"Last week: £{last_week_pnl:+.2f} | {last_week_trades} trades\n"
        f"Regime: {current_regime}\n\n"
        f"{response}"
    )
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=5,
        )
        print("[ai] Monday briefing sent to Telegram.")
    except Exception as e:
        print(f"[ai] Monday briefing Telegram failed: {e}")


# ------------------------------------------------------------------ #
#  4. Kill switch guardian                                             #
# ------------------------------------------------------------------ #

def ask_kill_switch_guardian(
    weekly_pnl:          float,
    kill_threshold:      float,
    recent_net_returns:  list[float],
) -> str:
    """
    When the −5% warning fires, ask AI: reduce capital deployment or hold?
    Returns 'reduce' or 'hold'.
    Falls back to 'hold' — CDaR in risk_manager already handles step-downs.
    """
    if not recent_net_returns:
        return "hold"

    wins     = [r for r in recent_net_returns if r > 0]
    losses   = [r for r in recent_net_returns if r < 0]
    win_rate = len(wins) / len(recent_net_returns) * 100
    avg_loss = sum(losses) / len(losses) if losses else 0

    key    = _cache_key("guardian", f"{weekly_pnl:.2f}")
    cached = _cache_get(key)
    if cached is not None:
        print(f"[ai] Kill switch guardian (cached): {cached}")
        return cached

    prompt = (
        f"BTC grid bot risk check. "
        f"Weekly P&L: £{weekly_pnl:.2f} (kill switch at £{kill_threshold:.2f}). "
        f"Last {len(recent_net_returns)} trades: win rate {win_rate:.0f}%, avg loss £{avg_loss:.3f}. "
        f"Should the bot reduce capital deployment to protect remaining capital, "
        f"or hold as the loss may be temporary? "
        f"Reply with one word: REDUCE or HOLD."
    )

    response = _call_ai(prompt, max_tokens=5)
    if response is None:
        print("[ai] Kill switch guardian fallback → HOLD")
        return "hold"

    decision = "reduce" if "reduce" in response.lower() else "hold"
    print(f"[ai] Kill switch guardian: {decision.upper()}  (raw: '{response}')")
    _cache_set(key, decision)
    return decision
