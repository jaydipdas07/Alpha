"""Decimal helpers (ADR 0002 — money is never a float).

Quantization to an instrument's tick/lot is enforced at order construction by the
OMS/risk against ``instruments.yaml``; these are the shared primitives.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal


def to_decimal(value: Decimal | int | str) -> Decimal:
    """Coerce to ``Decimal`` from Decimal/int/str only — float is rejected."""
    if isinstance(value, float):  # defensive; mypy forbids it at type level
        raise TypeError("money/quantity must be Decimal, int, or str — never float")
    return value if isinstance(value, Decimal) else Decimal(value)


def quantize_to_tick(price: Decimal, tick_size: Decimal) -> Decimal:
    """Round ``price`` to the nearest multiple of ``tick_size`` (half-up)."""
    if tick_size <= 0:
        raise ValueError("tick_size must be positive")
    return (price / tick_size).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick_size


def floor_to_lot(quantity: Decimal, lot_size: Decimal) -> Decimal:
    """Round ``quantity`` down to a whole multiple of ``lot_size``."""
    if lot_size <= 0:
        raise ValueError("lot_size must be positive")
    return (quantity / lot_size).quantize(Decimal(1), rounding=ROUND_DOWN) * lot_size


def is_lot_multiple(quantity: Decimal, lot_size: Decimal) -> bool:
    """True iff ``quantity`` is an exact whole multiple of ``lot_size``."""
    if lot_size <= 0:
        raise ValueError("lot_size must be positive")
    return quantity % lot_size == 0
