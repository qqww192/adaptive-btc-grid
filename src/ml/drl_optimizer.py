"""
Deep Reinforcement Learning parameter optimiser — Skill 9 (FinRL-Trading).

Provides:
  1. GridTradingEnv  — a Gymnasium environment where the agent controls
     grid parameters (spacing_pct, levels, capital_pct) and receives
     rewards based on realised P&L minus drawdown penalty.
  2. train()         — trains a PPO/SAC agent using stable-baselines3.
  3. suggest()       — loads a trained model and suggests parameters for
     the current regime + recent metrics.
  4. A CLI for training and inference.

Heavy dependencies (install separately when capital justifies it):
  pip install stable-baselines3 gymnasium torch finrl

The environment uses the same OHLCV data as the rest of the bot,
so it can be trained on any window of historical BTC candles.

Training example (run once, on a machine with more compute):
  python3 src/ml/drl_optimizer.py train --days 365 --timesteps 200000

Inference (runs in ~50ms on Oracle Cloud ARM):
  python3 src/ml/drl_optimizer.py suggest
  # → prints suggested params as JSON; integrate into gemini_optimizer.py

Architecture
------------
State  : [spacing_pct, levels, capital_pct, atr_pct, bbw_pct, win_rate, fee_drag]
Action : Δspacing ∈ [-0.2, +0.2], Δlevels ∈ {-2,-1,0,+1,+2}, Δcapital ∈ [-0.1, +0.1]
Reward : weekly_net_pnl_gbp − 0.5 × max_drawdown_gbp
         (penalises deep drawdowns to match John's low-risk preference)
"""

import json
import math
import os
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

MODEL_DIR   = ROOT / "data" / "drl_models"
CONFIG_FILE = ROOT / "config" / "grid_params.json"
REGIME_FILE = ROOT / "data"  / "regime.json"

SAFE_BOUNDS = {
    "spacing_pct": (0.55, 2.5),
    "levels":      (6,    16),
    "capital_pct": (0.50, 0.80),
}


# ------------------------------------------------------------------ #
#  Gymnasium environment                                               #
# ------------------------------------------------------------------ #

def _make_env(candles: list[dict], config: dict):
    """
    Build a GridTradingEnv. Returns None if gymnasium/numpy not installed.
    The environment is self-contained and does not call the live exchange.
    """
    try:
        import numpy as np
        import gymnasium as gym
        from gymnasium import spaces
    except ImportError:
        print("[drl] gymnasium not installed — run: pip install gymnasium")
        return None

    MAKER_FEE = 0.0025
    GBP_RATE  = float(config.get("gbp_usd_rate", 1.27))

    class GridTradingEnv(gym.Env):
        """
        Episode = one pass through the provided candles.
        State vector: [spacing_pct, levels_norm, capital_pct,
                       atr_pct, bbw_pct, win_rate, fee_drag]
        Action vector: [delta_spacing, delta_levels_disc, delta_capital]
        """
        metadata = {"render_modes": []}

        def __init__(self):
            super().__init__()
            self.candles     = candles
            self.base_config = dict(config)

            # Continuous action: [Δspacing ∈ [-0.2, 0.2],
            #                     Δlevels  ∈ [-2, 2] (rounded to int),
            #                     Δcapital ∈ [-0.1, 0.1]]
            self.action_space = spaces.Box(
                low  = np.array([-0.2, -2.0, -0.1], dtype=np.float32),
                high = np.array([ 0.2,  2.0,  0.1], dtype=np.float32),
            )
            # Observation: 7 normalised features
            self.observation_space = spaces.Box(
                low  = np.zeros(7, dtype=np.float32),
                high = np.ones(7, dtype=np.float32),
            )
            self.reset()

        def reset(self, *, seed=None, options=None):
            super().reset(seed=seed)
            self.step_idx    = 0
            self.spacing     = float(self.base_config.get("spacing_pct", 1.0))
            self.levels      = int(self.base_config.get("levels", 6))
            self.capital_pct = float(self.base_config.get("capital_pct", 0.70))
            self.cumulative_pnl   = 0.0
            self.peak_pnl         = 0.0
            self.weekly_sells     = []
            self.weekly_gross     = 0.0
            self.weekly_fees      = 0.0
            return self._obs(), {}

        def _obs(self) -> "np.ndarray":
            import numpy as np
            c        = self.candles[min(self.step_idx, len(self.candles) - 1)]
            closes   = [x["close"] for x in self.candles[max(0, self.step_idx-14):self.step_idx+1]]
            price    = c["close"]
            atr_norm = min((c["high"] - c["low"]) / price, 0.1) / 0.1
            bbw      = 0.0
            if len(closes) >= 5:
                mean   = sum(closes) / len(closes)
                std    = math.sqrt(sum((x - mean) ** 2 for x in closes) / len(closes))
                bbw    = min((4 * std) / mean if mean else 0, 0.15) / 0.15

            win_rate  = (sum(1 for x in self.weekly_sells if x > 0) / len(self.weekly_sells)
                         if self.weekly_sells else 0.5)
            fee_drag  = min(self.weekly_fees / max(self.weekly_gross, 1e-6), 1.0)

            return np.array([
                (self.spacing - 0.55) / (2.5 - 0.55),
                (self.levels  - 6)    / (16 - 6),
                (self.capital_pct - 0.50) / (0.80 - 0.50),
                atr_norm, bbw, win_rate, fee_drag,
            ], dtype=np.float32)

        def step(self, action):
            # Apply action deltas (clipped to safe bounds)
            self.spacing     = float(np.clip(self.spacing + action[0], 0.55, 2.5))
            self.levels      = int(np.clip(self.levels + round(action[1]), 6, 16))
            self.capital_pct = float(np.clip(self.capital_pct + action[2], 0.50, 0.80))

            candle   = self.candles[self.step_idx]
            high, low, close = candle["high"], candle["low"], candle["close"]
            spacing  = self.spacing / 100
            capital  = (self.base_config.get("total_capital", 150)
                        * self.capital_pct
                        * GBP_RATE)
            per_lvl  = capital / self.levels

            # Simulate fills for this candle
            daily_range = (high - low) / close
            round_trips = min(daily_range / (spacing * 2), self.levels / 2)
            gross_usdt  = round_trips * per_lvl * spacing
            fee_usdt    = round_trips * per_lvl * MAKER_FEE * 2
            net_gbp     = (gross_usdt - fee_usdt) / GBP_RATE

            self.weekly_gross += gross_usdt / GBP_RATE
            self.weekly_fees  += fee_usdt  / GBP_RATE
            if round_trips > 0:
                self.weekly_sells.append(net_gbp / max(round_trips, 1))

            self.cumulative_pnl += net_gbp
            self.peak_pnl        = max(self.peak_pnl, self.cumulative_pnl)
            drawdown             = self.peak_pnl - self.cumulative_pnl
            reward               = net_gbp - 0.5 * drawdown   # penalise deep drawdowns

            self.step_idx += 1
            terminated = self.step_idx >= len(self.candles)
            return self._obs(), float(reward), terminated, False, {}

    return GridTradingEnv()


