"""NGE pipeline tests (the Baltussen-form conditioning input — research/nge.py)."""

from __future__ import annotations

from datetime import UTC, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from alpha_core.core.enums import OptionRight, Venue
from alpha_core.data.options_store import OptionQuote, OptionsStore
from alpha_core.options.pricing import bs_price, time_to_expiry
from alpha_core.research.nge import NgeDay, compute_nge_series, shift_to_next_session

IST = ZoneInfo("Asia/Kolkata")
D1 = datetime(2024, 1, 8, tzinfo=UTC)  # Monday labels (the store's UTC-midnight convention)
D2 = datetime(2024, 1, 9, tzinfo=UTC)
EXPIRY = datetime(2024, 2, 8, tzinfo=UTC)  # a monthly ~4 weeks out (wings keep premium)
VOL = Decimal("0.20")


def _quote(day: datetime, strike: str, right: OptionRight, oi: int) -> OptionQuote:
    """A contract priced EXACTLY at Black-76 with VOL — the IV solver must recover it."""
    k = Decimal(strike)
    now = datetime.combine(day.date(), time(15, 30), tzinfo=IST)
    t = time_to_expiry(now, EXPIRY.date())
    # quantized to paise like a real NSE settle — ALSO keeps the decimal128(38,18)
    # store schema happy (a raw float-str Decimal can exceed 18 fractional digits)
    settle = bs_price(Decimal("100"), k, VOL, t, right).quantize(Decimal("0.0001"))
    return OptionQuote(
        underlying="NIFTY",
        venue=Venue.NSE,
        trade_date=day,
        expiry=EXPIRY,
        strike=k,
        right=right,
        open=settle,
        high=settle,
        low=settle,
        close=settle,
        settle=settle,
        volume_contracts=10,
        open_interest=oi,
        change_in_oi=0,
    )


def _day_chain(day: datetime, *, call_oi: int, put_oi: int) -> list[OptionQuote]:
    """A parity-clean 5-strike chain: calls at ``call_oi``, puts at ``put_oi``."""
    out: list[OptionQuote] = []
    for strike in ("90", "95", "100", "105", "110"):
        out.append(_quote(day, strike, OptionRight.CALL, call_oi))
        out.append(_quote(day, strike, OptionRight.PUT, put_oi))
    return out


def test_call_heavy_positive_put_heavy_negative(tmp_path: Path) -> None:
    store = OptionsStore(tmp_path)
    store.write(_day_chain(D1, call_oi=1000, put_oi=100))
    store.write(_day_chain(D2, call_oi=100, put_oi=1000))
    series = compute_nge_series(store)
    assert [r.day for r in series] == [D1, D2]
    assert series[0].nge > 0  # dealers-long-calls convention: call-heavy OI = long gamma
    assert series[1].nge < 0
    assert series[0].n_contracts == 10  # all 5 strikes x 2 rights solved (exact BS settles)


def test_same_day_expiry_is_excluded(tmp_path: Path) -> None:
    # A chain whose ONLY expiry is the trade date itself has no front expiry — no row
    # (the expiring chain is settling; its marks live in the expiry-settle trap).
    store = OptionsStore(tmp_path)
    expiring = [
        q.model_copy(update={"expiry": D1, "trade_date": D1})
        for q in _day_chain(D1, call_oi=500, put_oi=500)
    ]
    store.write(expiring)
    assert compute_nge_series(store) == []


def test_unusable_days_are_skipped_not_fabricated(tmp_path: Path) -> None:
    # Zero-settle rows kill the parity pair -> the day yields NO row (no signal).
    store = OptionsStore(tmp_path)
    dead = [
        q.model_copy(update={"settle": Decimal("0")})
        for q in _day_chain(D1, call_oi=500, put_oi=500)
    ]
    store.write(dead)
    assert compute_nge_series(store) == []


def test_shift_to_next_session_is_the_lookahead_fence() -> None:
    series = [
        NgeDay(day=D1, nge=5.0, n_contracts=4),
        NgeDay(day=D2, nge=-3.0, n_contracts=4),
        NgeDay(day=datetime(2024, 1, 10, tzinfo=UTC), nge=0.0, n_contracts=4),
    ]
    signs = shift_to_next_session(series)
    # day 1 has no completed prior row -> absent; each key carries the PRIOR day's sign
    assert "2024-01-08" not in signs
    assert signs["2024-01-09"] == 1  # D1 was positive
    assert signs["2024-01-10"] == -1  # D2 was negative
    assert len(signs) == 2


def test_moneyness_band_filters_far_strikes(tmp_path: Path) -> None:
    store = OptionsStore(tmp_path)
    chain = _day_chain(D1, call_oi=1000, put_oi=100)
    far = _quote(D1, "150", OptionRight.CALL, 10_000_000)  # 50% OTM — outside the band
    store.write([*chain, far])
    series = compute_nge_series(store, moneyness_band=0.15)
    assert series[0].n_contracts == 10  # the far strike contributed nothing
