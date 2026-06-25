"""Interface + error-taxonomy tests (ADR 0004)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from alpha_core.core.enums import AssetClass, Side, Venue
from alpha_core.core.errors import (
    AuthError,
    BrokerError,
    BrokerRateLimited,
    InsufficientFunds,
    InvalidOrder,
    OrderRejected,
    TerminalBrokerError,
    TransientBrokerError,
    UnknownOrder,
)
from alpha_core.core.interfaces import (
    BrokerAdapter,
    BrokerEventKind,
    BrokerOrderEvent,
    DataFeed,
    Strategy,
)
from alpha_core.core.models import Fill

TRANSIENT: list[type[BrokerError]] = [BrokerRateLimited]
TERMINAL: list[type[BrokerError]] = [
    OrderRejected,
    InvalidOrder,
    InsufficientFunds,
    UnknownOrder,
    AuthError,
]


@pytest.mark.parametrize("abc", [BrokerAdapter, DataFeed, Strategy])
def test_abcs_not_instantiable(abc: type) -> None:
    with pytest.raises(TypeError):
        abc()


def test_transient_vs_terminal_hierarchy() -> None:
    for exc in TRANSIENT:
        assert issubclass(exc, TransientBrokerError)
        assert issubclass(exc, BrokerError)
        assert not issubclass(exc, TerminalBrokerError)
    for exc in TERMINAL:
        assert issubclass(exc, TerminalBrokerError)
        assert issubclass(exc, BrokerError)
        assert not issubclass(exc, TransientBrokerError)


def test_rate_limited_carries_retry_after() -> None:
    err = BrokerRateLimited("429", retry_after=2.5)
    assert err.retry_after == 2.5


def test_order_rejected_carries_reason() -> None:
    assert OrderRejected("nope", reason="RMS").reason == "RMS"


def test_broker_order_event_shape() -> None:
    fill = Fill(
        fill_id="f1",
        client_order_id="vega-1",
        symbol="X",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        side=Side.BUY,
        quantity=Decimal("1"),
        price=Decimal("100"),
        ts=datetime(2026, 1, 1, tzinfo=UTC),
    )
    ev = BrokerOrderEvent(kind=BrokerEventKind.FILL, client_order_id="vega-1", fill=fill)
    assert ev.kind is BrokerEventKind.FILL
    assert ev.fill is fill
    with pytest.raises(AttributeError):  # frozen dataclass
        ev.reason = "x"  # type: ignore[misc]


class _NoopStrategy(Strategy):
    def on_bar(self, bar: object) -> list[object]:  # type: ignore[override]
        return []

    def on_tick(self, tick: object) -> list[object]:  # type: ignore[override]
        return []


def test_concrete_subclass_instantiable() -> None:
    assert _NoopStrategy().on_bar(object()) == []
