"""Shared session helpers used by both the paper loop and the backtester.

Keeping these in one place is how the backtest and live/paper paths stay
identical around the strategy (ADR 0001: backtest and live share code).
"""

from __future__ import annotations

from decimal import Decimal

from alpha_core.core.enums import OrderType, Side
from alpha_core.core.models import Bar, Order, Position, Signal, Tick
from alpha_core.execution.oms import OMS
from alpha_core.observability.notify import LoggingNotifier, Notifier, Severity
from alpha_core.risk.manager import KillTrigger


def quote_from_bar(bar: Bar) -> Tick:  # pragma: no cover - paper/backtest loop glue (B0.9d)
    """A synthetic tick at the bar close (LTP-only; the cost model adds the spread)."""
    return Tick(
        symbol=bar.symbol,
        venue=bar.venue,
        asset_class=bar.asset_class,
        ts=bar.start + bar.interval,
        last_price=bar.close,
    )


def pos_price(pos: Position) -> Decimal:  # pragma: no cover - used only by square_off (below)
    return pos.last_price or pos.average_price or Decimal("0")


async def handle_kill(
    oms: OMS, trigger: KillTrigger | None, *, notifier: Notifier | None = None, drain: bool = True
) -> list[Order]:
    """Execute the kill-switch cleanup (ADR 0006): **cancel working orders →
    flatten positions → alert**.

    This is what makes a halt safe. ``square_off`` cannot do it — it places via
    the risk gate, which rejects everything while halted — so on a kill the loop
    must call this instead. ``flatten_all`` bypasses the halt because flattening
    *is* the risk system acting. Both steps are idempotent, so re-running the
    handler is safe.

    ``drain`` pulls the resulting cancel/fill events inline — correct for the
    bounded paper/backtest loops. The **live** loop passes ``drain=False`` because
    its long-lived ``consume_events`` task handles the events (a live
    ``order_events`` stream blocks, so draining here would hang).
    """
    note = notifier or LoggingNotifier()
    note.send(
        f"KILL SWITCH ({trigger}): cancelling working orders and flattening",
        severity=Severity.CRITICAL,
    )
    # Persist the latched halt FIRST, so it survives a restart even if the book is flat
    # and the flatten below produces no fill (ADR 0006 — the halt is durable, M1).
    await oms.persist_halt(trigger)
    await oms.cancel_all_working()
    if drain:
        await oms.drain_events()
    flattened = await oms.flatten_all()
    if drain:
        await oms.drain_events()
    return flattened


# scheduled intraday square-off: wired + exercised by the scheduler/Phase-3 tests
async def square_off(oms: OMS, *, drain: bool = True) -> None:  # pragma: no cover
    """Flatten open positions with opposing market orders (intraday square-off).

    ``drain=True`` (paper/backtest — the bounded loops) pulls the resulting fills
    inline. In the live push model pass ``drain=False``: ``order_events`` is a
    continuous stream that never returns, so draining here would hang — the
    long-running ``consume_events`` task books the fills instead (mirrors
    ``handle_kill(drain=...)``).
    """
    now = oms.now()  # OMS clock (bar time in backtest) keeps backtest≡live (G23)
    for pos in list(oms.positions):
        if pos.quantity == 0:
            continue
        side = Side.SELL if pos.quantity > 0 else Side.BUY
        flatten = Signal(
            strategy_id="square_off",
            symbol=pos.symbol,
            asset_class=pos.asset_class,
            side=side,
            quantity=abs(pos.quantity),
            order_type=OrderType.MARKET,
            created_at=now,
            reason="end-of-session square-off",
        )
        await oms.submit_signal(flatten, reference_price=pos_price(pos))
        if drain:
            await oms.drain_events()
