"""Core enums — string enums, so they serialize to their value in config, DB, and logs.

The minimal set the B0.7 strategy contract needs; the full enum set (`OrderState`,
`OptionRight`, `Settlement`, …) lands with the Vega lift (B0.9).
"""

from __future__ import annotations

from enum import StrEnum


class Side(StrEnum):
    """Direction of a signal/order/fill."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    """Venue-agnostic order type; adapters map to venue terms. B0.7 emits MARKET."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"


class AssetClass(StrEnum):
    """Tradable segments, in rollout order."""

    EQUITY = "EQUITY"
    CRYPTO = "CRYPTO"
    INDEX_OPTION = "INDEX_OPTION"


class Venue(StrEnum):
    """Execution/data venues. PAPER is always first; crypto live target is DELTA."""

    PAPER = "PAPER"
    NSE = "NSE"
    BSE = "BSE"
    BINANCE = "BINANCE"
    DELTA = "DELTA"
    BYBIT = "BYBIT"
