"""
scanner.py
Stock scanner — screens a universe of US and LSE stocks against
user-defined conditions from the Scanner sheet tab.

Uses yfinance only (zero Gemini calls).

Universes are fetched dynamically from Wikipedia:
  - US_SP500:     Full S&P 500 (~500 tickers)
  - US_NASDAQ100: Full NASDAQ-100 (~100 tickers)
  - US_ALL:       S&P 500 + NASDAQ-100 deduplicated (~530 tickers)
  - LSE_FTSE100:  Full FTSE 100 (~100 tickers)
  - LSE_FTSE250:  Full FTSE 250 (~250 tickers)
  - LSE_ALL:      FTSE 100 + FTSE 250 (~350 tickers)
  - ALL:          US_ALL + LSE_ALL (~880 tickers)
  - CUSTOM:AAPL,SHEL.L,...  — user-defined list

Falls back to hardcoded lists if Wikipedia fetch fails.

Two-pass filtering:
  Pass 1: Filter on .info-based metrics (P/E, market cap, growth, etc.)
  Pass 2: For survivors, fetch price history for computed metrics (RSI, DMA)
"""

import logging
import time
from typing import Any

import numpy as np
import pandas as pd
import yfinance as yf

log = logging.getLogger(__name__)

# ── Dynamic universe fetching from Wikipedia ─────────────────────────────────

_WIKI_SP500 = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
_WIKI_NASDAQ100 = "https://en.wikipedia.org/wiki/Nasdaq-100"
_WIKI_FTSE100 = "https://en.wikipedia.org/wiki/FTSE_100_Index"
_WIKI_FTSE250 = "https://en.wikipedia.org/wiki/FTSE_250_Index"


def _fetch_wiki_table(url: str, ticker_col: str, suffix: str = "",
                      table_index: int = 0) -> list[str]:
    """Fetch a ticker list from a Wikipedia table using pandas.read_html."""
    try:
        tables = pd.read_html(url)
        if table_index >= len(tables):
            log.warning("Wikipedia table index %d out of range for %s", table_index, url)
            return []
        df = tables[table_index]
        # Try exact column name first, then case-insensitive search
        col = None
        if ticker_col in df.columns:
            col = ticker_col
        else:
            for c in df.columns:
                if ticker_col.lower() in str(c).lower():
                    col = c
                    break
        if col is None:
            log.warning("Column '%s' not found in Wikipedia table from %s. Columns: %s",
                        ticker_col, url, list(df.columns))
            return []
        tickers = df[col].dropna().astype(str).str.strip().tolist()
        # Clean tickers: remove any with spaces or empty
        tickers = [t for t in tickers if t and " " not in t and t != "nan"]
        if suffix:
            tickers = [f"{t}{suffix}" if not t.endswith(suffix) else t for t in tickers]
        log.info("Fetched %d tickers from %s", len(tickers), url)
        return tickers
    except Exception as e:
        log.warning("Failed to fetch tickers from %s: %s", url, e)
        return []


def fetch_sp500() -> list[str]:
    """Fetch full S&P 500 constituent list."""
    tickers = _fetch_wiki_table(_WIKI_SP500, "Symbol")
    # Wikipedia S&P 500 uses dots (BRK.B) but yfinance uses dashes (BRK-B)
    tickers = [t.replace(".", "-") for t in tickers]
    return tickers if tickers else _FALLBACK_US


def fetch_nasdaq100() -> list[str]:
    """Fetch full NASDAQ-100 constituent list."""
    tickers = _fetch_wiki_table(_WIKI_NASDAQ100, "Ticker")
    return tickers if tickers else _FALLBACK_US[:100]


def fetch_ftse100() -> list[str]:
    """Fetch full FTSE 100 constituent list (with .L suffix for yfinance)."""
    # FTSE 100 Wikipedia table has "Ticker" column, sometimes as "EPIC"
    tickers = _fetch_wiki_table(_WIKI_FTSE100, "EPIC", suffix=".L")
    if not tickers:
        tickers = _fetch_wiki_table(_WIKI_FTSE100, "Ticker", suffix=".L")
    return tickers if tickers else _FALLBACK_LSE


