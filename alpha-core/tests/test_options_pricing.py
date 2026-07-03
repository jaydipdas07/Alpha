"""Options pricing/IV/Greeks + chain selection tests (ADR 0017 — item-7 core).

Black-76 values pinned against hand computation (ATM F=K=100, sigma=0.2, T=0.25:
premium 3.9878, delta 0.5199, gamma 0.03984, vega 19.922, theta -7.969/yr) and
structural identities (put-call parity, IV round-trip, no-arb None)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from alpha_core.core.enums import AssetClass, OptionRight
from alpha_core.core.models import OptionContract
from alpha_core.execution.instruments import InstrumentSpec
from alpha_core.options.chain import nearest_expiry, select_chain
from alpha_core.options.pricing import bs_price, greeks, implied_vol, time_to_expiry

F = Decimal("100")
K = Decimal("100")
VOL = Decimal("0.2")
T = Decimal("0.25")


def _close(a: Decimal, b: str, tol: str = "0.0005") -> bool:
    return abs(a - Decimal(b)) <= Decimal(tol)


def test_atm_call_and_put_match_the_hand_computed_black76_values() -> None:
    call = bs_price(F, K, VOL, T, OptionRight.CALL)
    put = bs_price(F, K, VOL, T, OptionRight.PUT)
    assert _close(call, "3.9878")
    assert _close(put, "3.9878")  # ATM under Black-76: symmetric


def test_put_call_parity_holds_off_the_money() -> None:
    f, k = Decimal("105"), Decimal("98")
    call = bs_price(f, k, VOL, T, OptionRight.CALL)
    put = bs_price(f, k, VOL, T, OptionRight.PUT)
    assert _close(call - put, str(f - k), "0.000001")  # C - P = F - K (df=1)


def test_expiry_prices_as_intrinsic() -> None:
    assert bs_price(Decimal("110"), K, VOL, Decimal(0), OptionRight.CALL) == Decimal("10")
    assert bs_price(Decimal("90"), K, VOL, Decimal(0), OptionRight.PUT) == Decimal("10")
    assert bs_price(Decimal("90"), K, VOL, Decimal(0), OptionRight.CALL) == Decimal("0")


def test_implied_vol_round_trips() -> None:
    sigma = Decimal("0.37")
    price = bs_price(F, Decimal("103"), sigma, T, OptionRight.PUT)
    solved = implied_vol(price, F, Decimal("103"), T, OptionRight.PUT)
    assert solved is not None
    assert abs(solved - sigma) < Decimal("0.000001")


def test_implied_vol_none_outside_no_arb_band() -> None:
    # Below intrinsic (stale mark) and above the vol->inf ceiling both refuse.
    assert implied_vol(Decimal("9"), Decimal("110"), K, T, OptionRight.CALL) is None
    assert implied_vol(Decimal("101"), F, K, T, OptionRight.CALL) is None
    assert implied_vol(Decimal("5"), F, K, Decimal(0), OptionRight.CALL) is None  # expired


def test_greeks_match_hand_values_and_signs() -> None:
    g = greeks(F, K, VOL, T, OptionRight.CALL)
    assert g.delta is not None and _close(g.delta, "0.5199")
    assert g.gamma is not None and _close(g.gamma, "0.03984")
    assert g.vega is not None and _close(g.vega, "19.9222", "0.001")
    assert g.theta is not None and _close(g.theta, "-7.9689", "0.001")
    assert g.rho is None  # no rate curve yet — None, never silently 0
    assert g.iv == VOL
    p = greeks(F, K, VOL, T, OptionRight.PUT)
    assert p.delta is not None and _close(p.delta, "-0.4801")  # N(d1) - 1
    assert p.gamma == g.gamma and p.vega == g.vega  # right-independent


def test_greeks_uncomputable_returns_all_none() -> None:
    g = greeks(F, K, VOL, Decimal(0), OptionRight.CALL)
    assert g.delta is None and g.vega is None and g.iv is None


def test_time_to_expiry_pins_the_nse_close_and_floors_at_zero() -> None:
    # 2026-07-03 10:00 IST -> same-day 15:30 IST close = 5.5h.
    now = datetime(2026, 7, 3, 4, 30, tzinfo=UTC)
    t = time_to_expiry(now, date(2026, 7, 3))
    assert abs(t - Decimal("5.5") / (365 * 24)) < Decimal("1e-12")
    assert time_to_expiry(datetime(2026, 7, 4, 4, 30, tzinfo=UTC), date(2026, 7, 3)) == 0
    with pytest.raises(ValueError, match="tz-aware"):
        time_to_expiry(datetime(2026, 7, 3, 4, 30), date(2026, 7, 3))


def _spec(
    strike: str, right: OptionRight, expiry: date, underlying: str = "NIFTY"
) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=f"NFO:{underlying}-{expiry:%y%b}-{strike}-{right.value[0]}E",
        asset_class=AssetClass.INDEX_OPTION,
        lot_size=Decimal("75"),
        tick_size=Decimal("0.05"),
        option=OptionContract(
            underlying=underlying,
            right=right,
            strike=Decimal(strike),
            expiry=expiry,
            lot_size=75,
        ),
    )


def test_select_chain_filters_and_sorts_deterministically() -> None:
    near, far = date(2026, 7, 9), date(2026, 8, 27)
    specs = [
        _spec("24500", OptionRight.PUT, near),
        _spec("24000", OptionRight.CALL, near),
        _spec("24000", OptionRight.PUT, near),
        _spec("24000", OptionRight.CALL, far),  # outside the DTE window
        _spec("30000", OptionRight.CALL, near),  # outside the strike band
        _spec("24000", OptionRight.CALL, near, underlying="BANKNIFTY"),  # other underlying
    ]
    picked = select_chain(
        specs,
        "NIFTY",
        reference=date(2026, 7, 3),
        max_dte=14,
        strike_lo=Decimal("23000"),
        strike_hi=Decimal("25000"),
    )
    assert [(s.option.strike, s.option.right) for s in picked] == [  # type: ignore[union-attr]
        (Decimal("24000"), OptionRight.CALL),
        (Decimal("24000"), OptionRight.PUT),
        (Decimal("24500"), OptionRight.PUT),
    ]
    only_puts = select_chain(
        specs, "NIFTY", reference=date(2026, 7, 3), max_dte=14, rights=[OptionRight.PUT]
    )
    assert all(s.option.right is OptionRight.PUT for s in only_puts)  # type: ignore[union-attr]
    with pytest.raises(ValueError, match="min_dte"):
        select_chain(specs, "NIFTY", reference=date(2026, 7, 3), min_dte=5, max_dte=2)


def test_nearest_expiry_respects_min_dte() -> None:
    near, far = date(2026, 7, 9), date(2026, 8, 27)
    specs = [_spec("24000", OptionRight.CALL, near), _spec("24000", OptionRight.CALL, far)]
    assert nearest_expiry(specs, "NIFTY", reference=date(2026, 7, 3)) == near
    assert nearest_expiry(specs, "NIFTY", reference=date(2026, 7, 3), min_dte=10) == far
    assert nearest_expiry(specs, "SENSEX", reference=date(2026, 7, 3)) is None
