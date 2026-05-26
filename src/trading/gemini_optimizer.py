"""
AI weekly optimiser — runs Sunday 23:00 UTC via crontab.
Uses Groq (Qwen3-32B) as primary AI provider, Cerebras (gpt-oss-120b) as fallback.
Both are free tiers, no credit card required, UK-accessible.

Steps
-----
1. Reads the last 7 days of trade data.
2. Computes performance metrics (win rate, Sharpe, fee drag, etc.).
3. Sends metrics + regime history to Gemini AI.
4. Gemini returns proposed new grid parameters as JSON.
5. Walk-forward simulation: applies proposed params to last 30 days
   of price data and checks if net return improves on current params.
6. If confirmed: writes new params to config/grid_params.json.
7. Sends Telegram summary regardless of outcome.

Overfitting guard
-----------------
Gemini is explicitly instructed not to chase last week's noise.
Walk-forward validation on 30 days (not 7) is required before any
param change is accepted.
"""

import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from trading.trade_logger  import read_all, read_since
from trading.cdx_client    import CDXClient, CDXError


def _atomic_write(path: Path, content: str) -> None:
    """Write content atomically: temp file in same dir then os.replace()."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

CONFIG_FILE      = ROOT / "config" / "grid_params.json"
REGIME_FILE      = ROOT / "data"   / "regime.json"
CANDIDATES_FILE  = ROOT / "data"   / "optuna_candidates.json"  # Skill 5

# AI providers — tried in order until one succeeds
_PROVIDERS = [
    {
        "name":    "Groq",
        "url":     "https://api.groq.com/openai/v1/chat/completions",
        "model":   "qwen/qwen3-32b",
        "env_key": "GROQ_API_KEY",
    },
    {
        "name":    "Cerebras",
        "url":     "https://api.cerebras.ai/v1/chat/completions",
        "model":   "gpt-oss-120b",
        "env_key": "CEREBRAS_API_KEY",
    },
]


# ------------------------------------------------------------------ #
#  Performance metrics                                                 #
# ------------------------------------------------------------------ #

SAFE_BOUNDS = {
    "spacing_pct": (0.55, 3.0),
    "range_pct":   (2.0,  15.0),
    "levels":      (4,    20),
    "capital_pct": (0.40, 0.80),
    "kill_pct":    (0.05, 0.15),
}


def validate_params(proposed: dict) -> list[str]:
    """Return a list of violation strings; empty list means params are safe."""
    violations = []
    for key, (lo, hi) in SAFE_BOUNDS.items():
        val = proposed.get(key)
        if val is None:
            violations.append(f"{key} missing")
        elif not (lo <= val <= hi):
            violations.append(f"{key}={val} outside [{lo}, {hi}]")
    return violations


def compute_metrics(trades: list[dict]) -> dict:
    sells = [t for t in trades if t["side"] == "SELL"]
    if not sells:
        return {
            "trades_total": len(trades), "sells": 0, "win_rate_pct": 0,
            "avg_net_gbp": 0, "total_net_gbp": 0, "fee_drag_pct": 0,
            "sharpe": 0, "max_loss_gbp": 0, "avg_win_gbp": 0,
            "note": "no_sells_this_week",
        }

    # Include BUY-side fees (recorded as negative net_gbp) so P&L reflects true round-trip cost.
    buy_fees_gbp = sum(abs(t["net_gbp"]) for t in trades if t["side"] == "BUY")
    nets     = [t["net_gbp"] for t in sells]
    wins     = [n for n in nets if n > 0]
    losses   = [n for n in nets if n <= 0]
    gross    = sum(t["gross_gbp"] for t in trades)
    fees_gbp = sum(t["fee_usdt"]  for t in trades) / float(os.environ.get("GBP_USD_RATE", "1.27"))
    net_tot  = sum(nets) - buy_fees_gbp
    fee_drag = (fees_gbp / gross * 100) if gross else 0

    mean_ret = net_tot / len(nets) if nets else 0
    std_ret  = math.sqrt(sum((n - mean_ret) ** 2 for n in nets) / len(nets)) if len(nets) > 1 else 0
    sharpe   = mean_ret / std_ret if std_ret else 0

    return {
        "trades_total":   len(trades),
        "sells":          len(sells),
        "win_rate_pct":   round(len(wins) / len(sells) * 100, 1) if sells else 0,
        "avg_net_gbp":    round(mean_ret, 4),
        "total_net_gbp":  round(net_tot, 2),
        "fee_drag_pct":   round(fee_drag, 1),
        "sharpe":         round(sharpe, 2),
        "max_loss_gbp":   round(min(losses, default=0), 4),
        "avg_win_gbp":    round(sum(wins) / len(wins), 4) if wins else 0,
    }


# ------------------------------------------------------------------ #
#  Gemini call                                                         #
# ------------------------------------------------------------------ #

def _load_optuna_candidates() -> list[dict]:
    """Load Optuna top-3 candidates produced by optuna_optimizer.py (Skill 5)."""
    if not CANDIDATES_FILE.exists():
        return []
    try:
        data       = json.loads(CANDIDATES_FILE.read_text())
        candidates = data.get("candidates", [])
        generated  = data.get("generated_at", "")
        if generated:
            age_hours = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(generated)
            ).total_seconds() / 3600
            if age_hours > 36:
                print(f"[optimiser] Optuna candidates are {age_hours:.0f}h old — ignoring stale data")
                return []
        print(f"[optimiser] Loaded {len(candidates)} Optuna candidates from Saturday sweep")
        return candidates
    except Exception:
        return []


def _build_prompt(
    metrics_7d: dict,
    current_config: dict,
    regime: str,
    btc_price_usdt: float,
    candidates: list | None = None,
    agent_role: str = "conservative crypto trading bot optimiser",
    agent_stance: str = "",
) -> str:
    total_capital = current_config.get("total_capital", 150)
    gbp_usd       = current_config.get("gbp_usd_rate", 1.27)
    max_min_cap   = total_capital * 0.85

    btc_price_note = (
        f"Current BTC price ≈ ${btc_price_usdt:,.0f} USDT.\n"
        f"Minimum capital formula: 0.0001 × BTC_price × levels / capital_pct / gbp_usd_rate\n"
        f"Proposed params must keep this below £{max_min_cap:.0f} (total capital £{total_capital}).\n"
        if btc_price_usdt > 0 else ""
    )

    # Inject Optuna candidates when available (Skill 5)
    candidates_text = ""
    if candidates:
        candidates_text = (
            "\n\nOptuna pre-validated top candidates (Bayesian sweep, 30-day data):\n"
            + json.dumps(candidates, indent=2)
            + "\n\nPREFER to select from these — only deviate if there is a strong analytical reason.\n"
        )

    stance_text = f"\nYour analytical stance: {agent_stance}\n" if agent_stance else ""

    # Market sentiment + AI stance (written 4-hourly by regime_classifier).
    sentiment_text = ""
    try:
        if REGIME_FILE.exists():
            rd        = json.loads(REGIME_FILE.read_text())
            sentiment = rd.get("sentiment") or {}
            news_stance = rd.get("stance", "")
            bits = []
            if sentiment.get("fear_greed") is not None:
                bits.append(f"Fear&Greed {sentiment['fear_greed']} ({sentiment.get('fg_class','')})")
            heads = sentiment.get("headlines") or []
            if heads:
                bits.append("Top headlines: " + " | ".join(heads[:3]))
            if news_stance:
                bits.append(f"Current AI stance: {news_stance}")
            if bits:
                sentiment_text = "\nMarket sentiment & news:\n- " + "\n- ".join(bits) + "\n"
    except Exception:
        sentiment_text = ""

    return f"""You are a {agent_role}.
