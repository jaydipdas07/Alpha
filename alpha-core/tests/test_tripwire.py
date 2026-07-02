"""The funding-regime tripwire (M3.0 basis follow-on) — the pure assessment math, ranking,
trigger boundary, and report text. The network fetch (scripts/funding_tripwire.py) is untested
glue, like every ingest script."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from alpha_core.core.enums import Venue
from alpha_core.data.funding import FundingRate
from alpha_core.research.lease import ResearchConfig
from alpha_core.research.tripwire import (
    TripwireConfig,
    assess_funding_regime,
    format_reading,
)

START = datetime(2026, 7, 1, tzinfo=UTC)


def _rates(symbol: str, per_event: str, events: int) -> list[FundingRate]:
    return [
        FundingRate(
            symbol=symbol,
            venue=Venue.BINANCE,
            funding_time=START + i * timedelta(hours=8),
            rate=Decimal(per_event),
        )
        for i in range(events)
    ]


def test_annualization_sums_the_window_robust_to_any_interval() -> None:
    # 14 days at 0.0001 per 8h event (42 events): total 0.0042 -> /14*365 = 10.95%/yr.
    config = TripwireConfig(lookback_days=14, top_k=1, alert_annual_rate=Decimal("0.10"))
    reading = assess_funding_regime({"BTCUSDT": _rates("BTCUSDT", "0.0001", 42)}, config=config)
    assert reading.ranked[0][0] == "BTCUSDT"
    assert reading.ranked[0][1] == pytest.approx(Decimal("0.1095"))
    assert reading.triggered  # 10.95% >= the 10% bar
    # the same TOTAL delivered as 4h events (84 x 0.00005) annualizes identically.
    halves = _rates("BTCUSDT", "0.00005", 84)
    again = assess_funding_regime({"BTCUSDT": halves}, config=config)
    assert again.ranked[0][1] == reading.ranked[0][1]


def test_top_k_mean_ranks_and_gates_the_trigger() -> None:
    config = TripwireConfig(lookback_days=14, top_k=2, alert_annual_rate=Decimal("0.10"))
    rates = {
        "HOT": _rates("HOT", "0.0002", 42),  # 21.9%/yr
        "WARM": _rates("WARM", "0.0001", 42),  # 10.95%/yr
        "COLD": _rates("COLD", "-0.0001", 42),  # negative — never in the top book
    }
    reading = assess_funding_regime(rates, config=config)
    assert [s for s, _ in reading.ranked] == ["HOT", "WARM"]
    assert reading.top_k_annual_rate == pytest.approx(Decimal("0.164250"))
    assert reading.triggered and reading.symbols_assessed == 3
    # raise the bar above the book's spread -> quiet.
    cold_config = TripwireConfig(lookback_days=14, top_k=2, alert_annual_rate=Decimal("0.20"))
    assert not assess_funding_regime(rates, config=cold_config).triggered


def test_empty_and_rateless_universes_stay_quiet() -> None:
    config = TripwireConfig()
    assert not assess_funding_regime({}, config=config).triggered
    quiet = assess_funding_regime({"BTCUSDT": []}, config=config)
    assert not quiet.triggered and quiet.symbols_assessed == 0


def test_format_reading_carries_the_facts() -> None:
    config = TripwireConfig(lookback_days=14, top_k=1, alert_annual_rate=Decimal("0.10"))
    text = format_reading(
        assess_funding_regime({"BTCUSDT": _rates("BTCUSDT", "0.0001", 42)}, config=config)
    )
    assert "TRIGGERED" in text and "BTCUSDT" in text and "10%" in text


def test_config_validation_and_research_yaml_round_trip() -> None:
    with pytest.raises(ValidationError):
        TripwireConfig(lookback_days=0)
    with pytest.raises(ValidationError):
        TripwireConfig(alert_annual_rate=Decimal("0"))
    # the real research.yaml parses with the block (strict model — a typo fails loud).
    config = ResearchConfig.from_config().funding_tripwire
    assert config.lookback_days == 14 and config.top_k == 8
    assert config.alert_annual_rate == Decimal("0.10")
