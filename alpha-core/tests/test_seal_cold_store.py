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
from alpha_core.data.holdout import compute_holdout_window, seal_cold_store
from alpha_core.data.store import BarStore
from alpha_core.research.cold_store_bars import ColdStoreBarsFor, SeriesCoord


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


def test_cold_store_bars_reads_the_sealed_research_store_holdout_free(tmp_path: Path) -> None:
    # the PRODUCTION path: seal_cold_store -> research store -> ColdStoreBarsFor (what the nightly
    # uses). Every cell the adapter serves must be holdout-free (TEST-3 at the real boundary).
    source = BarStore(tmp_path / "raw")
    source.write_bars(_recent_crypto(100) + _old_equity(200))
    research = BarStore(tmp_path / "research")
    windows = seal_cold_store(
        source, research=research, holdout=BarStore(tmp_path / "holdout"), fraction=0.25
    )
    adapter = ColdStoreBarsFor(
        research,
        {
            (AssetClass.CRYPTO, "btc"): SeriesCoord("BTCUSDT", Venue.BINANCE, 300),
            (AssetClass.EQUITY, "rel"): SeriesCoord("NSE:RELIANCE", Venue.NSE, 86400),
        },
    )
    crypto = adapter(AssetClass.CRYPTO, "btc")
    equity = adapter(AssetClass.EQUITY, "rel")
    assert crypto and equity  # the adapter serves in-sample bars from the sealed store
    assert all(b.start < windows["BINANCE|BTCUSDT|300"].start for b in crypto)
    assert all(b.start < windows["NSE|NSE:RELIANCE|86400"].start for b in equity)


def test_seal_rejects_non_disjoint_roots(tmp_path: Path) -> None:
    source = BarStore(tmp_path / "raw")
    with pytest.raises(ValueError, match="disjoint"):  # source == research
        seal_cold_store(source, research=source, holdout=BarStore(tmp_path / "h"), fraction=0.2)
    shared = tmp_path / "shared"
    with pytest.raises(ValueError, match="disjoint"):  # research == holdout
        seal_cold_store(source, research=BarStore(shared), holdout=BarStore(shared), fraction=0.2)


# --- the monotonic holdout floor (TEST-3): the boundary only ever moves FORWARD -------------------


def test_reseal_over_backward_extended_history_pins_the_boundary(tmp_path: Path) -> None:
    # The Finding-3 hazard: extending a series' history BACKWARD (2023 -> 2019 ingest) stretches
    # the span, so the fraction-of-span start would move backward — into bars the discovery loop
    # already researched. The floor pins it: the holdout stays the same never-seen tail.
    sym, venue, ac, interval, interval_s = _EQUITY_1D
    source = BarStore(tmp_path / "raw")
    research = BarStore(tmp_path / "research")
    holdout = BarStore(tmp_path / "holdout")
    source.write_bars(
        _series(sym, venue, ac, interval, n=200, start=datetime(2023, 6, 1, tzinfo=UTC))
    )
    first = seal_cold_store(source, research=research, holdout=holdout, fraction=0.25)
    pinned = first[f"{venue.value}|{sym}|{interval_s}"].start

    # extend the SAME series 400 bars further back (the raw store merges; span now much longer).
    source.write_bars(
        _series(sym, venue, ac, interval, n=400, start=datetime(2022, 4, 27, tzinfo=UTC))
    )
    second = seal_cold_store(source, research=research, holdout=holdout, fraction=0.25)
    window = second[f"{venue.value}|{sym}|{interval_s}"]
    assert window.start == pinned  # floored — NOT dragged back by the longer span
    # nothing at/after the pinned boundary reached the research store (TEST-3 held).
    research_bars = research.read_bars(symbol=sym, venue=venue, interval_seconds=interval_s)
    assert research_bars and all(b.start < pinned for b in research_bars)
    # and the manifest records the CLAMPED window (the floor compounds across seals).
    manifest = json.loads((holdout.root / "_windows.json").read_text())
    assert manifest[f"{venue.value}|{sym}|{interval_s}"]["start"] == pinned.isoformat()


