# Oracle Cloud VM — Step-by-Step Deployment Guide

## Part 1: Provision the Oracle VM (one-time, ~30 min)

### 1.1 Create an Oracle Cloud account
1. Go to https://cloud.oracle.com and sign up with a credit card (not charged — always free).
2. Choose your home region carefully — pick the one nearest to you (e.g. UK South / London).
   You cannot change this later.

### 1.2 Create the Always Free ARM instance
1. Go to **Compute → Instances → Create Instance**.
2. Name it `trading-bot`.
3. Click **Edit** next to Image/Shape.
4. Click **Change Image** → Select **Ubuntu** → **22.04** → tick **Minimal** → Save.
5. Click **Change Shape** → **Ampere** → **VM.Standard.A1.Flex**.
   - OCPUs: 1 | Memory: 6 GB (well within the 4 OCPU / 24 GB free allowance).
6. Under **Add SSH keys**: upload your public key (`~/.ssh/id_rsa.pub` from your laptop).
7. Click **Create**.

> ⚠️ If you see "Out of capacity", try a different availability domain (AD-1, AD-2, AD-3)
> or retry the next day. Oracle frees up capacity regularly.

### 1.3 Open port 22 (SSH) in the firewall
Oracle Cloud blocks all inbound traffic by default.
1. Go to your instance → **Subnet** → **Security List** → **Add Ingress Rules**.
2. Source CIDR: `0.0.0.0/0`, Protocol: TCP, Destination port: 22.
3. Save.

### 1.4 SSH into the VM
```bash
ssh -i ~/.ssh/id_rsa ubuntu@<YOUR_VM_PUBLIC_IP>
```

---

## Part 2: Deploy the bot (~15 min)

### 2.1 Upload your .env file from your laptop
```bash
# On your LOCAL machine — fill in the IP first
scp .env ubuntu@<YOUR_VM_IP>:~/trading_bot_env_temp
```

### 2.2 Run the setup script
```bash
# On the VM
git clone https://github.com/qqww192/FinancialAdvisor.git
cd FinancialAdvisor
mv ~/trading_bot_env_temp .env   # move .env into repo root
chmod +x oracle_setup/setup.sh
./oracle_setup/setup.sh
```

The script will:
- Install Python 3.11 and dependencies
- Create the virtual environment
- Install the crontab (all scheduled jobs)
- Create the data/ directory

### 2.3 Verify crontab installed correctly
```bash
crontab -l
```
You should see 8 entries (1-min grid, 4-hourly regime, daily report, Saturday Optuna sweep, Sunday AI optimiser, daily git pull, Sunday log rotation, @reboot Telegram controller).

---

## Part 3: Test before live trading (~20 min)

### 3.1 Test API connection
```bash
cd ~/FinancialAdvisor
source .venv/bin/activate
python3 -c "
from src.trading.cdx_client import CDXClient
c = CDXClient()
ticker = c.get_ticker()
balance = c.get_balance('USDT')
print(f'BTC price: \${ticker[\"price\"]:,.0f}')
print(f'USDT balance: \${balance:.2f}')
"
```

### 3.2 Test regime classifier
```bash
python3 src/trading/regime_classifier.py
cat data/regime.json
```

### 3.3 Test kill switch (simulate a loss)
```bash
TOTAL_CAPITAL_GBP=10 KILL_SWITCH_PCT=0.10 python3 -c "
from src.trading.risk_manager import record_trade, is_kill_switch_active
record_trade(-1.01)   # force kill switch on £10 capital at 10%
print('Kill switch:', is_kill_switch_active())
"
# Reset afterwards:
python3 -c "
import json; from pathlib import Path
f = Path('data/weekly_state.json')
s = json.loads(f.read_text())
s['kill_switch_on'] = False; s['weekly_pnl_gbp'] = 0
f.write_text(json.dumps(s, indent=2))
print('Reset done.')
"
```

### 3.4 Send a test Telegram report
```bash
python3 src/trading/daily_reporter.py
```
Check your Telegram — you should receive the daily report immediately.

### 3.5 Dry-run the grid trader (no actual orders placed if balance = 0)
```bash
python3 src/trading/grid_trader.py
```

---

## Part 4: Go live

1. Confirm your crypto.com account has USDT funded (start with £50 equivalent).
2. Wait for the next 1-minute cron tick, or trigger manually:
   ```bash
   python3 src/trading/grid_trader.py
   ```
3. Verify orders are live:
   ```bash
   python3 -c "
   from src.trading.cdx_client import CDXClient
   c = CDXClient()
   orders = c.get_open_orders('BTC_USDT')
   print(f'{len(orders)} open orders')
   for o in orders[:3]:
       print(f\"  {o['side']} {o['quantity']} @ {o['price']}\")
   "
   ```
4. Check the live log:
   ```bash
   tail -f logs/grid_trader.log
   ```

---

## Ongoing maintenance

| Task | How |
|---|---|
| Change grid params | Edit `config/grid_params.json`, commit + push. VM pulls at 01:00 UTC. |
| View this week's P&L | `cat data/weekly_state.json` |
| View recent trades | `tail -20 data/trades.json` |
| Pause the bot | `crontab -r` (removes all jobs) |
| Resume the bot | `cd ~/FinancialAdvisor && ./oracle_setup/setup.sh` |
| SSH back in | `ssh ubuntu@<VM_IP>` |
| VM IP forgotten | Oracle Console → Compute → Instances → your instance |
