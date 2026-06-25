"""Core interfaces — the venue-agnostic contracts.

B0.7 defines the :class:`Strategy` ABC: the portable ``(bars, params) -> signals``
contract that both Phase-0 bake-off engines (the NautilusTrader shell and the lifted
Vega engine) wrap. A strategy is pure of broker SDKs, never places orders directly,
and never reads wall-clock time — it reacts to bar/tick timestamps, so backtest ≡
live. ``BrokerAdapter`` / ``DataFeed`` land with the Vega lift (B0.9).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence

from alpha_core.core.models import Bar, Signal, Tick


class Strategy(ABC):
    """Venue-agnostic strategy: consumes normalized data, emits abstract signals.

    The same ``Strategy`` runs in backtest and live. It returns ``Signal`` s for the
    risk gate; it never imports a broker SDK and never places orders directly.
    """

    @abstractmethod
    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        """React to a closed bar; return zero or more signals."""
        raise NotImplementedError

    @abstractmethod
    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        """React to a tick; return zero or more signals."""
        raise NotImplementedError
