"""
Optuna Bayesian grid parameter optimiser — Skill 5.

Runs before the weekly Gemini review (Saturday 22:30 UTC) to produce
numerically validated top-3 parameter candidates via Bayesian optimisation
(TPE sampler). Gemini then selects from these candidates rather than
searching blind.

Usage
-----
  Standalone:
    python3 src/trading/optuna_optimizer.py

  From gemini_optimizer.py (imported):
    from trading.optuna_optimizer import run as run_optuna

Output
------
  data/optuna_candidates.json — top-3 candidates with estimated return %
  Candidates are read by gemini_optimizer.py and injected into the prompt.
"""

import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

CANDIDATES_FILE = ROOT / "data" / "optuna_candidates.json"
CONFIG_FILE     = ROOT / "config" / "grid_params.json"

SAFE_BOUNDS = {
    "spacing_pct": (0.55, 2.5),
    "range_pct":   (3.0,  10.0),
    "levels":      (6,    16),
    "capital_pct": (0.50, 0.80),
    "kill_pct":    (0.05, 0.15),
}


# ------------------------------------------------------------------ #
#  Simulation (mirrors gemini_optimizer.simulate_return but richer)   #
# ------------------------------------------------------------------ #

def simulate_return(candles: list[dict], config: dict) -> float:
    """
    Walk-forward simulation on daily candles.

    More detailed than the original:
      - Counts intra-day fills based on daily range vs spacing
      - Accounts for fee on both BUY and SELL legs (round-trip)
      - Caps daily fills at levels/2 (can't fill more than half the grid)
      - Adds a slight penalty if spacing < 0.7% (taker-fill risk)
    """
    spacing_pct = config["spacing_pct"] / 100
    capital_pct = config["capital_pct"]
    levels      = config["levels"]
    fee_pct     = 0.0025   # 0.25% maker per leg

    # Taker risk penalty when spacing is very tight
    taker_risk_pct = max(0.0, (0.007 - spacing_pct) / 0.007 * 0.001)

    total_return = 0.0
    for c in candles:
        daily_range_pct = (c["high"] - c["low"]) / c["close"]
        fills           = min(daily_range_pct / spacing_pct, levels / 2)
        # Both gross and fees must scale with capital_pct so Optuna can't inflate
        # net return by simply increasing capital_pct while fees stay constant.
        # The return is expressed per unit of total capital (capital_pct cancels
        # when comparing params but prevents the bias toward always picking 0.80).
        gross           = fills * spacing_pct * capital_pct
        fees            = fills * (fee_pct * 2 + taker_risk_pct) * capital_pct
        total_return   += gross - fees

    return round(total_return * 100, 4)


# ------------------------------------------------------------------ #
#  Optuna study                                                        #
# ------------------------------------------------------------------ #

def run(
    candles:   list[dict],
    n_trials:  int  = 300,
    top_n:     int  = 3,
    regime:    str  = "ranging",
) -> list[dict]:
    """
    Run Bayesian optimisation over grid parameters.
    Returns the top `top_n` parameter sets ordered by estimated return.

    The regime biases the search space — avoids proposing wide spacing
    in a ranging market or tight spacing in a volatile one.
    """
    try:
        import optuna
        optuna.logging.set_verbosity(optuna.logging.WARNING)
    except ImportError:
        print("[optuna] optuna not installed — skipping Bayesian sweep.")
        return []

    # Regime-aware search space bounds
    spacing_lo, spacing_hi = {
        "ranging":     (0.60, 1.20),
        "volatile":    (1.00, 2.50),
        "trending_up": (0.90, 1.80),
        "trending_dn": (0.70, 1.50),
    }.get(regime, (0.55, 2.50))

    def objective(trial):
        config = {
            "spacing_pct": trial.suggest_float("spacing_pct", spacing_lo, spacing_hi),
            "range_pct":   trial.suggest_float("range_pct",   3.0,        10.0),
            "levels":      trial.suggest_int(  "levels",      6,          16),
            "capital_pct": trial.suggest_float("capital_pct", 0.50,       0.80),
            "kill_pct":    trial.suggest_float("kill_pct",    0.05,       0.15),
        }
        return simulate_return(candles, config)

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)

    seen   = set()
    top    = []
    for trial in sorted(study.trials, key=lambda t: t.value or 0, reverse=True):
        params = trial.params
        # De-duplicate — round spacing to 2dp to avoid near-identical entries
        key    = round(params.get("spacing_pct", 0), 2)
        if key in seen:
            continue
        seen.add(key)
        top.append({
            "spacing_pct": round(params["spacing_pct"], 3),
            "range_pct":   round(params["range_pct"],   2),
            "levels":      int(params["levels"]),
            "capital_pct": round(params["capital_pct"], 3),
            "kill_pct":    round(params["kill_pct"],    3),
            "estimated_return_pct": round(trial.value or 0, 4),
        })
        if len(top) >= top_n:
            break

    print(f"[optuna] Top {len(top)} candidates after {n_trials} trials:")
    for i, c in enumerate(top, 1):
        print(f"  #{i}: spacing={c['spacing_pct']}% levels={c['levels']} "
              f"capital={c['capital_pct']} → est. return={c['estimated_return_pct']}%")

    return top


# ------------------------------------------------------------------ #
#  Main                                                                #
# ------------------------------------------------------------------ #

def main() -> None:
    from trading.cdx_client import CDXClient

    print("[optuna] Fetching 30-day candles for optimisation...")
    cdx     = CDXClient()
    candles = cdx.get_candlesticks("BTC_USDT", timeframe="1D", count=30)

    regime = "ranging"
    regime_file = ROOT / "data" / "regime.json"
    if regime_file.exists():
        try:
            regime = json.loads(regime_file.read_text()).get("regime", "ranging")
        except Exception:
            pass

    candidates = run(candles, n_trials=300, regime=regime)

    payload = json.dumps(
        {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "regime":       regime,
            "candidates":   candidates,
        },
        indent=2,
    )
    CANDIDATES_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CANDIDATES_FILE.parent, prefix=".tmp_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp, CANDIDATES_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    print(f"[optuna] Candidates saved to {CANDIDATES_FILE}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    main()