def fetch_ftse250() -> list[str]:
    """Fetch full FTSE 250 constituent list (with .L suffix for yfinance)."""
    tickers = _fetch_wiki_table(_WIKI_FTSE250, "EPIC", suffix=".L")
    if not tickers:
        tickers = _fetch_wiki_table(_WIKI_FTSE250, "Ticker", suffix=".L")
    return tickers if tickers else _FALLBACK_LSE_250


# ── Hardcoded fallbacks (used when Wikipedia is unreachable) ─────────────────

_FALLBACK_US = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK-B", "LLY", "AVGO", "JPM",
    "TSLA", "UNH", "XOM", "V", "MA", "PG", "JNJ", "COST", "HD", "ABBV",
    "WMT", "NFLX", "KO", "MRK", "CRM", "BAC", "CVX", "PEP", "AMD", "TMO",
    "LIN", "ADBE", "ORCL", "ACN", "MCD", "CSCO", "ABT", "WFC", "GE", "DHR",
    "TXN", "PM", "QCOM", "INTU", "ISRG", "AMGN", "CAT", "NOW", "IBM", "GS",
    "RTX", "VZ", "AMAT", "NEE", "LOW", "SPGI", "BLK", "HON", "UNP", "DE",
    "PFE", "T", "ADP", "BKNG", "MDLZ", "SCHW", "CI", "LMT", "MMC", "SYK",
    "GILD", "CB", "ADI", "LRCX", "TMUS", "SO", "ZTS", "DUK", "MO", "CL",
    "REGN", "CME", "BSX", "PLD", "ICE", "PYPL", "SNPS", "CDNS", "EQIX", "AON",
    "MCK", "ITW", "SHW", "ABNB", "PGR", "KLAC", "APH", "MSI", "WELL", "CRWD",
    "EMR", "CTAS", "ORLY", "ADSK", "MAR", "GM", "F", "ROP", "MCHP", "NXPI",
    "HCA", "AJG", "TT", "PSA", "FTNT", "CARR", "OKE", "MNST", "AEP", "AIG",
    "FAST", "DLR", "SRE", "ALL", "CCI", "PAYX", "KMB", "MSCI", "GIS", "EA",
    "ROST", "TEL", "D", "HLT", "VRSK", "BK", "WEC", "YUM", "CTSH", "DXCM",
    "PCAR", "STZ", "EXC", "HPQ", "IDXX", "CPRT", "ODFL", "ON", "CSGP", "GEHC",
    "SQ", "SHOP", "MELI", "PLTR", "SNOW", "DDOG", "NET", "PANW", "COIN", "SOFI",
]

_FALLBACK_LSE = [
    "AZN.L", "SHEL.L", "HSBA.L", "ULVR.L", "BP.L", "GSK.L", "RIO.L", "BATS.L",
    "DGE.L", "LSEG.L", "REL.L", "NG.L", "CRH.L", "AHT.L", "AAL.L", "GLEN.L",
    "RKT.L", "CPG.L", "MNG.L", "PRU.L", "SSE.L", "VOD.L", "BA.L", "LLOY.L",
    "BARC.L", "STAN.L", "NWG.L", "ANTO.L", "ABF.L", "BT-A.L", "WPP.L", "III.L",
    "TSCO.L", "PSON.L", "INF.L", "IMB.L", "AV.L", "LGEN.L", "HLMA.L",
    "SVT.L", "UU.L", "ADM.L", "SN.L", "SDR.L", "RR.L", "EXPN.L", "IHG.L",
    "BNZL.L", "WEIR.L", "SMT.L", "SPX.L", "AUTO.L", "BRBY.L", "ENT.L",
    "SBRY.L", "KGF.L", "WTB.L", "MNDI.L", "SMIN.L", "BDEV.L", "JD.L",
    "HIK.L", "DARK.L", "FRAS.L", "ITRK.L", "CRDA.L", "BME.L", "PSN.L",
    "RTO.L", "HLN.L", "FLTR.L", "IAG.L", "EZJ.L", "PHNX.L", "DPLM.L",
]

