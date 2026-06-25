"""Position accounting — apply a fill to a position (the one place this math lives).

Pure function shared by the OMS (local/broker-truth books) and the PaperBroker
(its simulated venue books), so weighted-average and realized-P&L logic is defined
once. Handles opening, increasing, reducing, closing, and flipping past zero.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from alpha_core.core.enums import Side
from alpha_core.core.models import Fill, Position


def apply_fill(position: Position | None, fill: Fill, *, at: datetime | None = None) -> Position:
    """Return the new ``Position`` after ``fill`` is applied to ``position``."""
    signed = fill.quantity if fill.side is Side.BUY else -fill.quantity
    old_qty = position.quantity if position else Decimal(0)
    old_avg = (
        position.average_price if position and position.average_price is not None else fill.price
    )
    realized = position.realized_pnl if position else Decimal(0)
    new_qty = old_qty + signed

    if old_qty == 0 or (old_qty > 0) == (signed > 0):  # opening or increasing
        denom = abs(new_qty)
        new_avg = (
            (old_avg * abs(old_qty) + fill.price * fill.quantity) / denom if denom != 0 else None
        )
    else:  # reducing / closing / flipping
        closed = min(fill.quantity, abs(old_qty))
        direction = Decimal(1) if old_qty > 0 else Decimal(-1)
        realized += (fill.price - old_avg) * closed * direction
        if new_qty == 0:
            new_avg = None
        elif (new_qty > 0) == (old_qty > 0):  # still same side
            new_avg = old_avg
        else:  # flipped past zero: remainder opens at the fill price
            new_avg = fill.price

    return Position(
        symbol=fill.symbol,
        venue=fill.venue,
        asset_class=fill.asset_class,
        quantity=new_qty,
        average_price=new_avg,
        realized_pnl=realized,
        last_price=fill.price,
        updated_at=at or fill.ts,
    )
