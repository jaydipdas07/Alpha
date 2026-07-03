"""Risk manager — non-bypassable pre-trade checks + kill switch (ADR 0006).

The only path from a signal/order to the OMS. The halt gate is always checked
first; there is no flag that disables risk. The 10-step pre-trade order is
applied deterministically; the first failure rejects the order with a reason.
The kill switch latches and requires a deliberate manual re-arm (fail closed).
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from alpha_core.core.enums import AssetClass, Side, Venue
from alpha_core.core.models import BookGreeks, OptionContract, Order, Position
from alpha_core.execution.instruments import InstrumentRegistry
from alpha_core.risk.limits import RiskConfig


class KillTrigger(StrEnum):
    DAILY_LOSS = "daily_loss"
    CONSECUTIVE_ERRORS = "consecutive_errors"
    FEED_STALE = "feed_stale"
    RECONCILIATION_MISMATCH = "reconciliation_mismatch"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class RiskDecision:
    approved: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class WorkingExposure:
    """A non-terminal order's unfilled reservation (ADR 0014).

    The gate counts these alongside filled positions so concurrent strategies
    can't both pass a check against a book missing each other's in-flight orders.
    """

    venue: Venue
    symbol: str
    asset_class: AssetClass
    side: Side
    remaining_qty: Decimal
    price: Decimal


def _pos_price(p: Position) -> Decimal:
    return p.last_price or p.average_price or Decimal(0)


class RiskManager:
    """Hand-written, non-bypassable risk gate."""

    def __init__(
        self,
        config: RiskConfig,
        *,
        require_stop: bool = False,
        instruments: InstrumentRegistry | None = None,
    ) -> None:
        self._cfg = config
        self._require_stop = require_stop
        # The instrument master (ADR 0017): lets the margin estimate recognize option
        # symbols (strike/lot/right) and the Greeks step demand its context. None =
        # no options in this deployment's universe (equity/crypto books unchanged).
        self._instruments = instruments
        self._halted = False
        self._halt_trigger: KillTrigger | None = None
        # Monotonic counter incremented on each *transition* into a halt. Lets a
        # consumer (the worker's flatten orchestration) act exactly once per distinct
        # trip: re-checks while already halted don't bump it, but a fresh trip after a
        # re-arm does — so a re-trip's flatten is never skipped (ADR 0006).
        self._halt_generation = 0
        self._order_times: deque[datetime] = deque()
        self._consecutive_errors = 0

    # --- kill switch -----------------------------------------------------------

    @property
    def base_capital(self) -> Decimal:
        return self._cfg.base_capital

    @property
    def is_halted(self) -> bool:
        return self._halted

    @property
    def halt_trigger(self) -> KillTrigger | None:
        return self._halt_trigger

    @property
    def halt_generation(self) -> int:
        """The current halt's generation (bumped on each not-halted -> halted transition)."""
        return self._halt_generation

    def trip(self, trigger: KillTrigger) -> None:
        """Latch the kill switch. Cleanup (cancel/flatten) is orchestrated by the
        app/OMS; this records the halt so every subsequent check rejects. A trip while
        already halted (e.g. the daily-loss re-check on every mark) keeps the same
        generation; only a transition into a halt bumps it."""
        if not self._halted:
            self._halt_generation += 1
        self._halted = True
        self._halt_trigger = trigger

    def rearm(self) -> None:
        """Deliberate manual re-arm (fail-closed default requires this). The generation
        is NOT reset — a subsequent trip bumps it, so the next halt is seen as fresh."""
        self._halted = False
        self._halt_trigger = None
        self._consecutive_errors = 0

    def restore(self, *, halted: bool, trigger: KillTrigger | None) -> None:
        """Restore a persisted halt state on restart (ADR 0006 restart-safety).

        A latched daily-loss/kill halt must survive a crash — the bot does not
        silently resume after restart; it stays halted until a manual re-arm. A
        restored halt bumps the generation so the worker flattens it once on boot.
        """
        if halted and not self._halted:
            self._halt_generation += 1
        self._halted = halted
        self._halt_trigger = trigger if halted else None

    def record_error(self) -> None:
        """A reject/error; >= configured consecutive count trips the switch."""
        self._consecutive_errors += 1
        if self._consecutive_errors >= self._cfg.kill_switch.triggers.consecutive_errors:
            self.trip(KillTrigger.CONSECUTIVE_ERRORS)

    def record_success(self) -> None:
        self._consecutive_errors = 0

    def update_pnl(self, *, realized: Decimal, unrealized: Decimal) -> None:
        """Trip on a daily-loss breach (intraday realized + unrealized P&L)."""
        loss_limit = -self._cfg.cap(self._cfg.limits.max_daily_loss_halt)
        if (realized + unrealized) <= loss_limit:
            self.trip(KillTrigger.DAILY_LOSS)

    # --- pre-trade checks ------------------------------------------------------

    def check_order(
        self,
        order: Order,
        *,
        reference_price: Decimal,
        positions: list[Position],
        now: datetime,
        working: Sequence[WorkingExposure] = (),
        book_greeks_after: BookGreeks | None = None,
    ) -> RiskDecision:
        """Run the 10-step pre-trade order; first failure rejects.

        ``working`` is the set of in-flight (non-terminal) orders *other than this
        one* — their unfilled remainder is reserved into the effective book so the
        exposure checks (5-9) count commitments that haven't filled yet (ADR 0014).
        """
        lim = self._cfg.limits

        # 1. Halt gate (non-bypassable) — always first.
        if self._halted:
            return RiskDecision(False, f"halted ({self._halt_trigger})")

        # 2. Well-formed / known instrument (price must be available).
        if reference_price <= 0:
            return RiskDecision(False, "no reference price")

        # Effective book = filled positions ⊕ in-flight reservations (ADR 0014).
        positions = self._effective_book(positions, working, now)
        order_notional = order.quantity * reference_price
        by_symbol = {(p.venue, p.symbol): p for p in positions}
        key = (order.venue, order.symbol)
        cur = by_symbol.get(key)
        cur_qty = cur.quantity if cur else Decimal(0)
        signed = order.quantity if order.side is Side.BUY else -order.quantity
        new_qty = cur_qty + signed
        # A REDUCTION shrinks the position toward zero on the SAME side. A flip
        # THROUGH zero (SELL 149 against +75) is an entry in disguise — without the
        # sign guard it would skip exposure/margin/Greeks (steps 5-9b) and leave an
        # unchecked naked short (review #161 BLOCKER).
        reducing = abs(new_qty) < abs(cur_qty) and new_qty * cur_qty >= 0

        # 3. Fat-finger hard cap.
        if order_notional > self._cfg.cap(lim.max_order_value):
            return RiskDecision(False, "max_order_value exceeded")

        # 4. Throttle.
        cutoff = now - timedelta(seconds=60)
        while self._order_times and self._order_times[0] <= cutoff:
            self._order_times.popleft()
        if len(self._order_times) >= lim.max_orders_per_minute:
            return RiskDecision(False, "max_orders_per_minute exceeded")

        # De-risking orders skip exposure checks (5-8) but never the halt gate.
        if not reducing:
            # 5. Per-instrument cap.
            if abs(new_qty) * reference_price > self._cfg.cap(lim.max_position_per_instrument):
                return RiskDecision(False, "max_position_per_instrument exceeded")
            # 6. Concurrent positions.
            opening_new = cur_qty == 0 and new_qty != 0
            open_count = sum(1 for p in positions if p.quantity != 0)
            if opening_new and open_count >= lim.max_concurrent_positions:
                return RiskDecision(False, "max_concurrent_positions exceeded")
            # 7. Per-segment exposure.
            seg = self._segment_notional(
                order.asset_class, by_symbol, key, new_qty, reference_price
            )
            if seg > self._cfg.cap(lim.per_segment_exposure_cap):
                return RiskDecision(False, "per_segment_exposure_cap exceeded")
            # 8. Gross exposure.
            gross = self._gross_notional(by_symbol, key, new_qty, reference_price)
            if gross > self._cfg.cap(lim.max_gross_exposure):
                return RiskDecision(False, "max_gross_exposure exceeded")

            # 9. Margin / funds — estimated required margin for the resulting book
            # must fit base_capital (leverage per segment; real SPAN refined live).
            if self._cfg.margin.enabled:
                required = self._required_margin(
                    by_symbol, key, order.asset_class, new_qty, reference_price
                )
                if required > self._cfg.base_capital:
                    return RiskDecision(False, "insufficient margin")

            # 9b. Options Greeks caps (ADR 0017) — only when the portfolio config
            # sets them AND this order is a registry-known option. FAIL-CLOSED: caps
            # configured but no computed book context supplied => reject (a cap the
            # gate cannot verify is a cap, not a suggestion). Equity/crypto orders
            # and cap-less configs pass straight through.
            greeks_decision = self._check_option_greeks(order, book_greeks_after)
            if greeks_decision is not None:
                return greeks_decision

        # 10. Stop-loss discipline.
        if self._require_stop:
            if order.stop_price is None:
                return RiskDecision(False, "missing protective stop")
            worst = abs(reference_price - order.stop_price) * order.quantity
            if worst > self._cfg.cap(lim.max_loss_per_trade):
                return RiskDecision(False, "max_loss_per_trade exceeded")

        self._order_times.append(now)
        return RiskDecision(True)

    # --- exposure helpers ------------------------------------------------------

    def _effective_book(
        self,
        positions: list[Position],
        working: Sequence[WorkingExposure],
        now: datetime,
    ) -> list[Position]:
        """Merge in-flight reservations into the filled book (ADR 0014).

        Each working order adds its signed unfilled remainder to its symbol; a
        symbol's price keeps the filled position's price when one exists, else the
        order's price (an approximation that is fine for a conservative cap). Zero
        net rows are dropped. With no working orders this reproduces ``positions``,
        so the no-multi-strategy path is unchanged.
        """
        if not working:
            return positions
        qty: dict[tuple[Venue, str], Decimal] = {}
        price: dict[tuple[Venue, str], Decimal] = {}
        ac: dict[tuple[Venue, str], AssetClass] = {}
        ts: dict[tuple[Venue, str], datetime] = {}
        for p in positions:
            k = (p.venue, p.symbol)
            qty[k] = qty.get(k, Decimal(0)) + p.quantity
            price[k] = _pos_price(p)
            ac[k] = p.asset_class
            ts[k] = p.updated_at
        for w in working:
            k = (w.venue, w.symbol)
            signed = w.remaining_qty if w.side is Side.BUY else -w.remaining_qty
            qty[k] = qty.get(k, Decimal(0)) + signed
            price.setdefault(k, w.price)
            ac.setdefault(k, w.asset_class)
            ts.setdefault(k, now)
        return [
            Position(
                symbol=k[1],
                venue=k[0],
                asset_class=ac[k],
                quantity=q,
                average_price=price[k],
                last_price=price[k],
                updated_at=ts[k],
            )
            for k, q in qty.items()
            if q != 0
        ]

    def _resulting_notional(
        self,
        p: Position | None,
        key: tuple[Venue, str],
        order_key: tuple[Venue, str],
        new_qty: Decimal,
        reference_price: Decimal,
    ) -> Decimal:
        if key == order_key:
            return abs(new_qty) * reference_price
        assert p is not None
        return abs(p.quantity) * _pos_price(p)

    def _gross_notional(
        self,
        by_symbol: dict[tuple[Venue, str], Position],
        order_key: tuple[Venue, str],
        new_qty: Decimal,
        reference_price: Decimal,
    ) -> Decimal:
        keys = set(by_symbol) | {order_key}
        return sum(
            (
                self._resulting_notional(by_symbol.get(k), k, order_key, new_qty, reference_price)
                for k in keys
            ),
            Decimal(0),
        )

    def _spec_option(self, symbol: str) -> OptionContract | None:
        """The registry's option metadata for ``symbol`` (None = not a known option)."""
        if self._instruments is None or not self._instruments.is_known(symbol):
            return None
        return self._instruments.get(symbol).option

    def _check_option_greeks(
        self, order: Order, book_greeks_after: BookGreeks | None
    ) -> RiskDecision | None:
        """Step 9b (see check_order). Returns a rejection, or None to continue."""
        po = self._cfg.portfolio
        if po is None or (po.max_net_delta is None and po.max_net_vega is None):
            return None  # no Greeks caps configured
        is_option = (
            order.asset_class is AssetClass.INDEX_OPTION
            or self._spec_option(order.symbol) is not None
        )
        if not is_option:
            return None  # not an option order — the caps don't bind here
        # NB keying off the ASSET CLASS as well as the registry: caps configured +
        # an option order the gate cannot resolve (no registry wired, or a symbol
        # missing from it) must still fail CLOSED, never silently open (review #161).
        if book_greeks_after is None:
            return RiskDecision(
                False, "greeks caps configured but no book greeks supplied (fail closed)"
            )
        if po.max_net_delta is not None and abs(book_greeks_after.net_delta) > po.max_net_delta:
            return RiskDecision(False, "max_net_delta exceeded")
        if po.max_net_vega is not None and abs(book_greeks_after.net_vega) > po.max_net_vega:
            return RiskDecision(False, "max_net_vega exceeded")
        return None

    def _leverage(self, asset_class: AssetClass) -> Decimal:
        """Per-segment leverage for the margin estimate (default 1x = full notional)."""
        return self._cfg.margin.leverage.get(asset_class.value.lower(), Decimal(1))

    def _required_margin(
        self,
        by_symbol: dict[tuple[Venue, str], Position],
        order_key: tuple[Venue, str],
        order_ac: AssetClass,
        new_qty: Decimal,
        reference_price: Decimal,
    ) -> Decimal:
        """Estimated margin to hold the resulting book: option rows use the options
        model (long = premium; short = premium + ``options_short_pct`` x strike
        notional — ADR 0017); everything else = notional / segment leverage."""
        total = Decimal(0)
        keys = set(by_symbol) | {order_key}
        for k in keys:
            p = by_symbol.get(k)
            contract = self._spec_option(k[1])
            if contract is not None:
                signed = new_qty if k == order_key else (p.quantity if p else Decimal(0))
                premium = reference_price if k == order_key else _pos_price(p) if p else Decimal(0)
                required = abs(signed) * premium * contract.multiplier  # the premium leg
                if signed < 0:  # short: add the strike-notional charge (SPAN stand-in)
                    required += (
                        self._cfg.margin.options_short_pct
                        * contract.strike
                        * abs(signed)
                        * contract.multiplier
                    )
                total += required
                continue
            ac = order_ac if k == order_key else (p.asset_class if p else order_ac)
            notional = self._resulting_notional(p, k, order_key, new_qty, reference_price)
            total += notional / self._leverage(ac)
        return total

    def _segment_notional(
        self,
        segment: AssetClass,
        by_symbol: dict[tuple[Venue, str], Position],
        order_key: tuple[Venue, str],
        new_qty: Decimal,
        reference_price: Decimal,
    ) -> Decimal:
        total = Decimal(0)
        keys = set(by_symbol) | {order_key}
        for k in keys:
            p = by_symbol.get(k)
            ac = segment if k == order_key else (p.asset_class if p else segment)
            if ac is segment:
                total += self._resulting_notional(p, k, order_key, new_qty, reference_price)
        return total