The bot runs a spot BTC/USDT grid strategy on crypto.com Exchange.
Total capital: £{total_capital}. Weekly kill switch: 10%.
{stance_text}
{btc_price_note}
Current parameters:
{json.dumps(current_config, indent=2)}

Market regime: {regime}
{sentiment_text}

Last 7 days metrics:
{json.dumps(metrics_7d, indent=2)}
{candidates_text}
Propose grid parameters for next week. Rules:
- Prioritise capital preservation over returns.
- Do NOT chase last week — optimise for 30-day robustness.
- Keep changes minimal if win_rate > 60% and fee_drag < 30%.
- Tighten kill_pct if regime is trending_dn.
- spacing_pct > 0.55 (must beat 0.25% maker fee × 2).
- levels: 4–20. capital_pct: 0.50–0.80. range_pct: 3.0–10.0.

Return ONLY valid JSON, no markdown, no extra text:
{{"instrument":"BTC_USDT","spacing_pct":<float>,"range_pct":<float>,"levels":<int>,"capital_pct":<float>,"total_capital":{total_capital},"gbp_usd_rate":{gbp_usd},"kill_pct":<float>,"rationale":"<one sentence>"}}"""


def _raw_ai_call(prompt: str) -> str | None:
    """Try each provider in order; return raw response text on first success."""
    for provider in _PROVIDERS:
        api_key = os.environ.get(provider["env_key"], "")
        if not api_key:
            continue
        try:
            resp = httpx.post(
                provider["url"],
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model":           provider["model"],
                    "messages":        [{"role": "user", "content": prompt}],
                    "response_format": {"type": "json_object"},
                    "temperature":     0.3,
                },
                timeout=30,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()
            return text.replace("```json", "").replace("```", "").strip()
        except Exception as exc:
            print(f"[optimiser] {provider['name']} failed: {exc}")
    return None


def _multi_agent_debate(
    metrics_7d:     dict,
    current_config: dict,
    regime:         str,
    btc_price_usdt: float,
    candidates:     list,
) -> dict | None:
    """
    Skill 10: Run three specialist agents then synthesise consensus.

    Agents:
      1. Bullish analyst  — maximise fill frequency + return
      2. Bearish analyst  — minimise drawdown + fee drag
      3. Risk manager     — balance return vs kill-switch distance

    The synthesiser picks the single best set from the three proposals.
    Falls back to single-agent if two or more agents fail.
    """
    agent_configs = [
        ("Bullish Grid Analyst",
         "Maximise trade frequency and weekly return. Prefer tighter spacing and more levels."),
        ("Bearish Grid Analyst",
         "Minimise drawdown and fee drag. Prefer wider spacing and lower capital_pct."),
        ("Grid Risk Manager",
         "Balance return against kill-switch distance. Prioritise capital preservation."),
    ]

    proposals = []
    for role, stance in agent_configs:
        print(f"[optimiser] {role}...")
        prompt = _build_prompt(
            metrics_7d, current_config, regime, btc_price_usdt,
            candidates=candidates, agent_role=role, agent_stance=stance,
        )
        raw = _raw_ai_call(prompt)
        if not raw:
            continue
        try:
            prop = json.loads(raw)
            proposals.append({"agent": role, "proposal": prop})
            print(f"[optimiser]   → spacing={prop.get('spacing_pct')} levels={prop.get('levels')} "
                  f"capital={prop.get('capital_pct')}")
        except Exception as e:
            print(f"[optimiser]   {role} parse failed: {e}")

    if not proposals:
        return None
    if len(proposals) == 1:
        return proposals[0]["proposal"]

    # Synthesiser selects the consensus
    total_capital = current_config.get("total_capital", 150)
    gbp_usd       = current_config.get("gbp_usd_rate", 1.27)
    synth_prompt  = f"""You are a senior quantitative risk officer for a BTC/USDT spot grid bot.