# ------------------------------------------------------------------ #
#  Training                                                            #
# ------------------------------------------------------------------ #

def train(candles: list[dict], config: dict, total_timesteps: int = 200_000) -> Optional[Path]:
    """
    Train a PPO agent on the GridTradingEnv.
    Returns the path to the saved model, or None if dependencies missing.
    """
    env = _make_env(candles, config)
    if env is None:
        return None

    try:
        from stable_baselines3 import PPO
        from stable_baselines3.common.env_checker import check_env
    except ImportError:
        print("[drl] stable-baselines3 not installed — run: pip install stable-baselines3")
        return None

    print(f"[drl] Training PPO for {total_timesteps:,} timesteps on "
          f"{len(candles)} candles...")
    check_env(env, warn=True)
    model = PPO("MlpPolicy", env, verbose=0, learning_rate=3e-4, n_steps=512)
    model.learn(total_timesteps=total_timesteps)

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model_path = MODEL_DIR / "ppo_grid.zip"
    model.save(str(model_path))
    print(f"[drl] Model saved to {model_path}")
    return model_path


# ------------------------------------------------------------------ #
#  Inference                                                           #
# ------------------------------------------------------------------ #

def suggest(config: dict, candles: list[dict]) -> Optional[dict]:
    """
    Load the trained PPO model and run a single episode to suggest params.
    Returns a dict with suggested spacing_pct, levels, capital_pct, or None.
    """
    model_path = MODEL_DIR / "ppo_grid.zip"
    if not model_path.exists():
        print(f"[drl] No trained model at {model_path} — run: python3 src/ml/drl_optimizer.py train")
        return None

    try:
        from stable_baselines3 import PPO
        import numpy as np
    except ImportError:
        print("[drl] stable-baselines3 not installed.")
        return None

    env   = _make_env(candles, config)
    if env is None:
        return None

    model = PPO.load(str(model_path))
    obs, _ = env.reset()
    done   = False
    while not done:
        action, _ = model.predict(obs, deterministic=True)
        obs, _, terminated, truncated, _ = env.step(action)
        done = terminated or truncated

    return {
        "spacing_pct": round(env.spacing, 3),
        "levels":      env.levels,
        "capital_pct": round(env.capital_pct, 3),
        "source":      "drl_ppo",
    }


# ------------------------------------------------------------------ #
#  CLI                                                                 #
# ------------------------------------------------------------------ #

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="DRL grid parameter optimiser")
    sub    = parser.add_subparsers(dest="command")

    train_p = sub.add_parser("train")
    train_p.add_argument("--days",       type=int, default=365)
    train_p.add_argument("--timesteps",  type=int, default=200_000)

    sub.add_parser("suggest")
    args = parser.parse_args()

    from dotenv import load_dotenv
    from trading.cdx_client import CDXClient
    load_dotenv(ROOT / ".env")

    config = {}
    if CONFIG_FILE.exists():
        config = json.loads(CONFIG_FILE.read_text())

    cdx     = CDXClient()
    days    = getattr(args, "days", 90)
    candles = cdx.get_candlesticks("BTC_USDT", timeframe="1D", count=days)
    print(f"[drl] Fetched {len(candles)} candles")

    if args.command == "train":
        train(candles, config, total_timesteps=args.timesteps)

    elif args.command == "suggest":
        result = suggest(config, candles)
        if result:
            print(json.dumps(result, indent=2))
        else:
            print("[drl] No suggestion available.")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