_FALLBACK_LSE_250 = [
    "NXT.L", "ITV.L", "JET2.L", "GAW.L", "FOUR.L", "GNS.L",
    "PETS.L", "TRN.L", "FUTR.L", "GRG.L", "DOCS.L", "IPO.L",
]


# ── Universe resolution ──────────────────────────────────────────────────────

def _dedup(tickers: list[str]) -> list[str]:
    """Deduplicate preserving order."""
    seen = set()
    result = []
    for t in tickers:
        if t not in seen:
            seen.add(t)
            result.append(t)
    return result


def _resolve_single_universe(name: str) -> list[str]:
    """Resolve a single universe name to tickers."""
    name = name.strip().upper()
    resolvers = {
        "US_SP500":     fetch_sp500,
        "US_NASDAQ100": fetch_nasdaq100,
        "US_ALL":       lambda: _dedup(fetch_sp500() + fetch_nasdaq100()),
        "LSE_FTSE100":  fetch_ftse100,
        "LSE_FTSE250":  fetch_ftse250,
        "LSE_ALL":      lambda: _dedup(fetch_ftse100() + fetch_ftse250()),
        "ALL":          lambda: _dedup(fetch_sp500() + fetch_nasdaq100() +
                                       fetch_ftse100() + fetch_ftse250()),
    }
    resolver = resolvers.get(name)
    if resolver:
        return resolver()
    log.warning("Scanner: unknown universe '%s', skipping.", name)
    return []


def resolve_universe(universe_str: str) -> list[str]:
    """
    Resolve a universe string to a list of tickers.

    Supports:
      'US_SP500'              — full S&P 500 (~500)
      'US_NASDAQ100'          — full NASDAQ-100 (~100)
      'US_ALL'                — S&P 500 + NASDAQ-100 (~530)
      'LSE_FTSE100'           — full FTSE 100 (~100)
      'LSE_FTSE250'           — full FTSE 250 (~250)
      'LSE_ALL'               — FTSE 100 + FTSE 250 (~350)
      'ALL'                   — everything (~880)
      'US_SP500,LSE_FTSE100'  — comma-separated mix
      'CUSTOM:AAPL,SHEL.L'   — explicit ticker list
    """
    universe_str = universe_str.strip()
    if not universe_str:
        return _FALLBACK_US[:50]

    # Custom ticker list
    if universe_str.upper().startswith("CUSTOM:"):
        tickers = [t.strip() for t in universe_str[7:].split(",") if t.strip()]
        return tickers

    # Single or comma-separated universe names
    combined = []
    for name in universe_str.split(","):
        combined.extend(_resolve_single_universe(name))

    result = _dedup(combined)
    if not result:
        log.warning("Scanner: no tickers resolved, falling back to US top 50.")
        return _FALLBACK_US[:50]

    log.info("Scanner: resolved %d tickers from '%s'", len(result), universe_str)
    return result


# ── Metrics ──────────────────────────────────────────────────────────────────

# Pass-1 metrics: available from yf.Ticker.info (no history needed)
INFO_METRICS: dict[str, str] = {
    "pe_ratio":       "trailingPE",
    "forward_pe":     "forwardPE",
    "peg_ratio":      "pegRatio",
    "market_cap":     "marketCap",
    "revenue_growth": "revenueGrowth",
    "profit_margin":  "profitMargins",
    "debt_to_equity": "debtToEquity",
    "beta":           "beta",
    "dividend_yield": "dividendYield",
    "free_cash_flow": "freeCashflow",
}

# Pass-2 metrics: require price history to compute
COMPUTED_METRICS = {"rsi_14d", "price_vs_200dma", "price_vs_50dma", "52w_pct"}

