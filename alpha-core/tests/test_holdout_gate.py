"""One-shot holdout gate tests (M3.7, Workflow B). HoldoutBarsFor is the gate's read path;
the structural TEST-3 isolation (the holdout being unreachable elsewhere) is proven in
test_cold_store_bars / test_holdout."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.holdout import HoldoutStore, HoldoutWindow
from alpha_core.helpers.config import load_rigor_config
from alpha_core.research.calibration import edge_population
from alpha_core.research.cold_store_bars import SeriesCoord
from alpha_core.research.holdout_gate import HoldoutBarsFor, HoldoutGate
from alpha_core.research.quant_analyst import Assessment, QuantAnalyst, Verdict
from alpha_core.research.strategist import StrategyProposal

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _bars(symbol: str, n: int = 5) -> list[Bar]:
    return [
        Bar(
            symbol=symbol,
            venue=Venue.BINANCE,
            asset_class=AssetClass.CRYPTO,
            start=T0 + timedelta(minutes=i),
            interval=timedelta(minutes=1),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("1"),
        )
        for i in range(n)
    ]


def _proposal() -> StrategyProposal:
    return StrategyProposal(
        template="ma_crossover",
        params={},
        market=AssetClass.CRYPTO,
        window="w1",
        trial_index=1,
        fingerprint="fp",
    )


class _FakeBacktester:
    """Returns a fixed return series for any proposal (the holdout read is what we control)."""

    def __init__(self, returns: Sequence[float]) -> None:
        self._returns = returns

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        return self._returns


# --- HoldoutBarsFor: the one legitimate holdout read (TEST-3) -------------------


def test_holdout_bars_for_reads_the_gate_store(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = HoldoutStore(tmp_path / "holdout")
    bars = _bars("BTCUSDT")
    store.replace(bars, HoldoutWindow(start=T0, end=T0 + timedelta(hours=1), version="v1"))
    cells = {(AssetClass.CRYPTO, "w1"): SeriesCoord("BTCUSDT", Venue.BINANCE, 60)}
    bars_for = HoldoutBarsFor(store, cells)
    got = bars_for(AssetClass.CRYPTO, "w1")
    assert len(got) == len(bars) and got[0].symbol == "BTCUSDT"


def test_holdout_bars_for_unmapped_cell_raises(tmp_path) -> None:  # type: ignore[no-untyped-def]
    bars_for = HoldoutBarsFor(HoldoutStore(tmp_path / "h"), {})
    with pytest.raises(ValueError, match="no discovery cell mapped"):
        bars_for(AssetClass.CRYPTO, "nope")


def test_holdout_bars_for_from_config_builds() -> None:
    bars_for = HoldoutBarsFor.from_config(HoldoutStore("data_holdout"))
    assert len(bars_for._cells) > 0  # maps the discovery.yaml cells


# --- HoldoutGate: PROMOTE on a real edge, else not passed ----------------------


def test_gate_passes_a_genuine_edge() -> None:
    edge = edge_population(n_candidates=1, n_obs=240, seed=4, drift=0.3)[0]
    gate = HoldoutGate(backtester=_FakeBacktester(edge), quant_analyst=QuantAnalyst())
    res = gate.evaluate(_proposal(), n_trials=1, trial_sharpe_variance=0.0)
    assert res.passed and res.verdict is Verdict.PROMOTE and res.oos_sharpe > 0


def test_gate_does_not_pass_a_non_promote_verdict() -> None:
    # The gate passes ONLY on PROMOTE — a REVISE (real-looking but unconfirmed) does not.
    # (The rigor calibration of who-promotes is tested in test_quant_analyst/test_calibration;
    # here we pin the gate's pass = PROMOTE contract with a controlled verdict.)
    class _RevisingQA:
        def assess(self, returns, **kw):  # type: ignore[no-untyped-def]
            return Assessment(
                verdict=Verdict.REVISE,
                reason="fragile",
                oos_sharpe=0.3,
                deflated_sharpe=0.4,
                fold_consistency=0.5,
                n_trials=kw["n_trials"],
            )

    gate = HoldoutGate(backtester=_FakeBacktester([0.0] * 240), quant_analyst=_RevisingQA())  # type: ignore[arg-type]
    res = gate.evaluate(_proposal(), n_trials=12, trial_sharpe_variance=1.0)
    assert res.passed is False and res.verdict is Verdict.REVISE


def test_gate_defaults_to_the_holdout_eval_operating_point() -> None:
    # oos_fraction=None resolves to rigor.yaml holdout_eval (1.0: the whole holdout is OOS to a
    # frozen candidate — the sqrt(2) power fix), NOT the quant-analyst's in-sample 50% slice; an
    # explicit override still wins (tests/calibration studies pin their own slice).
    gate = HoldoutGate(backtester=_FakeBacktester([0.0] * 240), quant_analyst=QuantAnalyst())
    assert gate._oos_fraction == load_rigor_config().holdout_eval.oos_fraction == 1.0
    pinned = HoldoutGate(
        backtester=_FakeBacktester([0.0] * 240), quant_analyst=QuantAnalyst(), oos_fraction=0.5
    )
    assert pinned._oos_fraction == 0.5


def test_gate_judges_the_whole_holdout_window() -> None:
    # a series whose FIRST half carries the edge and second half is flat: the old 50% slice saw
    # only the flat tail (Sharpe ~0 -> REJECT); the full-window gate judges every holdout bar.
    front_loaded = edge_population(n_candidates=1, n_obs=120, seed=9, drift=0.6)[0] + [0.0] * 120
    gate = HoldoutGate(backtester=_FakeBacktester(front_loaded), quant_analyst=QuantAnalyst())
    res = gate.evaluate(_proposal(), n_trials=1, trial_sharpe_variance=0.0)
    old = HoldoutGate(
        backtester=_FakeBacktester(front_loaded), quant_analyst=QuantAnalyst(), oos_fraction=0.5
    ).evaluate(_proposal(), n_trials=1, trial_sharpe_variance=0.0)
    assert res.oos_sharpe > old.oos_sharpe  # the full window sees the edge the tail hides
    assert old.verdict is Verdict.REJECT  # the half-slice threw the evidence away


def test_holdout_bars_for_rejects_asset_class_mismatch(tmp_path) -> None:  # type: ignore[no-untyped-def]
    store = HoldoutStore(tmp_path / "h")
    store.replace(
        _bars("BTCUSDT"), HoldoutWindow(start=T0, end=T0 + timedelta(hours=1), version="v")
    )
    # the cell claims EQUITY but the series' bars are CRYPTO -> fail fast (config mismap)
    cells = {(AssetClass.EQUITY, "w1"): SeriesCoord("BTCUSDT", Venue.BINANCE, 60)}
    with pytest.raises(ValueError, match="not EQUITY"):
        HoldoutBarsFor(store, cells)(AssetClass.EQUITY, "w1")