def test_reseal_still_rolls_the_boundary_forward(tmp_path: Path) -> None:
    # the floor is one-directional: NEW data at the end still rolls the window forward (R5).
    sym, venue, ac, interval, interval_s = _EQUITY_1D
    source = BarStore(tmp_path / "raw")
    research = BarStore(tmp_path / "research")
    holdout = BarStore(tmp_path / "holdout")
    start = datetime(2023, 6, 1, tzinfo=UTC)
    source.write_bars(_series(sym, venue, ac, interval, n=200, start=start))
    first = seal_cold_store(source, research=research, holdout=holdout, fraction=0.25)
    source.write_bars(_series(sym, venue, ac, interval, n=280, start=start))  # 80 newer bars
    second = seal_cold_store(source, research=research, holdout=holdout, fraction=0.25)
    key = f"{venue.value}|{sym}|{interval_s}"
    assert second[key].start > first[key].start


def test_new_series_inherits_the_interval_floor(tmp_path: Path) -> None:
    # a series never sealed before (e.g. the basis track's spot leg) inherits the latest prior
    # start among same-interval series: its pre-boundary history was never holdout anywhere, but
    # its post-boundary tail must stay unseen like its siblings'.
    sym, venue, ac, interval, interval_s = _EQUITY_1D
    source = BarStore(tmp_path / "raw")
    research = BarStore(tmp_path / "research")
    holdout = BarStore(tmp_path / "holdout")
    start = datetime(2023, 6, 1, tzinfo=UTC)
    source.write_bars(_series(sym, venue, ac, interval, n=200, start=start))
    first = seal_cold_store(source, research=research, holdout=holdout, fraction=0.25)
    sibling_start = first[f"{venue.value}|{sym}|{interval_s}"].start

    # a NEW same-interval series with a LONGER history but the same recent end: its own 25% tail
    # would start far earlier than the sibling boundary -> it must be floored AT that boundary.
    source.write_bars(
        _series("NSE:TCS", venue, ac, interval, n=565, start=datetime(2022, 6, 1, tzinfo=UTC))
    )
    second = seal_cold_store(source, research=research, holdout=holdout, fraction=0.25)
    assert second[f"{venue.value}|NSE:TCS|{interval_s}"].start == sibling_start
    # a different-interval series is NOT clamped by it (each interval pins its own boundary).
    csym, cvenue, cac, cinterval, cinterval_s = _CRYPTO_5M
    source.write_bars(
        _series(csym, cvenue, cac, cinterval, n=100, start=datetime(2026, 5, 1, tzinfo=UTC))
    )
    third = seal_cold_store(source, research=research, holdout=holdout, fraction=0.25)
    assert third[f"{cvenue.value}|{csym}|{cinterval_s}"].start > sibling_start


def test_new_short_history_member_adopts_the_boundary_outright(tmp_path: Path) -> None:
    # a never-sealed member listed INSIDE the siblings' never-seen window: its own fraction-of-span
    # start would be later than the pinned boundary, quietly handing bars from the panel's holdout
    # window to the research store. It must adopt the boundary outright -> ALL holdout here.
    sym, venue, ac, interval, interval_s = _EQUITY_1D
    source = BarStore(tmp_path / "raw")
    research = BarStore(tmp_path / "research")
    holdout = BarStore(tmp_path / "holdout")
    source.write_bars(
        _series(sym, venue, ac, interval, n=200, start=datetime(2023, 6, 1, tzinfo=UTC))
    )
    first = seal_cold_store(source, research=research, holdout=holdout, fraction=0.25)
    boundary = first[f"{venue.value}|{sym}|{interval_s}"].start

    # NSE:NEWIPO lists AFTER the boundary — its whole life is inside the never-seen window.
    listed = boundary + 5 * interval
    source.write_bars(_series("NSE:NEWIPO", venue, ac, interval, n=20, start=listed))
    second = seal_cold_store(source, research=research, holdout=holdout, fraction=0.25)
    assert second[f"{venue.value}|NSE:NEWIPO|{interval_s}"].start == boundary
    assert (
        research.read_bars(symbol="NSE:NEWIPO", venue=venue, interval_seconds=interval_s) == []
    )  # nothing from the never-seen window reached the research store
    assert (
        len(holdout.read_bars(symbol="NSE:NEWIPO", venue=venue, interval_seconds=interval_s)) == 20
    )


