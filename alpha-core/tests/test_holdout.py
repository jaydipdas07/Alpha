"""Roll-forward locked holdout in a no-ACL store (R5/R6, B1a.6) — proves TEST-3."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.holdout import (
    HoldoutStore,
    compute_holdout_window,
    seal_dataset,
    split_research_holdout,
)
from alpha_core.data.store import BarStore
from alpha_core.helpers.config import HoldoutConfig, load_rigor_config

SYM = "BTCUSDT"
VENUE = Venue.BINANCE
INTERVAL_S = 3600
START = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _ts(i: int) -> datetime:
    return START + i * HOUR


def _bars(n: int) -> list[Bar]:
    px = Decimal("30000")
    return [
        Bar(
            symbol=SYM,
            venue=VENUE,
            asset_class=AssetClass.CRYPTO,
            start=_ts(i),
            interval=HOUR,
            open=px,
            high=px,
            low=px,
            close=px,
            volume=Decimal("1"),
        )
        for i in range(n)
    ]


# --- roll-forward window (R5) ---------------------------------------------------


def test_holdout_window_rolls_forward() -> None:
    w1 = compute_holdout_window([_ts(i) for i in range(50)], fraction=0.2)
    w2 = compute_holdout_window([_ts(i) for i in range(80)], fraction=0.2)  # 30 newer bars
    assert w1 is not None and w2 is not None
    assert w2.start > w1.start  # the locked window rolled forward
    assert w2.end > w1.end
    assert w2.version != w1.version  # ... so holdout_window_version changed


def test_compute_holdout_window_edges() -> None:
    assert compute_holdout_window([], fraction=0.2) is None
    with pytest.raises(ValueError, match="fraction"):
        compute_holdout_window([_ts(0)], fraction=0.0)
    with pytest.raises(ValueError, match="fraction"):
        compute_holdout_window([_ts(0)], fraction=1.0)


def test_split_research_holdout() -> None:
    bars = _bars(10)
    window = compute_holdout_window([b.start for b in bars], fraction=0.3)
    assert window is not None
    research, holdout = split_research_holdout(bars, window)
    assert research and holdout
    assert all(b.start < window.start for b in research)
    assert all(window.contains(b.start) for b in holdout)
    assert len(research) + len(holdout) == len(bars)


# --- TEST-3: the holdout is unreadable through the research surface -------------


def test_holdout_is_unreadable_through_the_research_surface(tmp_path: Path) -> None:
    research = BarStore(tmp_path / "cold")
    holdout = HoldoutStore(tmp_path / "holdout")
    window = seal_dataset(_bars(100), research=research, holdout=holdout, fraction=0.2)
    assert window is not None

    # 1) The agent-facing cold store yields ZERO holdout bars via EVERY read path.
    #    a) the Bar reader (the strategist / backtester path)
    via_read = research.read_bars(symbol=SYM, venue=VENUE, interval_seconds=INTERVAL_S)
    assert via_read  # research bars ARE present (data isn't gone)
    assert all(not window.contains(b.start) for b in via_read)
    #    b) the DuckDB analytical view (the table-read / RAG query path)
    con = research.connect()
    max_start, n = con.execute("SELECT max(start), count(*) FROM bars").fetchone()
    con.close()
    assert n > 0
    assert max_start < window.start  # the cold store's newest bar precedes the locked window

    # 2) The holdout is NOT lost — the gate-only store holds exactly the locked window.
    locked = holdout.read_holdout(symbol=SYM, venue=VENUE, interval_seconds=INTERVAL_S)
    assert locked
    assert all(window.contains(b.start) for b in locked)

    # 3) The stores are physically separate roots.
    assert research.root != holdout.root


def test_seal_rejects_overlapping_roots(tmp_path: Path) -> None:
    research = BarStore(tmp_path / "cold")
    # same root -> the cold store's glob would read the holdout
    with pytest.raises(ValueError, match="disjoint"):
        seal_dataset(
            _bars(10), research=research, holdout=HoldoutStore(tmp_path / "cold"), fraction=0.2
        )
    # holdout nested under the research root
    with pytest.raises(ValueError, match="disjoint"):
        seal_dataset(
            _bars(10),
            research=research,
            holdout=HoldoutStore(tmp_path / "cold" / "inner"),
            fraction=0.2,
        )
    # research nested under the holdout root (the other direction)
    with pytest.raises(ValueError, match="disjoint"):
        seal_dataset(
            _bars(10),
            research=BarStore(tmp_path / "h" / "cold"),
            holdout=HoldoutStore(tmp_path / "h"),
            fraction=0.2,
        )


def test_seal_empty_dataset(tmp_path: Path) -> None:
    research = BarStore(tmp_path / "cold")
    holdout = HoldoutStore(tmp_path / "holdout")
    assert seal_dataset([], research=research, holdout=holdout, fraction=0.2) is None


def test_reseal_rolls_forward_without_contaminating_the_holdout(tmp_path: Path) -> None:
    research = BarStore(tmp_path / "cold")
    holdout = HoldoutStore(tmp_path / "holdout")
    w1 = seal_dataset(_bars(100), research=research, holdout=holdout, fraction=0.2)
    w2 = seal_dataset(_bars(150), research=research, holdout=holdout, fraction=0.2)  # data grew
    assert w1 is not None and w2 is not None and w2.start > w1.start  # window rolled forward

    locked = holdout.read_holdout(symbol=SYM, venue=VENUE, interval_seconds=INTERVAL_S)
    assert locked
    assert all(b.start >= w2.start for b in locked)  # ONLY the current window
    assert not any(b.start < w2.start for b in locked)  # no stale bars from w1's window

    cold = research.read_bars(symbol=SYM, venue=VENUE, interval_seconds=INTERVAL_S)
    assert any(w1.start <= b.start < w2.start for b in cold)  # released bars promoted to research
    assert all(
        b.start < w2.start for b in cold
    )  # the current holdout never leaks into the cold store
    assert not ({b.start for b in cold} & {b.start for b in locked})  # no bar lives in both stores

    cw = holdout.current_window()  # holdout_window_version recorded + rolled
    assert cw is not None and cw.version == w2.version and w2.version != w1.version


def test_current_window_is_none_before_any_seal(tmp_path: Path) -> None:
    assert HoldoutStore(tmp_path / "holdout").current_window() is None


# --- config --------------------------------------------------------------------


def test_holdout_config() -> None:
    cfg = load_rigor_config()
    assert 0.0 < cfg.holdout.fraction < 1.0
    with pytest.raises(ValidationError):
        HoldoutConfig(fraction=0.0)
