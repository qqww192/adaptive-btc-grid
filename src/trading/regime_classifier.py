"""
Regime classifier — run every 4 hours via crontab.

Fetches 30 daily candles from crypto.com, computes:
  1. HMM-based regime (primary, Skill 2) — learns transition probabilities
     from BTC log-returns + log-range features. 3-state Gaussian HMM.
  2. Rule-based fallback (ATR-14 + Bollinger Band Width) — used when
     insufficient data for HMM or hmmlearn is not installed.

Output (data/regime.json) now includes:
  - regime           : string label
  - hmm_confidence   : float 0–1 (posterior probability of current state)
  - hmm_available    : bool (whether HMM ran successfully)
  - atr, bbw_pct     : classic indicators (always computed)
  - recommended      : param set for this regime
  - updated_at       : ISO timestamp

Regimes:
  ranging     — low volatility, tight bands → tighter grid (0.6–0.8%)
  trending_up — sustained upward move       → widen grid ceiling
  trending_dn — sustained downward move     → tighten kill switch
  volatile    — high ATR + wide BBW         → widen grid (1.2–1.5%)
"""

import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from trading.cdx_client import CDXClient
from trading.news_sentiment import get_market_sentiment, summarise
from trading.ai_advisor import ask_strategy_stance


def _send_telegram(message: str) -> None:
    token   = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception:
        pass


DATA_FILE         = ROOT / "data" / "regime.json"
TREND_PAUSE_FLAG  = ROOT / "data" / "trend_pause.flag"
INSTRUMENT        = "BTC_USDT"

# Minimum HMM confidence required before activating the trend pause.
# At lower confidence the signal is unreliable and pausing would forfeit
# good ranging fills unnecessarily.
TREND_PAUSE_CONFIDENCE = 0.70


# ------------------------------------------------------------------ #
#  Skill 2: HMM regime detection                                      #
# ------------------------------------------------------------------ #

def _build_hmm_features(candles: list[dict]) -> np.ndarray:
    """
    Feature matrix for HMM: [log_return, log_range_pct].
    log_range normalises volatility magnitude relative to price level.
    """
    closes = np.array([c["close"] for c in candles], dtype=float)
    highs  = np.array([c["high"]  for c in candles], dtype=float)
    lows   = np.array([c["low"]   for c in candles], dtype=float)
    log_returns = np.diff(np.log(closes))
    log_ranges  = np.log((highs[1:] - lows[1:]) / closes[1:] + 1e-8)
    return np.column_stack([log_returns, log_ranges])


def _fit_hmm(features: np.ndarray, n_components: int = 3):
    """Fit a Gaussian HMM and return the model."""
    from hmmlearn.hmm import GaussianHMM
    model = GaussianHMM(
        n_components=n_components,
        covariance_type="diag",
        n_iter=200,
        random_state=42,
        tol=1e-4,
    )
    model.fit(features)
    return model


def classify_hmm(candles: list[dict]) -> tuple[str, float]:
    """
    Classify market regime using a 3-state Gaussian HMM.

    States are ranked by their mean log_return and volatility to assign labels:
      - highest vol  → volatile
      - highest ret  → trending_up
      - lowest ret   → trending_dn
      - middle       → ranging

    Returns (regime_label, confidence) where confidence is the posterior
    probability of the predicted state for the most recent observation.
    """
    if len(candles) < 20:
        return "ranging", 0.5

    features = _build_hmm_features(candles)

    try:
        model = _fit_hmm(features)
    except Exception as e:
        print(f"[regime] HMM fitting failed: {e} — falling back to rule-based")
        raise

    states    = model.predict(features)
    posteriors = model.predict_proba(features)
    current_state = int(states[-1])
    confidence    = float(posteriors[-1, current_state])

    # Characterise each state
    state_mean_ret = model.means_[:, 0]          # mean log_return per state
    state_vol      = np.sqrt(model.covars_[:, 0]) # std of log_return per state

    vol_rank = int(np.argmax(state_vol))
    ret_rank = np.argsort(state_mean_ret)         # ascending

    if current_state == vol_rank:
        regime = "volatile"
    elif current_state == ret_rank[-1]:
        regime = "trending_up"
    elif current_state == ret_rank[0]:
        regime = "trending_dn"
    else:
        regime = "ranging"

    return regime, confidence


