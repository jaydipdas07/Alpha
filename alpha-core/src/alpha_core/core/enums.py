"""Core enums — exactly as fixed by ADR 0002.

All are string enums so they serialize to their value in config, DB, and logs.
"""

from __future__ import annotations

from enum import StrEnum


class Side(StrEnum):
    """Direction of an order/fill/signal. Position direction is the sign of its
    quantity, not a ``Side``."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    """Venue-agnostic order type; adapters map to venue terms (e.g. Kite SL/SL-M)."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


class OrderState(StrEnum):
    """Order lifecycle state. Transition rules are owned by ADR 0003."""

    NEW = "NEW"
    PENDING = "PENDING"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"

    @property
    def is_terminal(self) -> bool:
        """FILLED/REJECTED/CANCELLED/EXPIRED are terminal."""
        return self in _TERMINAL_STATES


_TERMINAL_STATES: frozenset[OrderState] = frozenset(
    {
        OrderState.FILLED,
        OrderState.REJECTED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
    }
)


class AssetClass(StrEnum):
    """Tradable segments, in the §3 rollout order."""

    EQUITY = "EQUITY"
    CRYPTO = "CRYPTO"
    INDEX_OPTION = "INDEX_OPTION"


class Venue(StrEnum):
    """Execution venues. PAPER is always first; crypto live target is DELTA.

    ``BINANCE_SPOT`` is Binance's *spot* market as a distinct venue from its futures
    (``BINANCE``): same symbols, different books and cost regimes (``crypto_spot`` vs
    ``crypto_perp`` in costs.yaml), and the cold store keys series by venue — the basis
    track's spot leg must never collide with the perp series. Research/data venue today
    (no adapter in venues.yaml); an execution adapter joins it if a spot leg ever trades.
    """

    PAPER = "PAPER"
    NSE = "NSE"
    BSE = "BSE"
    BYBIT = "BYBIT"
    BINANCE = "BINANCE"
    BINANCE_SPOT = "BINANCE_SPOT"
    DELTA = "DELTA"


class OptionRight(StrEnum):
    """Call or put (ADR 0017). The contract's strike/expiry live on ``OptionContract``."""

    CALL = "CALL"
    PUT = "PUT"


class Settlement(StrEnum):
    """How an option settles at expiry (ADR 0017). NSE index options and Delta crypto
    options are CASH-settled; PHYSICAL is reserved for later equity-stock options."""

    CASH = "CASH"
    PHYSICAL = "PHYSICAL"
