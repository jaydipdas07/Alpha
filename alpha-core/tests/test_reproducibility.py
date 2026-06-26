"""Reproducibility ledger (B1a.7) — a backtest is bit-for-bit reproducible from its record."""

from __future__ import annotations

import copy
import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from alpha_core.backtest.runner import BacktestResult
from alpha_core.core.enums import AssetClass, OrderType, Side, Venue
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Signal, Tick
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.execution.funding import FundingConfig
from alpha_core.research.reproducibility import (
    ReproLedger,
    ReproRecord,
    engine_version,
    hash_bars,
    hash_mapping,
    hash_result,
    record_backtest,
    run_and_record,
)
from alpha_core.risk.limits import RiskConfig

SYM = "BTCUSDT"
VENUE = Venue.BINANCE
START = datetime(2026, 6, 26, tzinfo=UTC)
HOUR = timedelta(hours=1)

COST_CONFIG: dict[str, object] = {
    "slippage": {
        "crypto": {"type": "bps", "value": 8},
        "default_spread": {"crypto": 0.0008},
        "stress_multiplier": 2,
    },
    "segments": {"crypto": {"trading_fee": {"pct": 0.001, "side": "both"}}},
}
INSTRUMENTS = {SYM: InstrumentMeta(asset_class=AssetClass.CRYPTO)}


def _bars(n: int, base: int = 100) -> list[Bar]:
    out: list[Bar] = []
    for i in range(n):
        c = Decimal(base + i)  # rising -> entry timing affects P&L
        out.append(
            Bar(
                symbol=SYM,
                venue=VENUE,
                asset_class=AssetClass.CRYPTO,
                start=START + i * HOUR,
                interval=HOUR,
                open=c,
                high=c + Decimal("1"),
                low=c - Decimal("1"),
                close=c,
                volume=Decimal("10"),
            )
        )
    return out


BARS = _bars(40)


class _NoisyStrategy(Strategy):
    """A seeded RNG decides which bars to buy — same seed => same decisions => reproducible."""

    def __init__(self, seed: int) -> None:
        self._rng = random.Random(seed)

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        if self._rng.random() < 0.4:
            return [
                Signal(
                    strategy_id="noisy",
                    symbol=bar.symbol,
                    asset_class=bar.asset_class,
                    side=Side.BUY,
                    quantity=Decimal("1"),
                    order_type=OrderType.MARKET,
                    created_at=bar.start + bar.interval,
                )
            ]
        return []

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []


def _risk(max_gross: str = "1.00") -> RiskConfig:
    return RiskConfig.model_validate(
        {
            "base_capital": "1000000",
            "limits": {
                "max_gross_exposure": max_gross,
                "max_position_per_instrument": "1.00",
                "max_concurrent_positions": 5,
                "max_order_value": "1.00",
                "max_orders_per_minute": 100000,
                "max_daily_loss_halt": "1.00",
                "max_loss_per_trade": "1.00",
                "per_segment_exposure_cap": "1.00",
            },
        }
    )


def _record(**over: Any) -> ReproRecord:
    kw: dict[str, Any] = {
        "bars": BARS,
        "instruments": INSTRUMENTS,
        "risk_config": _risk(),
        "cost_config": COST_CONFIG,
        "strategy_id": "s",
        "strategy_params": {"a": 1},
        "rng_seed": 1,
        "venue": VENUE,
        "starting_cash": Decimal("1000000"),
        "stress": False,
        "funding": None,
    }
    kw.update(over)
    return record_backtest(**kw)


async def _run(
    seed: int, *, ledger: ReproLedger | None = None, **over: Any
) -> tuple[BacktestResult, ReproRecord]:
    """Run + record via the wrapper, deriving the strategy AND its recorded seed/params from
    ``seed`` so they can never mismatch."""
    kw: dict[str, Any] = {
        "bars": BARS,
        "strategy": _NoisyStrategy(seed),
        "instruments": INSTRUMENTS,
        "risk_config": _risk(),
        "cost_config": COST_CONFIG,
        "strategy_id": "noisy",
        "strategy_params": {"seed": seed},
        "rng_seed": seed,
        "venue": VENUE,
    }
    kw.update(over)
    return await run_and_record(ledger=ledger, **kw)


# --- bit-for-bit reproducibility -----------------------------------------------


async def test_backtest_is_bit_for_bit_reproducible_from_its_record() -> None:
    r1, rec1 = await _run(7)
    r2, rec2 = await _run(7)  # same inputs
    assert hash_result(r1) == hash_result(r2)
    assert r1.stats == r2.stats and r1.equity_curve == r2.equity_curve
    assert rec1 == rec2 and rec1.fingerprint == rec2.fingerprint


