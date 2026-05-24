#!/usr/bin/env bash
# =============================================================================
# Oracle Cloud Always Free VM — one-time setup for the grid trading bot
#
# Run as ubuntu user:
#   chmod +x setup.sh && ./setup.sh
#
# Prerequisites:
#   - Fresh Ubuntu 22.04 ARM instance on Oracle Cloud
#   - SSH access configured
#   - Your .env file ready to upload (see .env.example)
# =============================================================================

set -euo pipefail

REPO_URL="https://github.com/qqww192/BTCTradeBot.git"
BOT_DIR="$HOME/BTCTradeBot"
PYTHON="python3.11"

echo ""
echo "======================================================"
echo "  Grid Trading Bot — Oracle VM Setup"
echo "======================================================"
echo ""

# ---- 1. System packages ----
echo "[1/7] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3.11 python3.11-venv python3-pip git curl \
    build-essential libssl-dev

# ---- 2. Clone repo ----
echo "[2/7] Cloning repository..."
if [ -d "$BOT_DIR" ]; then
    echo "      Repo already exists — pulling latest."
    cd "$BOT_DIR" && git pull
else
    git clone "$REPO_URL" "$BOT_DIR"
    cd "$BOT_DIR"
fi

# ---- 3. Python virtual environment ----
echo "[3/7] Creating virtual environment..."
cd "$BOT_DIR"
$PYTHON -m venv .venv
source .venv/bin/activate

# ---- 4. Install dependencies ----
echo "[4/7] Installing Python dependencies..."
pip install --upgrade pip -q
pip install -r requirements_trading.txt -q

# ---- 5. Environment variables ----
echo "[5/7] Setting up .env file..."
if [ ! -f "$BOT_DIR/.env" ]; then
    echo ""
    echo "  ⚠️  No .env file found."
    echo "  Upload your .env file to $BOT_DIR/.env before proceeding."
    echo "  Reference: .env.example in the repo."
    echo ""
    echo "  Quick copy from local machine:"
    echo "    scp .env ubuntu@<YOUR_VM_IP>:~/FinancialAdvisor/.env"
    echo ""
    read -p "  Press Enter once .env is in place, or Ctrl+C to abort..." _
fi

if [ ! -f "$BOT_DIR/.env" ]; then
    echo "ERROR: .env not found. Aborting."
    exit 1
fi

# Strip Windows CRLF line endings that break env var parsing
sed -i 's/\r$//' "$BOT_DIR/.env"
echo "      Stripped CRLF from .env (safe no-op if already Unix format)"
chmod 600 "$BOT_DIR/.env"
echo "      Locked .env permissions to owner-only (600)"

# ---- 6. Create data directories ----
echo "[6/7] Creating data directories..."
mkdir -p "$BOT_DIR/data"
touch "$BOT_DIR/data/trades.json"

# ---- 7. Install crontab ----
echo "[7/7] Installing crontab..."
VENV_PYTHON="$BOT_DIR/.venv/bin/python3"
LOG_DIR="$BOT_DIR/logs"
mkdir -p "$LOG_DIR"

# Build the crontab from oracle_setup/crontab.template
# replacing placeholders with actual paths
sed \
    -e "s|{BOT_DIR}|$BOT_DIR|g" \
    -e "s|{PYTHON}|$VENV_PYTHON|g" \
    -e "s|{LOG_DIR}|$LOG_DIR|g" \
    "$BOT_DIR/oracle_setup/crontab.template" | crontab -

echo ""
echo "======================================================"
echo "  ✅  Setup complete!"
echo "======================================================"
echo ""
echo "  Bot directory:  $BOT_DIR"
echo "  Virtual env:    $BOT_DIR/.venv"
echo "  Trade log:      $BOT_DIR/data/trades.json"
echo "  Cron log:       $LOG_DIR/"
echo ""
echo "  Active cron jobs:"
crontab -l
echo ""
echo "  ── Manual test commands ──"
echo "  Test API connection:"
echo "    cd $BOT_DIR && source .venv/bin/activate"
echo "    python3 -c \"from src.trading.cdx_client import CDXClient; c=CDXClient(); print(c.get_ticker())\""
echo ""
echo "  Run grid trader once (dry-run safe — reads but does not place orders without funds):"
echo "    python3 src/trading/grid_trader.py"
echo ""
echo "  Run regime classifier:"
echo "    python3 src/trading/regime_classifier.py"
echo ""
echo "  Send test daily report:"
echo "    python3 src/trading/daily_reporter.py"
echo ""
echo "  ⚠️  IMPORTANT: Before enabling real trading, verify:"
echo "    1. crypto.com API key has Trade permission enabled"
echo "    2. Kill switch is tested (set TOTAL_CAPITAL_GBP=1 and force a loss)"
echo "    3. Telegram bot is receiving messages"
echo "    4. Start with £50 for the first week — scale up once stable"
echo ""