Capital: £{total_capital}. Weekly kill switch: 10%. Regime: {regime}.
7-day performance: {json.dumps(metrics_7d)}

Three analysts proposed parameters:
{json.dumps([p["proposal"] for p in proposals], indent=2)}

Choose the single best set. Rules: spacing_pct > 0.55, levels 4–20, capital_pct 0.50–0.80.
Prioritise capital preservation. Keep changes minimal if win_rate > 60% and fee_drag < 30%.

Return ONLY valid JSON, no markdown:
{{"instrument":"BTC_USDT","spacing_pct":<float>,"range_pct":<float>,"levels":<int>,"capital_pct":<float>,"total_capital":{total_capital},"gbp_usd_rate":{gbp_usd},"kill_pct":<float>,"rationale":"<one sentence>"}}"""

    raw = _raw_ai_call(synth_prompt)
    if raw:
        try:
            return json.loads(raw)
        except Exception as e:
            print(f"[optimiser] Synthesiser parse failed: {e}")

    # Fallback: return the Risk Manager's proposal
    for p in proposals:
        if "Risk" in p["agent"]:
            return p["proposal"]
    return proposals[-1]["proposal"]


def ask_ai(
    metrics_7d: dict,
    current_config: dict,
    regime: str,
    btc_price_usdt: float = 0.0,
    candidates: list | None = None,
) -> dict | None:
    """
    Try multi-agent debate first; fall back to single-agent on failure.
    Injects Optuna candidates (Skill 5) into every agent prompt.
    """
    candidates = candidates or []
    # Multi-agent debate (Skill 10)
    result = _multi_agent_debate(metrics_7d, current_config, regime, btc_price_usdt, candidates)
    if result:
        return result

    # Single-agent fallback (original behaviour)
    print("[optimiser] Multi-agent debate failed — falling back to single-agent call")
    prompt = _build_prompt(
        metrics_7d, current_config, regime, btc_price_usdt, candidates=candidates
    )

    raw = _raw_ai_call(prompt)
    if raw:
        try:
            return json.loads(raw)
        except Exception as e:
            print(f"[optimiser] Single-agent parse failed: {e}")

    print("[optimiser] All AI providers failed — keeping current parameters.")
    return None


# ------------------------------------------------------------------ #
#  Walk-forward simulation                                             #
# ------------------------------------------------------------------ #

def simulate_return(candles: list[dict], config: dict) -> float:
    """
    Walk-forward simulation estimating net return % over the candle period.

    Fixes vs the prior version:
    - Uses per-level capital (not total deployed) as the unit of return
    - Distinguishes ranging days (completed round trips) from trending days
      (zero round trips + recalibration drag)
    - Expresses the result as % of total deployed capital
    """
    spacing_pct     = config["spacing_pct"] / 100
    fee_pct         = 0.0025   # 0.25% maker fee per leg (crypto.com Exchange VIP 0 / default tier)
    levels          = config["levels"]
    capital_usdt    = (
        config.get("total_capital", 150)
        * config.get("capital_pct", 0.70)
        * config.get("gbp_usd_rate", 1.27)
    )
    per_level_usdt  = capital_usdt / levels if levels else 0
    deployed_usdt   = capital_usdt

    total_usdt = 0.0
    for c in candles:
        high  = c["high"]
        low   = c["low"]
        close = c["close"]
        # Use open if available; fall back to midpoint of the day's range
        open_p = c.get("open", (high + low) / 2)

        daily_move_pct = abs(close - open_p) / open_p if open_p else 0
        # A day is "ranging" when the directional move is small relative to spacing
        is_ranging = daily_move_pct < (spacing_pct * 2)

        if is_ranging:
            daily_range_pct = (high - low) / close if close else 0
            round_trips     = min(daily_range_pct / (spacing_pct * 2), levels / 2)
            gross           = round_trips * per_level_usdt * spacing_pct
            fees            = round_trips * per_level_usdt * fee_pct * 2
        else:
            # Trending day: assume no completed round trips; only recalibration drag.
            # Use the actual range_pct threshold (not a hardcoded 3%) so the simulation
            # reflects how often the real bot actually recenters.
            recenter_threshold = config.get("range_pct", 5.0) / 100
            recal_count = math.floor(daily_move_pct / recenter_threshold) if recenter_threshold else 0
            gross = 0.0
            fees  = recal_count * per_level_usdt * fee_pct * 2

        total_usdt += gross - fees

    if deployed_usdt > 0:
        return round(total_usdt / deployed_usdt * 100, 2)
    return 0.0


def walk_forward_confirms(
    candles: list[dict],
    current: dict,
    proposed: dict,
) -> tuple[bool, float, float]:
    """
    Return (accepted, current_return, proposed_return).
    Accept if proposed return > current return AND proposed return > 0.
    Both-negative: log a warning but accept if proposed is clearly better (>0.1pp).
    """
    curr_ret = simulate_return(candles, current)
    prop_ret = simulate_return(candles, proposed)
    if prop_ret <= 0 and curr_ret <= 0:
        # Both regimes look unprofitable — accept only if proposed is meaningfully better
        accepted = (prop_ret - curr_ret) > 0.10
        print(f"[optimiser] Walk-forward: both configs negative (curr={curr_ret:.2f}% prop={prop_ret:.2f}%) "
              f"— {'accepting marginal improvement' if accepted else 'keeping current (not enough improvement)'}")
        return accepted, curr_ret, prop_ret
    return prop_ret > curr_ret, curr_ret, prop_ret


# ------------------------------------------------------------------ #
#  Telegram                                                            #
# ------------------------------------------------------------------ #

def send_telegram(msg: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print(f"[optimiser] Telegram: {msg}")
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as e:
        print(f"[optimiser] Telegram send failed: {e}")


# ------------------------------------------------------------------ #
#  Main                                                                #
# ------------------------------------------------------------------ #

def run() -> None:
    print("[optimiser] Starting weekly Gemini optimisation...")

    # Load current config
    current_config = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}

    # Load regime
    regime = "ranging"
    if REGIME_FILE.exists():
        regime = json.loads(REGIME_FILE.read_text()).get("regime", "ranging")

    # Compute last-7-day metrics
    since_7d  = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    trades_7d = read_since(since_7d)
    metrics   = compute_metrics(trades_7d)

    print(f"[optimiser] 7-day metrics: {metrics}")

    # Fetch 30 days of candles for walk-forward + current price for capital check
    cdx     = CDXClient()
    candles = cdx.get_candlesticks("BTC_USDT", timeframe="1D", count=30)
    try:
        ticker    = cdx.get_ticker("BTC_USDT")
        btc_price = float(ticker.get("ask", ticker.get("last", 0)))
    except Exception:
        btc_price = 0.0

    # Load Optuna pre-validated candidates (Skill 5)
    candidates = _load_optuna_candidates()

    # Multi-agent AI debate → consensus (Skill 10)
    proposed = ask_ai(metrics, current_config, regime, btc_price_usdt=btc_price, candidates=candidates)

    if proposed is None:
        send_telegram(
            "🔄 *Weekly optimisation*\n"
            "AI unavailable (all providers failed) — keeping current parameters.\n"
            f"7-day net P&L: £{metrics.get('total_net_gbp', 0):.2f}"
        )
        return

    rationale = proposed.pop("rationale", "No rationale provided.")

    # Safety gate — reject any params outside hard bounds before walk-forward
    violations = validate_params(proposed)
    if violations:
        msg = "⚠️ *Gemini proposed unsafe params — rejected*\n" + "\n".join(violations)
        send_telegram(msg)
        print(f"[optimiser] Unsafe proposal rejected: {violations}")
        return

    # Capital headroom check — reject if proposed config requires too much minimum capital
    if btc_price > 0:
        min_cap = (
            0.0001
            * btc_price
            * proposed.get("levels", 10)
            / proposed.get("capital_pct", 0.70)
            / proposed.get("gbp_usd_rate", 1.27)
        )
        total_cap = proposed.get("total_capital", 150)
        if min_cap > total_cap * 0.90:
            msg = (
                f"⚠️ *Gemini proposed params rejected — capital headroom too thin*\n"
                f"Proposed levels={proposed.get('levels')} requires £{min_cap:.0f} minimum "
                f"(capital: £{total_cap}, limit: £{total_cap * 0.90:.0f})"
            )
            send_telegram(msg)
            print(f"[optimiser] Capital headroom check failed: min_cap=£{min_cap:.0f}")
            return

    # Walk-forward validation
    accepted, curr_ret, prop_ret = walk_forward_confirms(candles, current_config, proposed)

    if accepted:
        # Merge proposed into current config so keys the AI didn't touch
        # (e.g. _notes, instrument) are preserved.
        merged = dict(current_config)
        merged.update(proposed)
        _atomic_write(CONFIG_FILE, json.dumps(merged, indent=2))
        action  = "✅ Parameters updated"
        details = (
            f"Old spacing: {current_config.get('spacing_pct')}% → New: {proposed.get('spacing_pct')}%\n"
            f"Old range: ±{current_config.get('range_pct')}% → New: ±{proposed.get('range_pct')}%\n"
            f"Old levels: {current_config.get('levels')} → New: {proposed.get('levels')}\n"
            f"Walk-forward: {curr_ret:.2f}% → {prop_ret:.2f}%"
        )
    else:
        action  = "⏸ Parameters unchanged (walk-forward did not confirm improvement)"
        details = f"Walk-forward: current {curr_ret:.2f}% vs proposed {prop_ret:.2f}%"

    optuna_note = f" · Optuna {len(candidates)} candidates" if candidates else ""
    send_telegram(
        f"🔄 *Weekly optimisation (multi-agent{optuna_note})*\n"
        f"{action}\n\n"
        f"_{rationale}_\n\n"
        f"{details}\n\n"
        f"7-day: {metrics.get('trades_total', 0)} trades · "
        f"win rate {metrics.get('win_rate_pct', 0)}% · "
        f"net £{metrics.get('total_net_gbp', 0):.2f}\n"
        f"New week starts now. P&L counter reset."
    )
    print(f"[optimiser] Done. {action}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    run()
