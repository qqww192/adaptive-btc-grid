"""
market_data.py
Fetch live prices, market health metrics, and financial data via yfinance.

Market Health Scorecard:
  - Indices: S&P 500, NASDAQ, Dow, FTSE 100, Russell 2000
  - Volatility: VIX
  - Rates: US 10Y Yield
  - Currency: DXY (Dollar Index)
  - Commodities: Gold, Oil
  - Composite: Market Health Score (0-100)
"""

import logging
from typing import Any

import yfinance as yf

log = logging.getLogger(__name__)

# ── Ticker map ────────────────────────────────────────────────────────────────

MARKET_TICKERS = {
    # Indices
    "S&P 500":      "^GSPC",
    "NASDAQ":       "^IXIC",
    "Dow Jones":    "^DJI",
    "FTSE 100":     "^FTSE",
    "Russell 2000": "^RUT",
    # Volatility
    "VIX":          "^VIX",
    # Rates
    "US 10Y Yield": "^TNX",
    # Currency
    "DXY":          "DX-Y.NYB",
    # Commodities
    "Gold":         "GC=F",
    "Oil (WTI)":    "CL=F",
}


# ── Single stock data ─────────────────────────────────────────────────────────

def get_stock_info(symbol: str) -> dict[str, Any]:
    """Fetch current price and key financials for a single ticker."""
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info
        return {
            "symbol": symbol,
            "price": info.get("currentPrice") or info.get("regularMarketPrice", 0),
            "prev_close": info.get("previousClose", 0),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "market_cap": info.get("marketCap", 0),
            "52w_high": info.get("fiftyTwoWeekHigh", 0),
            "52w_low": info.get("fiftyTwoWeekLow", 0),
            "beta": info.get("beta"),
            "dividend_yield": info.get("dividendYield"),
            "revenue_growth": info.get("revenueGrowth"),
            "profit_margin": info.get("profitMargins"),
            "debt_to_equity": info.get("debtToEquity"),
            "free_cash_flow": info.get("freeCashflow"),
            "short_name": info.get("shortName", symbol),
            "50dma": info.get("fiftyDayAverage"),
            "200dma": info.get("twoHundredDayAverage"),
        }
    except Exception as e:
        log.warning("Failed to fetch yfinance data for %s: %s", symbol, e)
        return {"symbol": symbol, "price": 0}


def get_batch_prices(symbols: list[str]) -> dict[str, float]:
    """Fetch current prices for multiple tickers in one call."""
    if not symbols:
        return {}
    try:
        tickers = yf.Tickers(" ".join(symbols))
        prices = {}
        for sym in symbols:
            try:
                info = tickers.tickers[sym].info
                prices[sym] = info.get("currentPrice") or info.get("regularMarketPrice", 0)
            except Exception:
                prices[sym] = 0
        return prices
    except Exception as e:
        log.warning("Batch price fetch failed: %s", e)
        return {s: 0 for s in symbols}


# ── Market scorecard ──────────────────────────────────────────────────────────

def _fetch_ticker_data(symbol: str) -> dict:
    """Fetch price + change for a single market ticker."""
    try:
        t = yf.Ticker(symbol)
        info = t.info
        price = info.get("regularMarketPrice") or info.get("previousClose", 0)
        prev = info.get("previousClose", 0)
        change_pct = ((price - prev) / prev * 100) if prev else 0
        return {"price": price, "prev_close": prev, "change_pct": change_pct}
    except Exception as e:
        log.warning("Failed to fetch %s: %s", symbol, e)
        return {"price": 0, "prev_close": 0, "change_pct": 0}


