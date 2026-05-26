"""
News & sentiment feed — gives the AI a view of the market beyond price candles.

Aggregates three free sources, each degrading gracefully if unavailable:
  1. Fear & Greed Index    — alternative.me (no key)
  2. cryptocurrency.cv     — BTC headlines, 200+ sources (no key)
  3. RSS headlines          — CoinDesk / Cointelegraph (no key)

Design principles (mirrors ai_advisor.py):
  - Every HTTP call has a short timeout so it never blocks the cron window.
  - The aggregate is cached in data/news_cache.json (4h TTL, aligned to the
    4-hourly regime job) — the 1-min grid loop reads the cache, never the network.
  - Any source failing is swallowed; total failure returns None and callers
    fall back to their existing price-only behaviour.
"""

import json
import os
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import httpx

ROOT       = Path(__file__).resolve().parents[2]
CACHE_FILE = ROOT / "data" / "news_cache.json"

HTTP_TIMEOUT = 8        # seconds — hard ceiling per source
CACHE_TTL    = 14400    # 4 hours — matches the regime classifier cadence
MAX_HEADLINES = 6

# Some feeds/APIs reject the default httpx user-agent — present a browser-like one.
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BTCTradeBot/1.0; +grid)"}

FEAR_GREED_URL = "https://api.alternative.me/fng/?limit=1"
CRYPTOCURRENCYCV_NEWS_URL = "https://cryptocurrency.cv/api/news"
RSS_FEEDS = [
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss/tag/bitcoin",
]


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


# ------------------------------------------------------------------ #
#  Individual sources                                                  #
# ------------------------------------------------------------------ #

def get_fear_greed() -> Optional[dict]:
    """Return {value:int, classification:str} or None. No API key required."""
    try:
        r = httpx.get(FEAR_GREED_URL, timeout=HTTP_TIMEOUT, headers=HTTP_HEADERS)
        r.raise_for_status()
        item = (r.json().get("data") or [])[0]
        return {
            "value":          int(item["value"]),
            "classification": item.get("value_classification", ""),
        }
    except Exception as e:
        print(f"[news] Fear & Greed fetch failed: {e}")
        return None


def get_cryptocurrencycv_headlines(limit: int = MAX_HEADLINES) -> list[dict]:
    """BTC headlines from cryptocurrency.cv — free, no API key required."""
    try:
        r = httpx.get(
            CRYPTOCURRENCYCV_NEWS_URL,
            params={"q": "bitcoin", "limit": limit},
            headers=HTTP_HEADERS,
            timeout=HTTP_TIMEOUT,
        )
        r.raise_for_status()
        articles = r.json().get("articles", [])
        return [
            {"title": a["title"], "source": a.get("source", "cryptocurrency.cv")}
            for a in articles if a.get("title")
        ][:limit]
    except Exception as e:
        print(f"[news] cryptocurrency.cv fetch failed: {e}")
        return []


def get_rss_headlines(limit: int = MAX_HEADLINES) -> list[dict]:
    """Return recent crypto headlines from public RSS feeds. No API key required."""
    out = []
    for url in RSS_FEEDS:
        try:
            r = httpx.get(url, timeout=HTTP_TIMEOUT, follow_redirects=True, headers=HTTP_HEADERS)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            # RSS 2.0: channel/item/title
            for item in root.iter("item"):
                title_el = item.find("title")
                if title_el is not None and title_el.text:
                    out.append({"title": title_el.text.strip(), "source": "rss"})
                if len(out) >= limit:
                    break
        except Exception as e:
            print(f"[news] RSS fetch failed ({url}): {e}")
        if len(out) >= limit:
            break
    return out[:limit]


# ------------------------------------------------------------------ #
#  Aggregate (cached)                                                  #
# ------------------------------------------------------------------ #

def _read_cache(max_age: int) -> Optional[dict]:
    try:
        if not CACHE_FILE.exists():
            return None
        cached = json.loads(CACHE_FILE.read_text())
        if time.time() - cached.get("fetched_ts", 0) < max_age:
            return cached
    except Exception:
        pass
    return None


def get_market_sentiment(max_age: int = CACHE_TTL, force: bool = False) -> Optional[dict]:
    """
    Aggregate all available sources into a compact sentiment summary, cached to
    data/news_cache.json. Returns None only if no source produced anything.

    Shape:
      {
        "fear_greed": int | None,
        "fg_class":   str,
        "headlines":  [ "title", ... ],   # up to MAX_HEADLINES
        "fetched_at": ISO-8601 str,
        "fetched_ts": float,
      }
    """
    if not force:
        cached = _read_cache(max_age)
        if cached is not None:
            return cached

    fg        = get_fear_greed()
    headlines = get_cryptocurrencycv_headlines() + get_rss_headlines()

    if fg is None and not headlines:
        # Nothing available — let callers fall back to price-only behaviour.
        return None

    titles = []
    for h in headlines:
        t = h.get("title", "").strip()
        if t and t not in titles:
            titles.append(t)
    titles = titles[:MAX_HEADLINES]

    summary = {
        "fear_greed": fg["value"] if fg else None,
        "fg_class":   fg["classification"] if fg else "",
        "headlines":  titles,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
        "fetched_ts": time.time(),
    }
    try:
        _atomic_write(CACHE_FILE, json.dumps(summary, indent=2))
    except Exception as e:
        print(f"[news] Failed to write cache: {e}")
    return summary


def load_cached_sentiment() -> Optional[dict]:
    """Read the last cached sentiment without hitting the network (1-min loop use)."""
    return _read_cache(CACHE_TTL)


def summarise(sentiment: Optional[dict], max_headlines: int = 3) -> str:
    """One-line human summary for prompts and Telegram. Empty string if no data."""
    if not sentiment:
        return ""
    parts = []
    if sentiment.get("fear_greed") is not None:
        parts.append(f"Fear&Greed {sentiment['fear_greed']} ({sentiment.get('fg_class', '')})")
    heads = sentiment.get("headlines") or []
    if heads:
        parts.append("Headlines: " + " | ".join(heads[:max_headlines]))
    return ". ".join(parts)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    s = get_market_sentiment(force=True)
    if s is None:
        print("[news] No sentiment data available from any source.")
    else:
        print(json.dumps(s, indent=2))
        print("\nSummary:", summarise(s))