# ------------------------------------------------------------------ #
#  Classic indicator helpers (fallback / cross-check)                  #
# ------------------------------------------------------------------ #

def compute_atr(candles: list[dict], period: int = 14) -> float:
    """Average True Range over `period` candles."""
    trs = []
    for i in range(1, len(candles)):
        high = candles[i]["high"]
        low  = candles[i]["low"]
        prev = candles[i - 1]["close"]
        trs.append(max(high - low, abs(high - prev), abs(low - prev)))
    if len(trs) < period:
        return float("nan")
    return sum(trs[-period:]) / period


def compute_bbw(candles: list[dict], period: int = 20) -> float:
    """Bollinger Band Width = (upper − lower) / middle."""
    closes = [c["close"] for c in candles[-period:]]
    if len(closes) < period:
        return float("nan")
    mean   = sum(closes) / period
    stddev = math.sqrt(sum((x - mean) ** 2 for x in closes) / period)
    upper  = mean + 2 * stddev
    lower  = mean - 2 * stddev
    return (upper - lower) / mean if mean else float("nan")


def classify_rules(atr: float, bbw: float, candles: list[dict]) -> str:
    """Rule-based classifier — ATR-14 + BBW + 5-candle slope."""
    price   = candles[-1]["close"]
    atr_pct = (atr / price) * 100 if price else 0
    bbw_pct = bbw * 100
    recent  = [c["close"] for c in candles[-5:]]
    slope   = (recent[-1] - recent[0]) / recent[0] * 100

    if bbw_pct < 3.0 and atr_pct < 1.5:
        return "ranging"
    elif bbw_pct > 6.0 or atr_pct > 3.5:
        return "volatile"
    elif slope > 4.0:
        return "trending_up"
    elif slope < -4.0:
        return "trending_dn"
    else:
        return "ranging"


# ------------------------------------------------------------------ #
#  Recommended grid params per regime                                  #
# ------------------------------------------------------------------ #

REGIME_PARAMS = {
    # ranging: optimal for small capital — 1.0% spacing gives 0.50% net vs 0.30% at 0.8%
    # asymmetric 4:2 buy/sell skew; trend pause prevents operation during trending phases
    "ranging": {
        "spacing_pct": 1.0,
        "range_pct":   5.0,
        "levels":      6,
        "capital_pct": 0.70,
        "kill_pct":    0.10,
    },
    # trending_up: grid continues only when HMM confidence < 0.70; wide spacing to ride swings
    "trending_up": {
        "spacing_pct": 1.4,
        "range_pct":   7.0,
        "levels":      6,
        "capital_pct": 0.55,
        "kill_pct":    0.10,
    },
    # trending_dn: reduced capital, tighter kill; grid runs only when confidence < 0.70
    "trending_dn": {
        "spacing_pct": 1.2,
        "range_pct":   5.0,
        "levels":      6,
        "capital_pct": 0.50,
        "kill_pct":    0.08,
    },
    # volatile: widest spacing to survive large swings without recentering
    "volatile": {
        "spacing_pct": 1.6,
        "range_pct":   8.0,
        "levels":      6,
        "capital_pct": 0.60,
        "kill_pct":    0.10,
    },
}


# ------------------------------------------------------------------ #
#  Main                                                                #
# ------------------------------------------------------------------ #

