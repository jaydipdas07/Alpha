"""Multi-series cold-store sealing (the holdout wiring) + ``BarStore.series()``.

The crux: a store with series of *different spans* (weeks of intraday vs years of daily) must
reserve **each series' own** rolled-forward holdout — a single global window would dump an
all-recent series entirely into the holdout. The research store ends up holdout-free per series.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.holdout import seal_cold_store
from alpha_core.data.store import BarStore


def _series(
    symbol: str,
    venue: Venue,
    asset_class: AssetClass,
    interval: timedelta,
    *,
    n: int,
    start: datetime,
) -> list[Bar]:
    px = Decimal("100")
    return [
        Bar(
            symbol=symbol,
            venue=venue,
            asset_class=asset_class,
            start=start + i * interval,
            interval=interval,
            open=px,
            high=px,
            low=px,
            close=px,
            volume=Decimal("1"),
        )
        for i in range(n)
    ]


_CRYPTO_5M = ("BTCUSDT", Venue.BINANCE, AssetClass.CRYPTO, timedelta(minutes=5), 300)
_EQUITY_1D = ("NSE:RELIANCE", Venue.NSE, AssetClass.EQUITY, timedelta(days=1), 86400)


def _recent_crypto(n: int = 100) -> list[Bar]:
    sym, venue, ac, interval, _ = _CRYPTO_5M
    return _series(sym, venue, ac, interval, n=n, start=datetime(2026, 5, 1, tzinfo=UTC))


def _old_equity(n: int = 200) -> list[Bar]:
    sym, venue, ac, interval, _ = _EQUITY_1D
    return _series(sym, venue, ac, interval, n=n, start=datetime(2023, 6, 1, tzinfo=UTC))


# --- BarStore.series() ---------------------------------------------------------------------------


def test_series_lists_distinct_series_and_empty(tmp_path: Path) -> None:
    store = BarStore(tmp_path / "s")
    assert store.series() == []  # empty store
    store.write_bars(_recent_crypto(3) + _old_equity(2))
    assert store.series() == [("BTCUSDT", Venue.BINANCE, 300), ("NSE:RELIANCE", Venue.NSE, 86400)]


# --- the multi-series seal -----------------------------------------------------------------------


def test_seal_reserves_each_series_own_holdout(tmp_path: Path) -> None:
    source = BarStore(tmp_path / "raw")
    source.write_bars(_recent_crypto(100) + _old_equity(200))  # different spans (weeks vs years)
    research = BarStore(tmp_path / "research")
    holdout = BarStore(tmp_path / "holdout")

    windows = seal_cold_store(source, research=research, holdout=holdout, fraction=0.25)

    assert set(windows) == {"BINANCE|BTCUSDT|300", "NSE|NSE:RELIANCE|86400"}
    research_a = research.read_bars(symbol="BTCUSDT", venue=Venue.BINANCE, interval_seconds=300)
    research_b = research.read_bars(symbol="NSE:RELIANCE", venue=Venue.NSE, interval_seconds=86400)
    # THE CRUX: the all-recent crypto series keeps its OWN in-sample bars — it is NOT wholly holdout
    # (one global window over both spans would dump all of it); each reserves its own tail.
    assert 0 < len(research_a) < 100
    assert 0 < len(research_b) < 200
    # TEST-3 per series: the research store yields ZERO bars at/after each series' own window start.
    wa, wb = windows["BINANCE|BTCUSDT|300"], windows["NSE|NSE:RELIANCE|86400"]
    assert all(bar.start < wa.start for bar in research_a)
    assert all(bar.start < wb.start for bar in research_b)
    # research + holdout reconstruct the source per series; the holdout holds the recent tail.
    holdout_a = holdout.read_bars(symbol="BTCUSDT", venue=Venue.BINANCE, interval_seconds=300)
    assert len(research_a) + len(holdout_a) == 100
    assert holdout_a and all(bar.start >= wa.start for bar in holdout_a)
    # the per-series windows are recorded in the manifest.
    manifest = json.loads((holdout.root / "_windows.json").read_text())
    assert set(manifest) == set(windows)
    assert manifest["BINANCE|BTCUSDT|300"]["version"] == wa.version


def test_seal_rejects_non_disjoint_roots(tmp_path: Path) -> None:
    source = BarStore(tmp_path / "raw")
    with pytest.raises(ValueError, match="disjoint"):  # source == research
        seal_cold_store(source, research=source, holdout=BarStore(tmp_path / "h"), fraction=0.2)
    shared = tmp_path / "shared"
    with pytest.raises(ValueError, match="disjoint"):  # research == holdout
        seal_cold_store(source, research=BarStore(shared), holdout=BarStore(shared), fraction=0.2)


def test_reseal_rebuilds_fresh(tmp_path: Path) -> None:
    source = BarStore(tmp_path / "raw")
    research = BarStore(tmp_path / "research")
    holdout = BarStore(tmp_path / "holdout")
    source.write_bars(_recent_crypto(100) + _old_equity(200))
    seal_cold_store(source, research=research, holdout=holdout, fraction=0.25)
    assert len(research.series()) == 2

    # re-seal from a source that now holds only the crypto series -> the equity series is cleared
    # from both targets (a rolled-forward re-seal must not leave stale bars behind).
    fresh = BarStore(tmp_path / "raw2")
    fresh.write_bars(_recent_crypto(100))
    seal_cold_store(fresh, research=research, holdout=holdout, fraction=0.25)
    assert research.series() == [("BTCUSDT", Venue.BINANCE, 300)]
    assert json.loads((holdout.root / "_windows.json").read_text()).keys() == {
        "BINANCE|BTCUSDT|300"
    }
