"""The real backtester for the discovery cycle (B1b.3c) — the production ``Backtester`` that runs
a proposal through the *actual* engine, turning it into the per-bar return series the quant-analyst
judges.

The discovery loop (``discovery.run_discovery_cycle``) takes an injected ``Backtester``; this is the
real one. Per proposal it: builds the strategy from the vetted template + params, fetches the cell's
bars (via an injected ``bars_for``), runs the lifted engine (``run_backtest`` — same
`StrategyEngine → OMS(risk) → PaperBroker → CostModel` path as live, with ``now`` injected from
bar-time so backtest ≡ live), and derives the per-bar return series from the mark-to-market equity.

**Holdout isolation (TEST-3) is delegated to the ``bars_for`` boundary**, not enforced here: this
adapter is a pure pass-through of whatever ``bars_for`` returns and adds no holdout guard. The
production ``bars_for`` is the **no-ACL in-sample cold store** (R6), which is where the holdout is
physically unreachable; ``bars_for`` MUST return in-sample-only bars. The adapter does, however,
enforce that a cell has **enough** bars for the rigor gate (``>= 2 * cpcv.n_groups``), failing fast
with a cell-identifying error rather than letting the quant-analyst raise on a thin window.

Thin by construction: ``run_backtest`` wires the broker / OMS / risk / clock internally; this
adapter only supplies the strategy + bars + configs and reduces the result to returns. It bridges
the async engine into the synchronous ``Backtester`` protocol via ``asyncio.run`` (the research-box
discovery loop is synchronous). Money is ``Decimal`` on the equity curve; the returns are ``float``
— the statistics plane the quant-analyst works in — never fed back onto the money path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import cast

from alpha_core.backtest.runner import run_backtest
from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.helpers.config import load_rigor_config
from alpha_core.research.strategist import TEMPLATES, StrategyProposal
from alpha_core.risk.limits import RiskConfig

# Source the cell's in-sample bars for ``(market, window)``. CONTRACT: must return in-sample-only
# bars — holdout isolation (TEST-3/R6) is enforced at this boundary (the no-ACL cold store), not in
# the adapter, which is a pure pass-through.
BarsFor = Callable[[AssetClass, str], list[Bar]]


def _returns_from_equity(
    equity_curve: list[tuple[datetime, Decimal]], starting_cash: Decimal
) -> list[float]:
    """Per-bar simple returns from the mark-to-market equity curve (the same reduction the runner's
    stats use): ``(equity[i] - equity[i-1]) / equity[i-1]``. ``float`` for the statistics plane."""
    equity = [starting_cash + pnl for _, pnl in equity_curve] or [starting_cash]
    return [
        float((equity[i] - equity[i - 1]) / equity[i - 1])
        for i in range(1, len(equity))
        if equity[i - 1] != 0
    ]


class EngineBacktester:
    """A ``discovery.Backtester`` that runs a proposal through the real engine on cold-store
    in-sample bars. The configs (instruments / risk / cost) and the ``bars_for`` source are injected
    — production wires them from ``config/`` + the cold store; tests inject synthetic bars."""

    def __init__(
        self,
        *,
        bars_for: BarsFor,
        instruments: dict[str, InstrumentMeta],
        risk_config: RiskConfig,
        cost_config: dict[str, object],
        venue: Venue = Venue.NSE,
        starting_cash: Decimal = Decimal("1000000"),
        stress: bool = False,
    ) -> None:
        self._bars_for = bars_for
        self._instruments = instruments
        self._risk = risk_config
        self._cost = cost_config
        self._venue = venue
        self._starting_cash = starting_cash
        self._stress = stress
        # the rigor gate needs >= 2*n_groups observations; one fewer return than bars, so require
        # that many bars and fail fast on a thin cell (rather than crash later in `assess`).
        self._min_bars = 2 * load_rigor_config().cpcv.n_groups

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        """Build the proposal's strategy, run it through the engine on the cell's in-sample bars,
        and return the per-bar return series. Raises ``ValueError`` for an unknown template or a
        window with too few bars for the rigor gate."""
        template = TEMPLATES.get(proposal.template)
        if template is None:
            raise ValueError(f"unknown template {proposal.template!r}; known: {sorted(TEMPLATES)}")
        bars = self._bars_for(proposal.market, proposal.window)
        if len(bars) < self._min_bars:
            raise ValueError(
                f"too few in-sample bars for cell {proposal.market.value}/{proposal.window}: "
                f"{len(bars)} < {self._min_bars} (need >= 2*cpcv.n_groups for the rigor gate)"
            )
        # build() is typed `object` (it backs both registries — see StrategyTemplate); TEMPLATES is
        # the single-instrument registry, so its builds are Strategy instances.
        strategy = cast(Strategy, template.build(proposal.params))
        result = asyncio.run(
            run_backtest(
                bars=bars,
                strategy=strategy,
                instruments=self._instruments,
                risk_config=self._risk,
                cost_config=self._cost,
                venue=self._venue,
                starting_cash=self._starting_cash,
                stress=self._stress,
            )
        )
        return _returns_from_equity(result.equity_curve, self._starting_cash)
