"""The real backtester adapter (B1b.3c) — runs a proposal through the actual engine -> returns."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.research.discovery import run_discovery_cycle
from alpha_core.research.engine_backtester import EngineBacktester
from alpha_core.research.proposal_ledger import ProposalLedger
from alpha_core.research.quant_analyst import QuantAnalyst, Verdict
from alpha_core.research.strategist import RandomProposer, Strategist, StrategyProposal
from alpha_core.risk.limits import RiskConfig

SYMBOL = "NSE:RELIANCE"
START = datetime(2026, 6, 15, 3, 45, tzinfo=UTC)
ONE_MIN = timedelta(minutes=1)

COST_CONFIG: dict[str, object] = {
    "slippage": {
        "equity": {"type": "bps", "value": 5},
        "crypto": {"type": "bps", "value": 8},
        "index_option": {"type": "ticks", "value": 2},
        "default_spread": {"equity": 0.0005, "crypto": 0.0008, "index_option_ticks": 1},
        "stress_multiplier": 2,
    },
    "segments": {
        "equity_intraday": {"brokerage": {"pct": 0.0003, "flat": 20, "mode": "min"}},
        "crypto": {"trading_fee": {"pct": 0.001, "side": "both"}},
    },
}


def _risk() -> RiskConfig:
    return RiskConfig.model_validate(
        {
            "base_capital": "100000",
            "limits": {
                "max_gross_exposure": "1.00",
                "max_position_per_instrument": "0.50",
                "max_concurrent_positions": 5,
                "max_order_value": "0.50",
                "max_orders_per_minute": 1000,
                "max_daily_loss_halt": "0.50",
                "max_loss_per_trade": "0.10",
                "per_segment_exposure_cap": "1.00",
            },
        }
    )


def _bars(closes: list[str]) -> list[Bar]:
    out: list[Bar] = []
    for i, c in enumerate(closes):
        close = Decimal(c)
        out.append(
            Bar(
                symbol=SYMBOL,
                venue=Venue.NSE,
                asset_class=AssetClass.EQUITY,
                start=START + i * ONE_MIN,
                interval=ONE_MIN,
                open=close - Decimal("1"),
                high=close + Decimal("2"),
                low=close - Decimal("2"),
                close=close,
                volume=Decimal("1000"),
            )
        )
    return out


def _backtester(bars: list[Bar]) -> EngineBacktester:
    return EngineBacktester(
        bars_for=lambda market, window: bars,  # the cold store, in production
        instruments={SYMBOL: InstrumentMeta(asset_class=AssetClass.EQUITY)},
        risk_config=_risk(),
        cost_config=COST_CONFIG,
    )


def _rise_then_fall() -> list[Bar]:
    # a trend up then down so a moving-average crossover actually enters and exits.
    return _bars([str(100 + i) for i in range(25)] + [str(125 - i) for i in range(1, 26)])


def test_engine_backtester_produces_a_per_bar_return_series() -> None:
    bars = _rise_then_fall()
    proposal = StrategyProposal(
        "ma_crossover", {"fast_period": 3, "slow_period": 8}, AssetClass.EQUITY, "2026", 1, "fp"
    )
    returns = _backtester(bars).run(proposal)
    assert all(isinstance(r, float) for r in returns)
    # ~one return per bar (mark-to-market equity) — comfortably enough for the quant-analyst's CPCV.
    assert len(returns) >= len(bars) - 1
    assert any(r != 0.0 for r in returns)  # the strategy actually traded (non-flat equity)


def test_the_discovery_loop_runs_end_to_end_on_the_real_engine() -> None:
    # the full real pipeline: the strategist proposes, EACH proposal is run through the actual
    # engine (build strategy -> run_backtest on in-sample bars) into real returns, and the
    # quant-analyst adjudicates -> a verdict per candidate. No synthetic return populations.
    backtester = _backtester(_rise_then_fall())
    with ProposalLedger() as ledger:
        report = run_discovery_cycle(
            "ma_crossover",
            market=AssetClass.EQUITY,
            window="2026",
            strategist=Strategist(ledger, proposer=RandomProposer(seed=1)),
            quant_analyst=QuantAnalyst(),
            backtester=backtester,
            n_candidates=4,
        )
    assert len(report.assessments) == 4
    assert all(
        a.verdict in (Verdict.PROMOTE, Verdict.REVISE, Verdict.REJECT)
        for _, a in report.assessments
    )