def get_market_scorecard() -> list[dict]:
    """
    Fetch all market indicators and compute signals.
    Returns a list of dicts, one per indicator:
      {name, category, value, change_pct, signal}
    Plus a final "Market Health Score" row.
    """
    rows = []
    raw = {}

    for name, ticker in MARKET_TICKERS.items():
        data = _fetch_ticker_data(ticker)
        raw[name] = data

        # Determine category
        if name in ("S&P 500", "NASDAQ", "Dow Jones", "FTSE 100", "Russell 2000"):
            category = "Index"
        elif name == "VIX":
            category = "Volatility"
        elif name == "US 10Y Yield":
            category = "Rates"
        elif name == "DXY":
            category = "Currency"
        else:
            category = "Commodity"

        signal = _compute_signal(name, data)

        rows.append({
            "name": name,
            "category": category,
            "value": data["price"],
            "change_pct": data["change_pct"],
            "signal": signal,
        })

    # Compute composite health score
    health = _compute_health_score(raw)
    rows.append({
        "name": "Market Health",
        "category": "Composite",
        "value": health["score"],
        "change_pct": "",
        "signal": health["signal"],
    })

    return rows


def _compute_signal(name: str, data: dict) -> str:
    """Determine signal for a single indicator."""
    change = data.get("change_pct", 0)
    price = data.get("price", 0)

    if name == "VIX":
        if price < 15:
            return "Greedy"
        elif price < 20:
            return "Calm"
        elif price < 25:
            return "Nervous"
        elif price < 30:
            return "Fearful"
        else:
            return "Panic"

    if name == "US 10Y Yield":
        if price < 3.5:
            return "Easing"
        elif price < 4.5:
            return "Neutral"
        else:
            return "Tightening"

    if name == "DXY":
        if change > 0.5:
            return "Strong $"
        elif change < -0.5:
            return "Weak $"
        return "Stable"

    if name in ("Gold",):
        if change > 1:
            return "Risk-Off"
        elif change < -1:
            return "Risk-On"
        return "Neutral"

    if name in ("Oil (WTI)",):
        if change > 2:
            return "Inflation risk"
        elif change < -2:
            return "Demand concern"
        return "Stable"

    # Indices
    if change > 1:
        return "Strong Rally"
    elif change > 0.3:
        return "Bullish"
    elif change > -0.3:
        return "Flat"
    elif change > -1:
        return "Bearish"
    else:
        return "Sell-off"


def _compute_health_score(raw: dict) -> dict:
    """
    Composite market health score (0-100).

    Components (each 0-20 points):
      1. VIX level           → low = healthy, high = stressed
      2. S&P 500 momentum    → positive change = healthy
      3. Yield level          → moderate = healthy, extreme = stressed
      4. DXY stability        → stable = healthy
      5. Gold direction       → flat = healthy, surging = risk-off
    """
    score = 0

    # 1. VIX (0-20)
    vix = raw.get("VIX", {}).get("price", 20)
    if vix < 13:
        score += 20
    elif vix < 16:
        score += 17
    elif vix < 20:
        score += 14
    elif vix < 25:
        score += 10
    elif vix < 30:
        score += 5
    # 30+ = 0

    # 2. S&P 500 momentum (0-20)
    sp_change = raw.get("S&P 500", {}).get("change_pct", 0)
    if sp_change > 1:
        score += 20
    elif sp_change > 0.3:
        score += 16
    elif sp_change > -0.3:
        score += 12
    elif sp_change > -1:
        score += 6
    # worse = 0

    # 3. Yield (0-20) — sweet spot is 3.5-4.5%
    yld = raw.get("US 10Y Yield", {}).get("price", 4.0)
    if 3.5 <= yld <= 4.5:
        score += 18
    elif 3.0 <= yld <= 5.0:
        score += 12
    elif 2.5 <= yld <= 5.5:
        score += 6
    # extreme = 0

    # 4. DXY stability (0-20)
    dxy_change = abs(raw.get("DXY", {}).get("change_pct", 0))
    if dxy_change < 0.3:
        score += 20
    elif dxy_change < 0.6:
        score += 15
    elif dxy_change < 1.0:
        score += 10
    elif dxy_change < 1.5:
        score += 5
    # extreme = 0

    # 5. Gold (0-20) — surging gold = risk-off signal
    gold_change = raw.get("Gold", {}).get("change_pct", 0)
    if abs(gold_change) < 0.5:
        score += 18
    elif abs(gold_change) < 1.0:
        score += 14
    elif gold_change < 0:  # gold falling = risk-on, mildly positive
        score += 12
    elif gold_change < 2:
        score += 6
    # gold surging 2%+ = 0 (panic)

    # Map to signal
    if score >= 80:
        signal = "Strong"
    elif score >= 65:
        signal = "Healthy"
    elif score >= 50:
        signal = "Neutral"
    elif score >= 35:
        signal = "Cautious"
    elif score >= 20:
        signal = "Stressed"
    else:
        signal = "Danger"

    return {"score": score, "signal": signal}


