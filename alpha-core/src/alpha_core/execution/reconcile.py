"""Reconciliation — broker is the source of truth (ADR 0009).

Fetches the broker's orders/positions, diffs them against local state, and:
- **adopts** the recoverable cases (missed ack, missed fill / terminal, an order
  that never reached the venue) by moving local state toward broker truth, then
- **halts + alerts** on any irreconcilable mismatch (an order the broker has that
  we don't, local state ahead of the broker, an unexplained position drift) —
  never guesses.

Runs as a startup gate (trading blocked until clean), periodically, and on FSM
conflicts. The report carries the adoptions for the OMS to persist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from alpha_core.core.enums import OrderState, Venue
from alpha_core.core.interfaces import BrokerAdapter
from alpha_core.core.models import Order, Position
from alpha_core.observability.logging import get_logger
from alpha_core.observability.notify import LoggingNotifier, Notifier, Severity
from alpha_core.risk.manager import KillTrigger, RiskManager


class ReconcileStatus(StrEnum):
    CLEAN = "CLEAN"  # in sync, or fully recovered by adoption
    HALTED = "HALTED"  # irreconcilable mismatch


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    status: ReconcileStatus
    issues: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    adopted_orders: list[Order] = field(default_factory=list)
    adopted_positions: list[Position] = field(default_factory=list)


def _net(positions: list[Position]) -> dict[tuple[Venue, str], Decimal]:
    return {(p.venue, p.symbol): p.quantity for p in positions if p.quantity != 0}


class Reconciler:
    """Diffs broker truth against local state; adopts recoverable cases, halts else."""

    def __init__(
        self,
        *,
        adapter: BrokerAdapter,
        risk: RiskManager,
        notifier: Notifier | None = None,
    ) -> None:
        self._adapter = adapter
        self._risk = risk
        self._notifier = notifier or LoggingNotifier()
        self._log = get_logger("reconcile")

    async def reconcile(
        self, *, local_orders: list[Order], local_positions: list[Position]
    ) -> ReconcileReport:
        broker_orders = await self._adapter.get_orders()
        broker_positions = await self._adapter.get_positions()
        broker_by_id = {o.client_order_id: o for o in broker_orders}
        local_by_id = {o.client_order_id: o for o in local_orders}

        issues: list[str] = []
        recovered: list[str] = []
        adopted_orders: list[Order] = []

        # An order the broker has that we have no record of -> never guess.
        for unknown in broker_orders:
            if unknown.client_order_id not in local_by_id:
                issues.append(f"unknown order at broker: {unknown.client_order_id}")

        for lo in local_orders:
            bo = broker_by_id.get(lo.client_order_id)
            if lo.state is OrderState.PENDING and lo.venue_order_id is None:
                if bo is not None:  # missed ack -> adopt the broker order
                    adopted_orders.append(bo)
                    recovered.append(f"adopt missed ack: {lo.client_order_id}")
                else:  # never reached the venue -> cancel locally
                    adopted_orders.append(
                        lo.model_copy(
                            update={"state": OrderState.CANCELLED, "updated_at": datetime.now(UTC)}
                        )
                    )
                    recovered.append(f"never placed -> cancelled: {lo.client_order_id}")
            elif lo.state in (OrderState.OPEN, OrderState.PARTIALLY_FILLED):
                if bo is None:
                    issues.append(f"local live order missing at broker: {lo.client_order_id}")
                elif bo.filled_quantity > lo.filled_quantity or (
                    bo.state.is_terminal and not lo.state.is_terminal
                ):  # missed fill / terminal -> adopt broker truth
                    adopted_orders.append(bo)
                    recovered.append(f"adopt missed fill/terminal: {lo.client_order_id}")
            elif lo.state.is_terminal and bo is not None and not bo.state.is_terminal:
                issues.append(f"local ahead of broker: {lo.client_order_id}")

        # Positions: a mismatch is recoverable only if an adopted order on that
        # *same* instrument explains it -> adopt broker truth. Any mismatch on an
        # instrument no adopted order touches is unexplained drift -> halt (never
        # guess). A legitimate adoption on one name must not mask drift on another.
        adopted_positions: list[Position] = []
        bpos, lpos = _net(broker_positions), _net(local_positions)
        explained = {(o.venue, o.symbol) for o in adopted_orders}
        mismatched = [
            k for k in set(bpos) | set(lpos) if bpos.get(k, Decimal(0)) != lpos.get(k, Decimal(0))
        ]
        unexplained = [k for k in mismatched if k not in explained]
        if unexplained:
            issues += [
                f"position mismatch {k[1]}: broker {bpos.get(k, 0)} vs local {lpos.get(k, 0)}"
                for k in unexplained
            ]
        elif mismatched:
            # Adopt broker truth for the explained mismatch. The broker omits flat
            # positions, so for any local-nonzero symbol it no longer reports we must
            # synthesize an explicit zero: apply_reconciliation only *sets* positions
            # (never removes), so without this the stale local position would survive a
            # CLEAN reconcile and the bot would keep trading a phantom holding (M2).
            broker_keys = {(p.venue, p.symbol) for p in broker_positions}
            zeroed = [
                lp.model_copy(update={"quantity": Decimal(0)})
                for lp in local_positions
                if (lp.venue, lp.symbol) in mismatched and (lp.venue, lp.symbol) not in broker_keys
            ]
            adopted_positions = list(broker_positions) + zeroed
            recovered.append(f"adopt {len(mismatched)} position(s) from broker")

        if issues:
            self._risk.trip(KillTrigger.RECONCILIATION_MISMATCH)
            self._log.error("reconcile_mismatch", issues=issues, recovered=recovered)
            self._notifier.send(
                f"reconciliation mismatch: {'; '.join(issues)}", severity=Severity.CRITICAL
            )
            return ReconcileReport(ReconcileStatus.HALTED, issues, recovered)
        if recovered:
            self._log.info("reconcile_recovered", recovered=recovered)
        return ReconcileReport(
            ReconcileStatus.CLEAN,
            recovered=recovered,
            adopted_orders=adopted_orders,
            adopted_positions=adopted_positions,
        )

    async def startup_gate(
        self, *, local_orders: list[Order], local_positions: list[Position]
    ) -> bool:
        """Run the mandatory startup reconcile; True iff trading may proceed."""
        report = await self.reconcile(local_orders=local_orders, local_positions=local_positions)
        return report.status is ReconcileStatus.CLEAN
