# T212 Portfolio Checker

Automated pipeline that fetches your Trading 212 portfolio, analyses it with Google Gemini, and delivers:

- **Weekly report** → Google Drive (Google Doc, append-only log)
- **Daily news alerts** → Telegram (Tier 1 events only: rate surprises, crashes, fund incidents)

Runs entirely on **GitHub Actions** — no server or local machine required.

## How it works

```
GitHub Actions (cron)
    │
    ├─► Trading 212 API  ──► portfolio positions
    │
    ├─► Gemini API (with Search grounding)  ──► analysis / news scan
    │
    ├─► Google Docs API  ──► weekly report + daily audit log
    │
    └─► Telegram Bot API  ──► Tier 1 alerts only
```

## Setup

See **[docs/setup.md](docs/setup.md)** for the full step-by-step guide.

**Secrets required in GitHub Actions:**

| Secret | Source |
|---|---|
| `T212_API_KEY` | Trading 212 → Settings → API |
| `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com) → Get API Key |
| `GOOGLE_SA_JSON` | Google Cloud → Service Account → JSON key |
| `GOOGLE_DOC_ID` | Google Doc URL (set after first run) |
| `TELEGRAM_BOT_TOKEN` | Telegram → @BotFather → /newbot |
| `TELEGRAM_CHAT_ID` | Telegram → @userinfobot |

## Schedules

| Job | Default schedule | File |
|---|---|---|
| Weekly portfolio report | Every Monday 07:00 UTC | `.github/workflows/schedule.yml` |
| Daily news alert | Mon–Fri 07:00 UTC | `.github/workflows/daily-alert.yml` |

Both schedules are a single cron expression — edit to suit your timezone.

## Alert tiers

| Tier | Score | Action |
|---|---|---|
| 🔴 Tier 1 | 8–10 | Telegram alert sent immediately |
| 🟡 Tier 2 | 5–7 | Logged to Google Doc, surfaces in weekly report |
| ⚪ Tier 3 | 1–4 | Briefly logged, no further action |
