"""Core interfaces — the venue-agnostic contracts (ADR 0001/0004).

`BrokerAdapter`, `DataFeed`, and `Strategy` are the three ABCs the core depends
on; `PaperBroker`/`KiteAdapter`/`CryptoAdapter` satisfy `BrokerAdapter`
identically (ADR 0004). The strategy engine never imports a broker SDK.

The abstract methods default to `NotImplementedError`; concrete adapters
(`PaperBroker`/`KiteAdapter`/`CcxtAdapter`) implement them.

Naming note: ``BrokerOrderEvent`` here is the *normalized broker-callback
payload* (the FILL/REJECT/CANCEL/EXPIRE notification an adapter emits). It feeds
the order FSM, whose own driving-event enum is ``order_fsm.OrderEvent`` (ACK is
delivered as the return of ``place_order``, not via this stream).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from alpha_core.core.models import Bar, Fill, Order, Position, Signal, TargetExposure, Tick


class BrokerEventKind(StrEnum):
    """The kinds of order event an adapter streams (ACK is the place_order return)."""

    FILL = "FILL"
    REJECT = "REJECT"
    CANCEL = "CANCEL"
    EXPIRE = "EXPIRE"


@dataclass(frozen=True, slots=True)
class BrokerOrderEvent:
    """A normalized order-callback payload that feeds the FSM (ADR 0004).

    The adapter maps every venue's native payload to this; the core never sees a
    venue-native object. ``fill`` is present iff ``kind is FILL``.
    """

    kind: BrokerEventKind
    client_order_id: str
    venue_order_id: str | None = None
    reason: str | None = None
    fill: Fill | None = None


class BrokerAdapter(ABC):
    """One contract for every venue (ADR 0004). All venue I/O is async."""

    @abstractmethod
    async def place_order(self, order: Order) -> str:
        """Submit `order`; return the `venue_order_id` once accepted (= FSM ACK).
        Idempotent on `order.client_order_id`. Raises OrderRejected | InvalidOrder
        | InsufficientFunds | AuthError (terminal); TransientBrokerError."""
        raise NotImplementedError

    @abstractmethod
    async def cancel(self, client_order_id: str) -> None:
        """Request cancel. Idempotent: cancelling a terminal/absent order is a
        safe no-op. Raises UnknownOrder | AuthError (terminal); Transient."""
        raise NotImplementedError

    @abstractmethod
    async def modify(
        self,
        client_order_id: str,
        *,
        quantity: Decimal | None = None,
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
    ) -> None:
        """Modify a working order in place; at least one field required.
        Raises UnknownOrder | InvalidOrder | AuthError (terminal); Transient."""
        raise NotImplementedError

    @abstractmethod
    async def get_positions(self) -> list[Position]:
        """Broker's current positions (broker truth). Raises AuthError; Transient."""
        raise NotImplementedError

    @abstractmethod
    async def get_orders(self) -> list[Order]:
        """All orders the broker knows this session. Raises AuthError; Transient."""
        raise NotImplementedError

    @abstractmethod
    async def find_order_id(self, client_order_id: str) -> str | None:
        """Query the venue for an order previously tagged with `client_order_id`
        (Kite `tag` / venue idempotency key); return its `venue_order_id` if the
        venue has it, else None. Makes placement idempotent across a lost ack —
        the dedup-by-query hook so a retry never duplicates (ADR 0004).
        Raises AuthError; Transient."""
        raise NotImplementedError

    @abstractmethod
    def subscribe_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        """Yield normalized ticks. The adapter handles reconnect + resubscribe
        internally and surfaces a terminal failure only after the reconnect
        budget is exhausted."""
        raise NotImplementedError

    @abstractmethod
    def order_events(self) -> AsyncIterator[BrokerOrderEvent]:
        """Yield normalized order events (FILL/REJECT/CANCEL/EXPIRE) for the FSM."""
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release any network resources (websockets/HTTP connectors). Default
        no-op; adapters holding a live SDK session (e.g. CCXT) override this and
        callers should `await adapter.aclose()` on shutdown."""
        return None


class DataFeed(ABC):
    """A source of normalized market data (replay or live)."""

    @abstractmethod
    def stream_ticks(self, symbols: Sequence[str]) -> AsyncIterator[Tick]:
        """Yield normalized ticks for `symbols`."""
        raise NotImplementedError

    @abstractmethod
    def stream_bars(self, symbols: Sequence[str]) -> AsyncIterator[Bar]:
        """Yield normalized bars for `symbols`."""
        raise NotImplementedError


class Strategy(ABC):
    """Venue-agnostic strategy: consumes normalized data, emits abstract signals.

    The same `Strategy` runs in backtest and live (ADR 0001). It never imports a
    broker SDK and never places orders directly — it returns `Signal`s for the
    risk gate.
    """

    @abstractmethod
    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        """React to a closed bar; return zero or more signals."""
        raise NotImplementedError

    @abstractmethod
    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        """React to a tick; return zero or more signals."""
        raise NotImplementedError


class PortfolioConstructor(ABC):
    """Pure cross-sectional allocator (scaling design PD/PB).

    Ranks the bar-close cross-section of scored `Signal`s against the reconciled
    book and deployable capital, emitting desired `TargetExposure`s; the execution
    layer diffs current→target into orders (target-portfolio rebalancing). Like a
    `Strategy`, an instance carries its own config, so `construct` is a pure
    function of its inputs — identical in backtest and live. Implemented in
    `portfolio/construction.py` (Phase R / R4).
    """

    @abstractmethod
    def construct(
        self,
        signals: Sequence[Signal],
        positions: Sequence[Position],
        capital: Decimal,
    ) -> Sequence[TargetExposure]:
        """Map scored signals + book + capital to target exposures (weights)."""
        raise NotImplementedError
