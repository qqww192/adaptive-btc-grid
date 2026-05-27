"""
crypto.com Exchange API client — powered by CCXT.

Replaces the hand-rolled HMAC-SHA256 wrapper with CCXT's battle-tested
cryptocom connector. The public interface is identical to the original so
all callers (grid_trader, regime_classifier, gemini_optimizer) need no changes.

CCXT handles:
  - HMAC-SHA256 request signing
  - Automatic rate limiting (enableRateLimit=True)
  - Retry-safe network error wrapping
  - POST_ONLY limit order flag
"""

import os
from datetime import datetime
from typing import Any

import ccxt

CDX_TIMEOUT = 10_000  # CCXT uses milliseconds

_TIMEFRAME_MAP = {
    "1D": "1d", "4h": "4h", "1h": "1h",
    "30m": "30m", "15m": "15m", "5m": "5m", "1m": "1m",
}


class CDXError(Exception):
    """Raised when the crypto.com API returns an error."""
    pass


class CDXClient:
    def __init__(self):
        self._ex = ccxt.cryptocom({
            "apiKey":          os.environ["CDX_API_KEY"],
            "secret":          os.environ["CDX_API_SECRET"],
            "timeout":         CDX_TIMEOUT,
            "enableRateLimit": True,
        })

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _sym(instrument: str) -> str:
        """Convert BTC_USDT → BTC/USDT (CCXT unified symbol format)."""
        return instrument.replace("_", "/")

    def _call(self, fn, *args, **kwargs) -> Any:
        """Wrap every CCXT call and convert library exceptions to CDXError."""
        try:
            return fn(*args, **kwargs)
        except ccxt.NetworkError as e:
            raise CDXError(f"Network error: {e}") from e
        except ccxt.RateLimitExceeded as e:
            raise CDXError(f"Rate limited: {e}") from e
        except ccxt.ExchangeError as e:
            raise CDXError(f"Exchange error: {e}") from e

    @staticmethod
    def _norm_order(o: dict) -> dict:
        """
        Normalise a CCXT order dict to the format expected by grid_trader.py.

        CCXT status → grid_trader expectation:
          'closed'   → 'FILLED'
          'open'     → 'ACTIVE'
          'canceled' → 'CANCELED'
        """
        status_map = {
            "closed":    "FILLED",
            "open":      "ACTIVE",
            "canceled":  "CANCELED",
            "cancelled": "CANCELED",
            "expired":   "EXPIRED",
            "rejected":  "REJECTED",
        }
        fee = o.get("fee") or {}
        return {
            "order_id":            str(o.get("id", "")),
            "status":              status_map.get(o.get("status", ""), (o.get("status") or "").upper()),
            "side":                (o.get("side") or "").upper(),
            "avg_price":           float(o.get("average") or o.get("price") or 0),
            "cumulative_quantity": float(o.get("filled") or 0),
            "cumulative_fee":      float(fee.get("cost") or 0),
            "instrument_name":     (o.get("symbol") or "").replace("/", "_"),
            "price":               float(o.get("price") or 0),
            "quantity":            float(o.get("amount") or 0),
        }

    # ------------------------------------------------------------------ #
    #  Account                                                             #
    # ------------------------------------------------------------------ #

    def get_balance(self, currency: str = "USDT") -> float:
        """Return available balance for a given currency."""
        result = self._call(self._ex.fetch_balance)
        return float((result.get(currency) or {}).get("free") or 0)

    def get_portfolio_value_gbp(self, gbp_usd_rate: float = 1.27) -> dict:
        """
        Return total portfolio value in GBP, valuing the *total* balance
        (free + funds locked in open grid orders) for both currencies.

        Using `total` rather than `free` is essential: a running grid keeps
        most of its capital tied up in live limit orders (reported as `used`),
        so a free-only sum understates the real portfolio.

        Returns:
          total_gbp      — combined GBP value of all BTC and USDT held
          usdt_total     — USDT total balance (free + used)
          btc_total      — BTC total balance (free + used)
          btc_value_gbp  — GBP value of the BTC holding
          btc_price_usdt — BTC/USDT price used for BTC valuation
          usdt_free      — USDT free balance (transparency)
          btc_free       — BTC free balance (transparency)
        """
        balance    = self._call(self._ex.fetch_balance)
        usdt_node  = balance.get("USDT") or {}
        btc_node   = balance.get("BTC")  or {}
        usdt_total = float(usdt_node.get("total") or 0)
        btc_total  = float(btc_node.get("total")  or 0)
        usdt_free  = float(usdt_node.get("free")  or 0)
        btc_free   = float(btc_node.get("free")   or 0)

        btc_price = 0.0
        try:
            ticker    = self._call(self._ex.fetch_ticker, "BTC/USDT")
            btc_price = float(ticker.get("last") or ticker.get("close") or 0)
        except Exception:
            pass

        btc_in_usdt = btc_total * btc_price if btc_price > 0 else 0.0
        total_usdt  = usdt_total + btc_in_usdt
        total_gbp   = total_usdt / gbp_usd_rate if gbp_usd_rate else 0.0

        return {
            "total_gbp":      round(total_gbp, 2),
            "usdt_total":     round(usdt_total, 4),
            "btc_total":      round(btc_total, 8),
            "btc_value_gbp":  round((btc_in_usdt / gbp_usd_rate) if gbp_usd_rate else 0.0, 2),
            "btc_price_usdt": round(btc_price, 2),
            "usdt_free":      round(usdt_free, 4),
            "btc_free":       round(btc_free, 8),
        }

    # ------------------------------------------------------------------ #
    #  Market data                                                         #
    # ------------------------------------------------------------------ #

    def get_ticker(self, instrument: str = "BTC_USDT") -> dict:
        """Return the latest ticker for an instrument."""
        t = self._call(self._ex.fetch_ticker, self._sym(instrument))
        return {
            "price":    float(t.get("last") or t.get("close") or 0),
            "bid":      float(t.get("bid") or 0),
            "ask":      float(t.get("ask") or 0),
            "high_24h": float(t.get("high") or 0),
            "low_24h":  float(t.get("low") or 0),
            "volume":   float(t.get("baseVolume") or 0),
        }

    def get_order_book(self, instrument: str = "BTC_USDT", depth: int = 5) -> dict:
        """
        Return the top-N levels of the order book.

        Returns:
          best_bid  — highest bid price
          best_ask  — lowest ask price
          mid_price — (best_bid + best_ask) / 2
          spread    — best_ask - best_bid
          bids      — list of [price, qty] pairs (descending)
          asks      — list of [price, qty] pairs (ascending)
        """
        ob = self._call(self._ex.fetch_order_book, self._sym(instrument), depth)
        bids = ob.get("bids") or []
        asks = ob.get("asks") or []
        best_bid = float(bids[0][0]) if bids else 0.0
        best_ask = float(asks[0][0]) if asks else 0.0
        return {
            "best_bid":  best_bid,
            "best_ask":  best_ask,
            "mid_price": (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0,
            "spread":    best_ask - best_bid,
            "bids":      [[float(p), float(q)] for p, q in bids],
            "asks":      [[float(p), float(q)] for p, q in asks],
        }

    def get_candlesticks(
        self,
        instrument: str = "BTC_USDT",
        timeframe:  str = "1D",
        count:      int = 30,
    ) -> list[dict]:
        """
        Return OHLCV candles for regime classification.
        timeframe: 1m, 5m, 15m, 30m, 1h, 4h, 1D
        """
        tf    = _TIMEFRAME_MAP.get(timeframe, timeframe.lower())
        ohlcv = self._call(self._ex.fetch_ohlcv, self._sym(instrument), tf, limit=count)
        return [
            {
                "ts":     int(c[0]),
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]),
            }
            for c in ohlcv
        ]

    # ------------------------------------------------------------------ #
    #  Orders                                                              #
    # ------------------------------------------------------------------ #

    def place_limit_order(
        self,
        instrument: str,
        side:       str,
        price:      float,
        quantity:   float,
    ) -> str:
        """Place a POST_ONLY GTC limit order. Returns the order ID."""
        order = self._call(
            self._ex.create_order,
            self._sym(instrument),
            "limit",
            side.lower(),
            quantity,
            price,
            {"postOnly": True},
        )
        order_id = str(order.get("id") or "").strip()
        if not order_id:
            raise CDXError("API accepted order but returned no order_id")
        return order_id

    def cancel_order(self, instrument: str, order_id: str) -> None:
        """Cancel a single order by ID."""
        self._call(self._ex.cancel_order, order_id, self._sym(instrument))

    def cancel_all_orders(self, instrument: str) -> None:
        """Cancel every open order for an instrument (kill switch use)."""
        try:
            self._call(self._ex.cancel_all_orders, self._sym(instrument))
        except CDXError:
            # Fallback: cancel open orders one by one
            open_orders = self._call(self._ex.fetch_open_orders, self._sym(instrument))
            for o in open_orders:
                try:
                    self._call(self._ex.cancel_order, str(o["id"]), self._sym(instrument))
                except Exception as e:
                    # Log but continue — caller must NOT clear grid_state for this order
                    # since it may still be live on the exchange.
                    print(f"[cdx] WARNING: failed to cancel order {o.get('id')}: {e} "
                          f"— order may remain open, audit will catch it next run")

    def get_open_orders(self, instrument: str) -> list[dict]:
        """Return all currently open orders for an instrument."""
        orders = self._call(self._ex.fetch_open_orders, self._sym(instrument))
        return [self._norm_order(o) for o in orders]

    def get_order_history(self, instrument: str, limit: int = 20) -> list[dict]:
        """Return recent order history (filled and cancelled)."""
        try:
            orders = self._call(
                self._ex.fetch_closed_orders, self._sym(instrument), limit=limit
            )
        except CDXError:
            orders = self._call(
                self._ex.fetch_orders, self._sym(instrument), limit=limit
            )
        return [self._norm_order(o) for o in orders]

    def get_filled_orders_since(self, instrument: str, since_iso: str, limit: int = 200) -> list[dict]:
        """Return FILLED orders placed on or after since_iso (ISO timestamp string)."""
        since_ms = int(datetime.fromisoformat(since_iso).timestamp() * 1000)
        try:
            orders = self._call(
                self._ex.fetch_closed_orders, self._sym(instrument),
                since=since_ms, limit=limit
            )
        except CDXError:
            orders = self._call(
                self._ex.fetch_orders, self._sym(instrument),
                since=since_ms, limit=limit
            )
        return [o for o in [self._norm_order(x) for x in orders] if o.get("status") == "FILLED"]
