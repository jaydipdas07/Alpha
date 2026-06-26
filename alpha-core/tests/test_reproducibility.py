"""Reproducibility ledger (B1a.7) — a backtest is bit-for-bit reproducible from its record."""

from __future__ import annotations

import copy
import random
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from alpha_core.backtest.runner import BacktestResult, run_backtest
from alpha_core.core.enums import AssetClass, OrderType, Side, Venue
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Signal, Tick
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.research.reproducibility import (
    ReproLedger,
    engine_version,
    hash_bars,
    hash_mapping,
    hash_result,
    record_backtest,
)
from alpha_core.risk.limits import RiskConfig

SYM = "BTCUSDT"
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


def _bars(n: int, base: int = 100) -> list[Bar]:
    out: list[Bar] = []
    for i in range(n):
        c = Decimal(base + i)  # rising -> entry timing affects P&L
        out.append(
            Bar(
                symbol=SYM,
                venue=Venue.BINANCE,
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


def _risk() -> RiskConfig:
    return RiskConfig.model_validate(
        {
            "base_capital": "1000000",
            "limits": {
                "max_gross_exposure": "1.00",
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


async def _run(
    strategy: Strategy, *, bars: list[Bar], cost: dict[str, object] | None = None
) -> BacktestResult:
    return await run_backtest(
        bars=bars,
        strategy=strategy,
        instruments={SYM: InstrumentMeta(asset_class=AssetClass.CRYPTO)},
        risk_config=_risk(),
        cost_config=cost or COST_CONFIG,
        venue=Venue.BINANCE,
        starting_cash=Decimal("1000000"),
    )


# --- bit-for-bit reproducibility -----------------------------------------------


async def test_backtest_is_bit_for_bit_reproducible_from_its_record() -> None:
    bars = _bars(40)
    a = await _run(_NoisyStrategy(7), bars=bars)
    b = await _run(_NoisyStrategy(7), bars=bars)  # same seed + same inputs
    assert hash_result(a) == hash_result(b)
    assert a.stats == b.stats and a.equity_curve == b.equity_curve
    rec_a = record_backtest(
        bars=bars,
        cost_config=COST_CONFIG,
        strategy_id="noisy",
        strategy_params={"seed": 7},
        rng_seed=7,
    )
    rec_b = record_backtest(
        bars=bars,
        cost_config=COST_CONFIG,
        strategy_id="noisy",
        strategy_params={"seed": 7},
        rng_seed=7,
    )
    assert rec_a == rec_b and rec_a.fingerprint == rec_b.fingerprint


async def test_seed_makes_a_stochastic_strategy_reproducible() -> None:
    bars = _bars(40)
    same_seed = hash_result(await _run(_NoisyStrategy(7), bars=bars)) == hash_result(
        await _run(_NoisyStrategy(7), bars=bars)
    )
    diff_seed = hash_result(await _run(_NoisyStrategy(7), bars=bars)) != hash_result(
        await _run(_NoisyStrategy(99), bars=bars)
    )
    assert same_seed  # same seed -> identical result
    assert diff_seed  # different seed -> different result (so the seed is a real input)


# --- the record captures everything that affects the result --------------------


def test_record_fingerprint_changes_with_each_input() -> None:
    bars = _bars(40)
    base = record_backtest(
        bars=bars, cost_config=COST_CONFIG, strategy_id="s", strategy_params={"a": 1}, rng_seed=1
    )

    def fp(**overrides: object) -> str:
        kwargs: dict[str, object] = {
            "bars": bars,
            "cost_config": COST_CONFIG,
            "strategy_id": "s",
            "strategy_params": {"a": 1},
            "rng_seed": 1,
        }
        kwargs.update(overrides)
        return record_backtest(**kwargs).fingerprint  # type: ignore[arg-type]

    assert fp(bars=_bars(41)) != base.fingerprint  # dataset
    assert fp(cost_config={**COST_CONFIG, "extra": 1}) != base.fingerprint  # cost model
    assert fp(strategy_params={"a": 2}) != base.fingerprint  # strategy params
    assert fp(strategy_id="s2") != base.fingerprint  # strategy id
    assert fp(rng_seed=2) != base.fingerprint  # RNG seed


async def test_result_changes_when_a_recorded_input_changes() -> None:
    bars = _bars(40)
    cheap = await _run(_NoisyStrategy(7), bars=bars, cost=COST_CONFIG)
    pricey_cost = copy.deepcopy(COST_CONFIG)
    pricey_cost["segments"]["crypto"]["trading_fee"]["pct"] = 0.05  # type: ignore[index]
    pricey = await _run(_NoisyStrategy(7), bars=bars, cost=pricey_cost)
    assert hash_result(cheap) != hash_result(pricey)  # the cost change shows in the result
    assert hash_mapping(COST_CONFIG) != hash_mapping(pricey_cost)  # ... and in the record


# --- the ledger persists + enforces reproducibility ----------------------------


async def test_ledger_records_persists_and_enforces(tmp_path: Path) -> None:
    bars = _bars(40)
    result = await _run(_NoisyStrategy(7), bars=bars)
    rec = record_backtest(
        bars=bars,
        cost_config=COST_CONFIG,
        strategy_id="noisy",
        strategy_params={"seed": 7},
        rng_seed=7,
    )
    rh = hash_result(result)
    db = tmp_path / "repro.db"
    with ReproLedger(db) as led:
        assert led.get(rec.fingerprint) is None
        led.record(rec, rh)
        led.record(rec, rh)  # idempotent on the identical result
        stored = led.get(rec.fingerprint)
        assert stored is not None and stored[0] == rec and stored[1] == rh
        assert led.verify(rec, rh) is True  # re-ran to the same result
        assert led.verify(rec, "deadbeef00000000") is False  # a drifted result fails verification
        with pytest.raises(ValueError, match="reproducibility violation"):
            led.record(rec, "deadbeef00000000")  # same inputs, different result -> rejected
    with ReproLedger(db) as led:  # persists across reopen
        assert led.verify(rec, rh) is True


def test_verify_unseen_record_is_false() -> None:
    with ReproLedger() as led:
        rec = record_backtest(
            bars=_bars(5), cost_config=COST_CONFIG, strategy_id="s", strategy_params={}, rng_seed=0
        )
        assert led.verify(rec, "anything") is False


def test_hash_helpers_are_stable_and_exact() -> None:
    assert hash_bars(_bars(10)) == hash_bars(_bars(10))  # deterministic
    assert hash_bars(_bars(10)) != hash_bars(_bars(11))  # content-sensitive
    # mapping hash is key-order-independent and Decimal-exact (never lossy-float)
    assert hash_mapping({"a": Decimal("1.5"), "b": 2}) == hash_mapping(
        {"b": 2, "a": Decimal("1.5")}
    )
    assert hash_mapping({"a": Decimal("1.5")}) != hash_mapping({"a": Decimal("1.50001")})
    assert engine_version()  # non-empty
