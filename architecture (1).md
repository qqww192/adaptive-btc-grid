# Architecture

## System Overview

```
GitHub Actions (cron scheduler)
        │
        ▼
   src/main.py  ◄── orchestrates the three stages
        │
        ├─► src/fetch_portfolio.py
        │         │
        │         └─► Trading 212 REST API (paginated)
        │                   │
        │             list[Position]
        │
        ├─► src/analyse.py
        │         │
        │         └─► Anthropic API (claude-sonnet-4)
        │                   │   (+ web_search tool for live news)
        │             Markdown report string
        │
        └─► src/report.py
                  │
                  └─► Google Docs API
                            │
                      Appended to master Google Doc
```

## Key Design Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Scheduler | GitHub Actions | Free, cloud-hosted, no VPS required; runs when laptop is closed |
| HTTP client | `httpx` | Modern sync/async, better than `requests` for type hints |
| T212 pagination | Cursor-based | T212's documented pattern; avoids missed records on large portfolios |
| Report storage | Google Doc (append) | Persistent log of every run; easy to share; no extra DB needed |
| Auth (Google) | Service Account | Headless / non-interactive — works in CI without OAuth browser flow |
| Analysis model | claude-sonnet-4 | Best balance of speed and analytical depth for this use case |
| Web search in analysis | Enabled via tool | Allows Claude to fetch live news per ticker rather than relying on training data |

## Data Flow

1. **Fetch**: `fetch_portfolio.py` calls `GET /equity/portfolio` with `limit=50` and follows `nextCursor` until exhausted. Returns a list of position dicts (ticker, quantity, average price, current price, P&L).

2. **Analyse**: `analyse.py` formats positions as a Markdown table, constructs a structured prompt, and sends it to Claude with the `web_search` tool enabled. Claude may make several web search calls before returning the final text response. All `type: "text"` blocks are concatenated into the report string.

3. **Report**: `report.py` authenticates via service account, finds or creates the master Google Doc, and appends the new report with a horizontal divider at the end of the document's body.

## External Dependencies

| Service | Purpose | Criticality |
|---|---|---|
| Trading 212 API | Source of portfolio data | High — no fallback |
| Anthropic API | Portfolio analysis | High — no fallback |
| Google Docs API | Report delivery | Medium — report is logged to stdout even if Drive write fails |

## Known Limitations

- **T212 rate limits**: The API enforces rate limits (undocumented but roughly 1 req/sec). The current implementation is single-threaded and slow enough not to trigger them.
- **No historical price data**: Claude analyses based on current prices only; no charting or time-series analysis.
- **Google Doc size**: After many months of weekly runs, the doc will grow large. Consider archiving to a new doc annually.
- **UTC timing**: GitHub Actions cron runs in UTC — adjust your cron expression for your local timezone.