def run() -> None:
    cdx     = CDXClient()
    candles = cdx.get_candlesticks(INSTRUMENT, timeframe="1D", count=30)

    if len(candles) < 20:
        print("[regime] Not enough candle data — skipping regime update.")
        return

    # Classic indicators (always computed — used in daily_reporter + fallback)
    atr = compute_atr(candles)
    bbw = compute_bbw(candles)
    if math.isnan(atr) or math.isnan(bbw):
        print(f"[regime] Insufficient candle data (atr={atr}, bbw={bbw}) — skipping")
        return

    # HMM classification (primary)
    hmm_available = False
    hmm_confidence = 0.0
    try:
        regime, hmm_confidence = classify_hmm(candles)
        hmm_available = True
        print(f"[regime] HMM: {regime} (confidence={hmm_confidence:.2f})")
    except Exception:
        regime = classify_rules(atr, bbw, candles)
        print(f"[regime] Rule-based fallback: {regime}")

    # Cross-check: if HMM confidence is low (<0.6), blend with rule-based.
    # When rule-based overrides, treat confidence as 0.5 (rule-based certainty proxy)
    # so grid_trader's apply_regime_params() knows to distrust the HMM value.
    if hmm_available and hmm_confidence < 0.6:
        rule_regime = classify_rules(atr, bbw, candles)
        if rule_regime != regime:
            print(f"[regime] Low HMM confidence — rule-based ({rule_regime}) overrides HMM ({regime})")
            regime         = rule_regime
            hmm_confidence = 0.5   # synthetic: rule-based is more certain than low-conf HMM

    params = REGIME_PARAMS[regime]

    # ── News-aware strategy stance (bounded recommendation) ──────────────────
    # Fetch market sentiment (populates the 4h cache the grid loop reads) and
    # ask the AI for a safe stance. The stance only selects among already-safe
    # behaviours; STAND_ASIDE can pause, never force a trade. Fully degrades to
    # NEUTRAL if news or AI is unavailable.
    sentiment = get_market_sentiment()
    try:
        stance = ask_strategy_stance(regime, sentiment, candles)
    except Exception as e:
        print(f"[regime] Stance fetch failed: {e} — defaulting to NEUTRAL")
        stance = "NEUTRAL"
    print(f"[regime] Stance: {stance} | Sentiment: {summarise(sentiment) or 'n/a'}")

    # ── Trend / stand-aside pause flag ───────────────────────────────────────
    # The flag tells grid_trader to stand aside entirely (avoiding recenter fee
    # drag). It activates on a confirmed strong trend OR an AI STAND_ASIDE
    # stance, and clears as soon as neither holds.
    was_paused      = TREND_PAUSE_FLAG.exists()
    is_strong_trend = (
        regime in ("trending_up", "trending_dn")
        and hmm_confidence >= TREND_PAUSE_CONFIDENCE
    )
    stand_aside  = (stance == "STAND_ASIDE")
    should_pause = is_strong_trend or stand_aside
    if should_pause:
        TREND_PAUSE_FLAG.parent.mkdir(parents=True, exist_ok=True)
        TREND_PAUSE_FLAG.touch()
        reason = "strong trend" if is_strong_trend else "AI stance STAND_ASIDE"
        print(f"[regime] Pause ACTIVATED ({reason}; {regime} conf={hmm_confidence:.2f}) "
              f"— grid_trader will stand aside until conditions clear")
        if not was_paused:
            if is_strong_trend:
                direction = "uptrend" if regime == "trending_up" else "downtrend"
                headline = f"strong {direction} detected"
            else:
                headline = "AI recommends standing aside"
            _send_telegram(
                f"⏸ *Grid paused — {headline}*\n"
                f"Regime: `{regime}` (HMM {hmm_confidence:.0%} confident) · Stance: `{stance}`\n"
                f"ATR: ${atr:,.0f} | BBW: {bbw * 100:.1f}%\n"
                f"Standing aside to protect capital. Will resume when conditions clear."
            )
    else:
        TREND_PAUSE_FLAG.unlink(missing_ok=True)
        if regime in ("trending_up", "trending_dn"):
            print(f"[regime] Trend detected but confidence too low ({hmm_confidence:.2f} < "
                  f"{TREND_PAUSE_CONFIDENCE}) — grid continues")
        if was_paused:
            _send_telegram(
                f"▶️ *Grid resumed — conditions cleared*\n"
                f"Regime: `{regime}` (HMM {hmm_confidence:.0%} confident) · Stance: `{stance}`\n"
                f"Grid is active again."
            )

    payload = json.dumps(
        {
            "regime":         regime,
            "hmm_available":  hmm_available,
            "hmm_confidence": round(hmm_confidence, 3),
            "atr":            round(atr, 2),
            "bbw_pct":        round(bbw * 100, 2),
            "recommended":    params,
            "stance":         stance,
            "sentiment":      sentiment,
            "updated_at":     datetime.now(timezone.utc).isoformat(),
        },
        indent=2,
    )
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=DATA_FILE.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp, DATA_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print(
        f"[regime] {regime} | ATR={atr:.0f} | BBW={bbw*100:.1f}% | "
        f"HMM={'✓' if hmm_available else '✗'} conf={hmm_confidence:.2f} | "
        f"spacing={params['spacing_pct']}% | range=±{params['range_pct']}%"
    )


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    run()