def test_new_series_on_another_venue_is_not_floored_by_this_one(tmp_path: Path) -> None:
    # the inherited floor is VENUE-scoped: an unrelated market's later boundary must never set a
    # new member's boundary (a cross-market clamp would silently move data across the TEST-3 line).
    sym, venue, ac, interval, interval_s = _EQUITY_1D
    source = BarStore(tmp_path / "raw")
    research = BarStore(tmp_path / "research")
    holdout = BarStore(tmp_path / "holdout")
    source.write_bars(
        _series(sym, venue, ac, interval, n=200, start=datetime(2023, 6, 1, tzinfo=UTC))
    )
    seal_cold_store(source, research=research, holdout=holdout, fraction=0.25)

    # a never-sealed BINANCE daily series: same interval, different venue -> its OWN computed
    # window (not the NSE floor).
    crypto = _series(
        "BTCUSDT",
        Venue.BINANCE,
        AssetClass.CRYPTO,
        interval,
        n=400,
        start=datetime(2022, 6, 1, tzinfo=UTC),
    )
    source.write_bars(crypto)
    windows = seal_cold_store(source, research=research, holdout=holdout, fraction=0.25)
    own = compute_holdout_window([b.start for b in crypto], fraction=0.25)
    assert own is not None
    assert windows[f"BINANCE|BTCUSDT|{interval_s}"].start == own.start


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


def test_seed_floor_pins_a_new_venue_at_its_twins_boundary(tmp_path: Path) -> None:
    # A brand-new venue (the basis SPOT leg) has no manifest history of its own; without a seed
    # its fraction-of-span boundary would sit far earlier than the twin perp venue's published
    # boundary. seed_floors pins it — and is venue-scoped: the perp series keeps its own window.
    source = BarStore(tmp_path / "raw")
    research = BarStore(tmp_path / "research")
    holdout = BarStore(tmp_path / "holdout")
    interval = timedelta(days=1)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    perp = _series("BTCUSDT", Venue.BINANCE, AssetClass.CRYPTO, interval, n=100, start=start)
    spot = _series("BTCUSDT", Venue.BINANCE_SPOT, AssetClass.CRYPTO, interval, n=100, start=start)
    source.write_bars(perp + spot)

    computed = compute_holdout_window([b.start for b in spot], fraction=0.2)
    assert computed is not None
    seed = computed.start + timedelta(days=10)  # the twin boundary sits LATER than fresh 20%
    windows = seal_cold_store(
        source,
        research=research,
        holdout=holdout,
        fraction=0.2,
        seed_floors={(Venue.BINANCE_SPOT, 86400): seed},
    )
    assert windows["BINANCE_SPOT|BTCUSDT|86400"].start == seed  # pinned at the twin boundary
    assert windows["BINANCE|BTCUSDT|86400"].start == computed.start  # venue-scoped: unaffected
    # the pinned window is what lands in the manifest, so the floor compounds on the next seal.
    manifest = json.loads((holdout.root / "_windows.json").read_text())
    assert manifest["BINANCE_SPOT|BTCUSDT|86400"]["start"] == seed.isoformat()
    # and the research store holds no spot bar at/after the pinned boundary (TEST-3).
    spot_research = research.read_bars(
        symbol="BTCUSDT", venue=Venue.BINANCE_SPOT, interval_seconds=86400
    )
    assert spot_research and all(b.start < seed for b in spot_research)
