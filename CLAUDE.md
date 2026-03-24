# T212 Portfolio Checker — Claude Code Context

## Project Overview
An automated pipeline that fetches a Trading 212 portfolio via their REST API, analyses each position using Claude (Anthropic API), and writes a formatted report to Google Drive as a Google Doc. The pipeline is scheduled via GitHub Actions and runs entirely in the cloud — no local machine required.

## Stack
- Language: Python 3.12
- Package manager: pip + `requirements.txt`
- Key libraries: `httpx` (API calls), `google-api-python-client` (Drive), `anthropic` (Claude API)
- Scheduler: GitHub Actions (cron)
- Secrets store: GitHub Actions Secrets (never `.env` files in repo)

## Common Commands
```bash
# Install dependencies
pip install -r requirements.txt

# Run locally (requires .env file — see docs/setup.md)
python src/main.py

# Run tests
pytest tests/

# Lint
ruff check src/
```

## Architecture
Full design lives in `docs/architecture.md`. Read before making structural changes.

## Skills
Task-specific procedures live in `skills/`. Read `skills/README.md` first.

## Constraints — Read These First
- **Never** commit API keys, tokens, or secrets — all secrets go in GitHub Actions Secrets or a local `.env` (gitignored)
- **Never** modify `.github/workflows/schedule.yml` without understanding the cron syntax — a bad schedule could spam the API
- Trading 212 API is rate-limited — always use the paginated fetch in `src/fetch_portfolio.py`, never call in a loop without the cursor
- Google Drive writes are append-only by default — do not overwrite the master sheet without confirmation
- The `.env` file is gitignored; `docs/setup.md` explains how to populate it

## What NOT to Touch
- `.github/workflows/` — only modify the schedule cron expression, nothing else, without reading the Actions docs
- `credentials/` — this folder is gitignored and holds the Google service account JSON locally; never commit it