ALL_METRICS = set(INFO_METRICS.keys()) | COMPUTED_METRICS


def _compute_rsi(prices: list[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


# ── Data fetching ────────────────────────────────────────────────────────────

def _fetch_info_batch(symbols: list[str]) -> dict[str, dict]:
    """Fetch .info for a batch of tickers."""
    result = {}
    if not symbols:
        return result
    try:
        tickers = yf.Tickers(" ".join(symbols))
        for sym in symbols:
            try:
                result[sym] = tickers.tickers[sym].info or {}
            except Exception:
                result[sym] = {}
    except Exception as e:
        log.warning("Scanner: batch info fetch failed: %s", e)
        for sym in symbols:
            result[sym] = {}
    return result


def _extract_info_metrics(info: dict) -> dict[str, float | None]:
    """Extract pass-1 metric values from a yfinance info dict."""
    data: dict[str, float | None] = {}
    for metric_key, yf_field in INFO_METRICS.items():
        val = info.get(yf_field)
        data[metric_key] = float(val) if val is not None else None
    # Also grab display fields
    data["_price"] = info.get("currentPrice") or info.get("regularMarketPrice")
    data["_name"] = info.get("shortName") or info.get("longName") or ""
    data["_market_cap_raw"] = info.get("marketCap")
    data["_52w_high"] = info.get("fiftyTwoWeekHigh")
    data["_52w_low"] = info.get("fiftyTwoWeekLow")
    data["_50dma"] = info.get("fiftyDayAverage")
    data["_200dma"] = info.get("twoHundredDayAverage")
    return data


def _compute_history_metrics(symbol: str) -> dict[str, float | None]:
    """Fetch price history and compute RSI, DMA%, 52w% for a single ticker."""
    result: dict[str, float | None] = {
        "rsi_14d": None, "price_vs_200dma": None,
        "price_vs_50dma": None, "52w_pct": None,
    }
    try:
        hist = yf.Ticker(symbol).history(period="1y")
        if hist.empty or len(hist) < 15:
            return result
        closes = hist["Close"].tolist()
        price = closes[-1]

        result["rsi_14d"] = _compute_rsi(closes)

        if len(closes) >= 200:
            dma200 = float(np.mean(closes[-200:]))
            result["price_vs_200dma"] = ((price - dma200) / dma200) * 100
        if len(closes) >= 50:
            dma50 = float(np.mean(closes[-50:]))
            result["price_vs_50dma"] = ((price - dma50) / dma50) * 100

        high_52w = max(closes)
        if high_52w > 0:
            result["52w_pct"] = ((price - high_52w) / high_52w) * 100

    except Exception as e:
        log.warning("Scanner: history fetch failed for %s: %s", symbol, e)
    return result


# ── Condition evaluation ─────────────────────────────────────────────────────

def _evaluate_condition(value: float | None, condition: str, threshold: str) -> bool:
    """Check if a single metric value passes a condition."""
    if value is None:
        return False
    condition = condition.strip().lower()
    if condition == "between":
        parts = [p.strip() for p in threshold.split(",")]
        if len(parts) != 2:
            return False
        try:
            lo, hi = float(parts[0]), float(parts[1])
        except ValueError:
            return False
        return lo <= value <= hi
    try:
        thresh = float(threshold)
    except ValueError:
        return False
    if condition in (">", "above"):
        return value > thresh
    if condition in (">=",):
        return value >= thresh
    if condition in ("<", "below"):
        return value < thresh
    if condition in ("<=",):
        return value <= thresh
    if condition in ("=", "=="):
        return value == thresh
    return False


def _passes_all(stock_data: dict, conditions: list[dict]) -> bool:
    """Return True if stock_data passes ALL conditions."""
    for cond in conditions:
        metric = cond["metric"]
        value = stock_data.get(metric)
        if not _evaluate_condition(value, cond["condition"], cond["value"]):
            return False
    return True


# ── Scoring ──────────────────────────────────────────────────────────────────

def _compute_match_score(stock_data: dict, conditions: list[dict]) -> int:
    """
    Simple quality score 0-100.
    Each condition contributes points based on how much margin the stock
    passes by. More conditions passed with wider margins = higher score.
    """
    if not conditions:
        return 50
    total = 0
    for cond in conditions:
        metric = cond["metric"]
        value = stock_data.get(metric)
        if value is None:
            continue
        c = cond["condition"].strip().lower()
        try:
            if c == "between":
                parts = [float(p.strip()) for p in cond["value"].split(",")]
                lo, hi = parts[0], parts[1]
                mid = (lo + hi) / 2
                span = (hi - lo) / 2 if hi != lo else 1
                closeness = 1 - min(abs(value - mid) / span, 1)
                total += closeness * 100
            else:
                thresh = float(cond["value"])
                if thresh == 0:
                    total += 50
                elif c in (">", ">=", "above"):
                    ratio = value / thresh
                    total += min(ratio * 50, 100)
                elif c in ("<", "<=", "below"):
                    ratio = thresh / value if value != 0 else 1
                    total += min(ratio * 50, 100)
        except (ValueError, ZeroDivisionError):
            total += 50

    return int(round(total / len(conditions)))


# ── Format helpers ───────────────────────────────────────────────────────────

def _fmt_market_cap(mc: float | None) -> str:
    if mc is None:
        return ""
    if mc >= 1e12:
        return f"${mc/1e12:.1f}T"
    if mc >= 1e9:
        return f"${mc/1e9:.1f}B"
    if mc >= 1e6:
        return f"${mc/1e6:.0f}M"
    return f"${mc:,.0f}"


def _fmt_pct(val: float | None) -> str:
    if val is None:
        return ""
    return f"{val*100:.1f}%" if abs(val) < 1 else f"{val:.1f}%"


def _fmt_num(val: float | None, decimals: int = 1) -> str:
    if val is None:
        return ""
    return f"{val:.{decimals}f}"


# ── Main scan ────────────────────────────────────────────────────────────────

def _split_groups_by_pass(
    groups: dict[str, list[dict]],
) -> tuple[dict[str, list[dict]], dict[str, list[dict]], bool]:
    """
    Split each group's conditions into pass-1 (info) and pass-2 (history).
    Returns (pass1_groups, pass2_groups, needs_pass2).
    """
    pass1: dict[str, list[dict]] = {}
    pass2: dict[str, list[dict]] = {}
    needs_pass2 = False
    for group_name, conds in groups.items():
        p1 = [c for c in conds if c["metric"] not in COMPUTED_METRICS]
        p2 = [c for c in conds if c["metric"] in COMPUTED_METRICS]
        if p1:
            pass1[group_name] = p1
        if p2:
            pass2[group_name] = p2
            needs_pass2 = True
    return pass1, pass2, needs_pass2


def _check_any_group(stock_data: dict, groups: dict[str, list[dict]]) -> list[str]:
    """
    Check if stock passes ANY group (OR logic between groups).
    Within each group, ALL conditions must pass (AND logic).
    Returns list of matched group names.
    """
    matched = []
    for group_name, conds in groups.items():
        if _passes_all(stock_data, conds):
            matched.append(group_name)
    return matched


def run_scan(groups: dict[str, list[dict]], universe: list[str]) -> list[dict]:
    """
    Run the stock scanner with grouped conditions.

    groups: dict mapping group name -> list of {metric, condition, value}
            Conditions within a group are AND'd.
            Different groups are OR'd (stock passes if it matches ANY group).
    universe: list of yfinance ticker strings

    Returns list of matching stocks with all metric data, sorted by score.
    """
    if not groups or not universe:
        return []

    # All conditions flat (for scoring)
    all_conditions = [c for conds in groups.values() for c in conds]
    total_conds = len(all_conditions)

    # Split into pass-1 and pass-2 per group
    pass1_groups, pass2_groups, needs_pass2 = _split_groups_by_pass(groups)

    log.info("Scanner: %d tickers, %d group(s) [%s], %d total conditions",
             len(universe), len(groups), ", ".join(groups.keys()), total_conds)

    # ── Pass 1: filter on .info metrics (OR across groups) ───────────────────
    survivors = []
    batch_size = 20
    for i in range(0, len(universe), batch_size):
        batch = universe[i:i + batch_size]
        if i > 0:
            time.sleep(0.5)  # gentle rate limiting for yfinance
        infos = _fetch_info_batch(batch)
        batch_pass = 0
        for sym in batch:
            info = infos.get(sym, {})
            if not info:
                continue
            data = _extract_info_metrics(info)
            data["_symbol"] = sym
            # Stock survives pass 1 if it matches ANY group's pass-1 conditions
            if not pass1_groups:
                # No info-based conditions — all survive pass 1
                data["_p1_groups"] = list(groups.keys())
                survivors.append(data)
                batch_pass += 1
            else:
                matched = _check_any_group(data, pass1_groups)
                if matched:
                    data["_p1_groups"] = matched
                    survivors.append(data)
                    batch_pass += 1
        log.info("  Batch %d-%d: %d/%d survived pass 1.",
                 i + 1, min(i + batch_size, len(universe)),
                 batch_pass, len(batch))

    log.info("Scanner pass 1 complete: %d/%d survived.", len(survivors), len(universe))

    # ── Pass 2: compute history metrics for survivors ────────────────────────
    if needs_pass2 and survivors:
        log.info("Scanner pass 2: computing history metrics for %d stocks...", len(survivors))
        final = []
        for stock in survivors:
            sym = stock["_symbol"]
            hist_data = _compute_history_metrics(sym)
            stock.update(hist_data)

            # For each group that passed pass 1, check if pass 2 also passes
            p1_matched = stock.get("_p1_groups", [])
            final_matched = []
            for gname in p1_matched:
                p2_conds = pass2_groups.get(gname, [])
                if not p2_conds or _passes_all(stock, p2_conds):
                    final_matched.append(gname)
            if final_matched:
                stock["_matched_groups"] = final_matched
                final.append(stock)
            time.sleep(0.3)
        log.info("Scanner pass 2 complete: %d/%d survived.", len(final), len(survivors))
        survivors = final
    else:
        # No pass 2 needed — carry pass 1 matches through
        for stock in survivors:
            stock["_matched_groups"] = stock.get("_p1_groups", [])

    # ── Score and sort ───────────────────────────────────────────────────────
    results = []
    for stock in survivors:
        price = stock.get("_price")
        if not price:
            continue
        matched_groups = stock.get("_matched_groups", [])
        # Score using the best-matching group's conditions
        best_score = 0
        for gname in matched_groups:
            group_conds = groups.get(gname, [])
            score = _compute_match_score(stock, group_conds)
            best_score = max(best_score, score)
        results.append({
            "symbol": stock["_symbol"],
            "name": stock.get("_name", ""),
            "price": f"{price:.2f}",
            "pe": _fmt_num(stock.get("pe_ratio")),
            "fwd_pe": _fmt_num(stock.get("forward_pe")),
            "mkt_cap": _fmt_market_cap(stock.get("_market_cap_raw")),
            "rev_growth": _fmt_pct(stock.get("revenue_growth")),
            "margin": _fmt_pct(stock.get("profit_margin")),
            "de": _fmt_num(stock.get("debt_to_equity")),
            "rsi": _fmt_num(stock.get("rsi_14d")),
            "vs_200dma": _fmt_num(stock.get("price_vs_200dma")),
            "vs_52w": _fmt_num(stock.get("52w_pct")),
            "beta": _fmt_num(stock.get("beta"), 2),
            "div_yield": _fmt_pct(stock.get("dividend_yield")),
            "score": best_score,
            "matched_groups": ", ".join(matched_groups),
        })

    results.sort(key=lambda r: r["score"], reverse=True)
    log.info("Scanner: %d matches found.", len(results))
    return results
