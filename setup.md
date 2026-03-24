# Setup Guide

This document walks you through every step needed to get the portfolio checker running, from API keys to the first scheduled report in Google Drive.

---

## Prerequisites

- Python 3.12+ installed locally
- A [Trading 212](https://www.trading212.com) account with live or paper trading enabled
- An [Anthropic](https://console.anthropic.com) account with API access
- A [Google Cloud](https://console.cloud.google.com) account (free tier is fine)
- A GitHub account

---

## Step 1 — Get Your Trading 212 API Key

1. Log in to Trading 212
2. Go to **Settings → API** (bottom-left)
3. Click **Generate API key**
4. Copy the key — you won't see it again

> **Note:** By default the pipeline targets the **live** endpoint (`live.trading212.com`). To test against paper trading, change `T212_BASE_URL` in `src/fetch_portfolio.py` to `demo.trading212.com`.

---

## Step 2 — Get Your Anthropic API Key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Navigate to **API Keys → Create Key**
3. Copy the key

---

## Step 3 — Set Up Google Drive Access (Service Account)

This is the most involved step. You only do it once.

### 3a — Create a Google Cloud Project

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Click **New Project** → name it `t212-portfolio-checker`
3. Select the project

### 3b — Enable APIs

In the project, go to **APIs & Services → Enable APIs**. Enable both:
- **Google Drive API**
- **Google Docs API**

### 3c — Create a Service Account

1. Go to **IAM & Admin → Service Accounts → Create Service Account**
2. Name: `t212-checker`
3. Click **Create and Continue** → skip role assignment → **Done**
4. Click the service account → **Keys → Add Key → JSON**
5. Download the JSON file

### 3d — Share Your Google Doc with the Service Account

1. Open (or create) the Google Doc you want reports written to
2. Click **Share** and add the service account email (looks like `t212-checker@your-project.iam.gserviceaccount.com`)
3. Give it **Editor** access
4. Copy the Doc ID from the URL: `https://docs.google.com/document/d/**THIS_PART**/edit`

---

## Step 4 — Configure Local Environment

```bash
cd t212-portfolio-checker
cp .env.template .env
```

Open `.env` and fill in:

```
T212_API_KEY=...
ANTHROPIC_API_KEY=...
GOOGLE_SA_JSON=...     ← paste the full JSON from Step 3c as one line
GOOGLE_DOC_ID=...      ← from Step 3d
```

To paste the JSON as one line: `cat your-key.json | tr -d '\n'`

---

## Step 5 — Test Locally

```bash
pip install -r requirements.txt
cd src
python main.py
```

You should see the pipeline log and, after ~30 seconds, a new section in your Google Doc.

---

## Step 6 — Deploy to GitHub Actions

### 6a — Create a Private GitHub Repository

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/t212-portfolio-checker.git
git push -u origin main
```

> The repo **must** be private — it will be linked to your API keys via Secrets.

### 6b — Add GitHub Secrets

Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**.

Add each of these:

| Secret name | Value |
|---|---|
| `T212_API_KEY` | Your Trading 212 API key |
| `ANTHROPIC_API_KEY` | Your Anthropic API key |
| `GOOGLE_SA_JSON` | The full service account JSON (one line) |
| `GOOGLE_DOC_ID` | Your Google Doc ID |
| `GOOGLE_DRIVE_FOLDER_ID` | (Optional) Drive folder ID |

### 6c — Set Your Schedule

Open `.github/workflows/schedule.yml` and edit the cron expression:

```yaml
schedule:
  - cron: '0 7 * * 1'   # Every Monday at 07:00 UTC
```

Cron syntax: `minute hour day month weekday` — all times are UTC.

Use [crontab.guru](https://crontab.guru) to build your expression interactively.

### 6d — Trigger a Test Run

Go to your repo → **Actions → Portfolio Check → Run workflow**.

Watch the logs — if everything is green, you're done.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `403` from T212 API | Wrong API key or wrong endpoint (live vs demo) | Check `T212_API_KEY` and `T212_BASE_URL` |
| `EnvironmentError: GOOGLE_SA_JSON not set` | Secret not added or misnamed | Check GitHub Secrets names exactly |
| Google Doc not updating | Service account not shared on the doc | Re-share the doc with the SA email |
| `anthropic.AuthenticationError` | Wrong Anthropic key | Regenerate at console.anthropic.com |
| Actions not triggering on schedule | GitHub Actions schedules can be delayed by up to ~15 min | Wait; or use manual trigger to test |