async def test_seed_makes_a_stochastic_strategy_reproducible() -> None:
    a, _ = await _run(7)
    b, _ = await _run(7)
    c, _ = await _run(99)
    assert hash_result(a) == hash_result(b)  # same seed -> identical result
    assert hash_result(a) != hash_result(c)  # different seed -> different result


# --- the record captures EVERY input that affects the result -------------------


def test_record_fingerprint_changes_with_each_input() -> None:
    base = _record().fingerprint

    def fp(**over: Any) -> str:
        return _record(**over).fingerprint

    assert fp(bars=_bars(41)) != base  # dataset
    other_instr = {SYM: InstrumentMeta(asset_class=AssetClass.CRYPTO, tick_size=Decimal("5"))}
    assert fp(instruments=other_instr) != base  # instruments
    assert fp(risk_config=_risk(max_gross="0.5")) != base  # risk config
    assert fp(cost_config={**COST_CONFIG, "extra": 1}) != base  # cost model
    assert fp(strategy_id="other") != base  # strategy id
    assert fp(strategy_params={"x": 9}) != base  # strategy params
    assert fp(rng_seed=999) != base  # RNG seed
    assert fp(venue=Venue.NSE) != base  # venue
    assert fp(starting_cash=Decimal("2000000")) != base  # starting cash
    assert fp(stress=True) != base  # the 2x-stress flag
    assert fp(funding=FundingConfig(interval_hours=8, rate=Decimal("0.01"))) != base  # perp funding


async def test_stress_toggle_is_a_distinct_run_not_a_false_violation() -> None:
    """Regression: a flag that changes the result (stress) MUST be in the fingerprint, or the
    ledger would falsely cry 'non-deterministic engine' when a researcher just toggled it."""
    with ReproLedger() as led:
        normal, rec_n = await _run(7, ledger=led, stress=False)
        stressed, rec_s = await _run(7, ledger=led, stress=True)  # records WITHOUT raising
        assert rec_n.fingerprint != rec_s.fingerprint  # distinct runs
        assert hash_result(normal) != hash_result(stressed)  # stress really changed the result
        assert led.get(rec_n.fingerprint) is not None
        assert led.get(rec_s.fingerprint) is not None


async def test_result_hash_captures_derived_ratio_stats() -> None:
    """A change that moves only the derived float stats (return %, Sharpe) — here a larger
    starting cash with the same fills — still changes the result hash (no collision)."""
    cheap, rec_c = await _run(7)
    pricey_cost = copy.deepcopy(COST_CONFIG)
    pricey_cost["segments"]["crypto"]["trading_fee"]["pct"] = 0.05  # type: ignore[index]
    pricey, rec_p = await _run(7, cost_config=pricey_cost)
    assert hash_result(cheap) != hash_result(pricey)  # cost change shows in the result
    assert rec_c.fingerprint != rec_p.fingerprint  # ... and in the record
    big, rec_b = await _run(7, starting_cash=Decimal("5000000"))
    assert hash_result(cheap) != hash_result(big)  # return %/Sharpe differ -> hash differs
    assert rec_c.fingerprint != rec_b.fingerprint


# --- the ledger persists + enforces reproducibility ----------------------------


async def test_ledger_records_persists_and_enforces(tmp_path: Path) -> None:
    result, rec = await _run(7)
    rh = hash_result(result)
    db = tmp_path / "repro.db"
    with ReproLedger(db) as led:
        assert led.get(rec.fingerprint) is None
        led.record(rec, rh)
        led.record(rec, rh)  # idempotent on the identical result
        stored = led.get(rec.fingerprint)
        assert stored is not None and stored[0] == rec and stored[1] == rh
        assert led.verify(rec, rh) is True  # re-ran to the same result
        assert led.verify(rec, "deadbeef" * 4) is False  # a drifted result fails verification
        with pytest.raises(ValueError, match="reproducibility violation"):
            led.record(rec, "deadbeef" * 4)  # same inputs, different result -> rejected
    with ReproLedger(db) as led:  # persists across reopen
        assert led.verify(rec, rh) is True


def test_verify_unseen_record_is_false() -> None:
    with ReproLedger() as led:
        assert led.verify(_record(), "anything") is False


def test_hash_helpers_are_stable_and_exact() -> None:
    assert hash_bars(_bars(10)) == hash_bars(_bars(10))  # deterministic
    assert hash_bars(_bars(10)) != hash_bars(_bars(11))  # content-sensitive
    # mapping hash is key-order-independent, Decimal-exact, and set-order-normalized
    assert hash_mapping({"a": Decimal("1.5"), "b": 2}) == hash_mapping(
        {"b": 2, "a": Decimal("1.5")}
    )
    assert hash_mapping({"a": Decimal("1.5")}) != hash_mapping({"a": Decimal("1.50001")})
    assert hash_mapping({"s": {3, 1, 2}}) == hash_mapping({"s": {1, 2, 3}})
    assert engine_version()  # non-empty