# ── Alert evaluation ──────────────────────────────────────────────────────────

def evaluate_alert(symbol_or_indicator: str, metric: str, current_value: float | None = None) -> float:
    """
    Fetch current value for an alert rule.
    metric: 'price', 'pe', 'change%', 'vix', 'yield', etc.
    Returns the current value of the metric.
    """
    # Market-level indicators
    indicator_map = {
        "vix": "^VIX",
        "sp500": "^GSPC",
        "nasdaq": "^IXIC",
        "dxy": "DX-Y.NYB",
        "gold": "GC=F",
        "oil": "CL=F",
        "us10y": "^TNX",
    }

    ticker_sym = indicator_map.get(symbol_or_indicator.lower(), symbol_or_indicator)

    if metric.lower() in ("price", "vix", "yield", "level"):
        data = _fetch_ticker_data(ticker_sym)
        return data.get("price", 0)
    elif metric.lower() in ("change%", "change"):
        data = _fetch_ticker_data(ticker_sym)
        return data.get("change_pct", 0)
    elif metric.lower() == "pe":
        info = get_stock_info(ticker_sym)
        return info.get("pe_ratio") or 0
    elif metric.lower() == "52w_high_pct":
        info = get_stock_info(ticker_sym)
        price = info.get("price", 0)
        high = info.get("52w_high", 0)
        return ((price - high) / high * 100) if high else 0
    else:
        return current_value or 0


# ── Helpers for AI prompts ────────────────────────────────────────────────────

def build_stock_context(symbol: str) -> str:
    """Build a concise financial context string for the AI prompt."""
    info = get_stock_info(symbol)
    parts = [f"Price: ${info.get('price', 0):.2f}"]

    if info.get("pe_ratio"):
        parts.append(f"P/E: {info['pe_ratio']:.1f}")
    if info.get("forward_pe"):
        parts.append(f"Fwd P/E: {info['forward_pe']:.1f}")
    if info.get("market_cap"):
        mc = info["market_cap"]
        if mc >= 1e12:
            parts.append(f"MCap: ${mc/1e12:.1f}T")
        elif mc >= 1e9:
            parts.append(f"MCap: ${mc/1e9:.1f}B")
        else:
            parts.append(f"MCap: ${mc/1e6:.0f}M")
    if info.get("52w_high"):
        parts.append(f"52w: ${info['52w_low']:.2f}-${info['52w_high']:.2f}")
    if info.get("revenue_growth") is not None:
        parts.append(f"Rev Growth: {info['revenue_growth']*100:.1f}%")
    if info.get("profit_margin") is not None:
        parts.append(f"Margin: {info['profit_margin']*100:.1f}%")
    if info.get("debt_to_equity") is not None:
        parts.append(f"D/E: {info['debt_to_equity']:.1f}")
    if info.get("beta") is not None:
        parts.append(f"Beta: {info['beta']:.2f}")

    return " | ".join(parts)


def build_market_summary(scorecard: list[dict]) -> str:
    """Build a concise market context string from the scorecard for AI prompt."""
    lines = []
    for row in scorecard:
        if row["name"] == "Market Health":
            lines.append(f"Health Score: {row['value']}/100 ({row['signal']})")
        else:
            val = row["value"]
            chg = row["change_pct"]
            if isinstance(chg, (int, float)):
                lines.append(f"{row['name']}: {val:,.2f} ({chg:+.2f}%) [{row['signal']}]")
            else:
                lines.append(f"{row['name']}: {val} [{row['signal']}]")
    return "\n".join(lines)
