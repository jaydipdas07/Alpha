"""Risk manager + limits tests (ADR 0006)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from alpha_core.core.enums import AssetClass, OrderState, OrderType, Side, Venue
from alpha_core.core.models import Order, Position
from alpha_core.risk.limits import RiskConfig, load_risk_config
from alpha_core.risk.manager import KillTrigger, RiskManager, WorkingExposure

NOW = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)


def _cfg(**limit_overrides: Any) -> RiskConfig:
    limits = {
        "max_gross_exposure": "1.00",
        "max_position_per_instrument": "0.20",
        "max_concurrent_positions": 5,
        "max_order_value": "0.25",
        "max_orders_per_minute": 10,
        "max_daily_loss_halt": "0.02",
        "max_loss_per_trade": "0.01",
        "per_segment_exposure_cap": "0.60",
    }
    limits.update(limit_overrides)
    return RiskConfig.model_validate(
        {"base_capital": "100000", "currency": "INR", "limits": limits}
    )


def _order(side: Side = Side.BUY, qty: str = "10", stop: str | None = None) -> Order:
    return Order(
        client_order_id="alpha-1",
        symbol="NSE:RELIANCE",
        venue=Venue.NSE,
        asset_class=AssetClass.EQUITY,
        side=side,
        order_type=OrderType.STOP if stop else OrderType.MARKET,
        quantity=Decimal(qty),
        stop_price=Decimal(stop) if stop else None,
        state=OrderState.NEW,
        strategy_id="s1",
        created_at=NOW,
        updated_at=NOW,
    )


def _pos(symbol: str, qty: str, price: str = "100", ac: AssetClass = AssetClass.EQUITY) -> Position:
    return Position(
        venue=Venue.NSE,
        symbol=symbol,
        asset_class=ac,
        quantity=Decimal(qty),
        average_price=Decimal(price),
        realized_pnl=Decimal("0"),
        last_price=Decimal(price),
        updated_at=NOW,
    )


def _check(mgr: RiskManager, order: Order, positions: list[Position], price: str = "100") -> Any:
    return mgr.check_order(order, reference_price=Decimal(price), positions=positions, now=NOW)


# --- approve + halt gate -------------------------------------------------------


def test_normal_order_approved() -> None:
    assert _check(RiskManager(_cfg()), _order(), []).approved is True


def test_halt_gate_rejects() -> None:
    mgr = RiskManager(_cfg())
    mgr.trip(KillTrigger.MANUAL)
    d = _check(mgr, _order(), [])
    assert d.approved is False
    assert "halted" in (d.reason or "")


# --- step 3: fat-finger --------------------------------------------------------


def test_max_order_value() -> None:
    # 300 * 100 = 30000 > 25% of 100000 (25000)
    assert _check(RiskManager(_cfg()), _order(qty="300"), []).reason == "max_order_value exceeded"


# --- step 4: throttle ----------------------------------------------------------


def test_throttle() -> None:
    mgr = RiskManager(_cfg(max_orders_per_minute=2))
    assert _check(mgr, _order(), []).approved
    assert _check(mgr, _order(), []).approved
    assert _check(mgr, _order(), []).reason == "max_orders_per_minute exceeded"


# --- step 5: per-instrument ----------------------------------------------------


def test_per_instrument_cap() -> None:
    mgr = RiskManager(_cfg())
    # existing 100 @100 = 10000; buy 120 -> 220 @100 = 22000 > 20000
    d = _check(mgr, _order(qty="120"), [_pos("NSE:RELIANCE", "100")])
    assert d.reason == "max_position_per_instrument exceeded"


# --- step 6: concurrent --------------------------------------------------------


def test_concurrent_positions() -> None:
    mgr = RiskManager(_cfg(max_concurrent_positions=2))
    held = [_pos("A", "10"), _pos("B", "10")]
    d = _check(mgr, _order(qty="10"), held)  # opening a 3rd symbol
    assert d.reason == "max_concurrent_positions exceeded"


# --- step 7/8: segment + gross -------------------------------------------------


def test_gross_exposure() -> None:
    mgr = RiskManager(_cfg(per_segment_exposure_cap="1.00"))  # isolate gross
    held = [_pos(s, "180") for s in ("A", "B", "C", "D", "E")]  # 5 * 18000 = 90000
    # buy on existing A: 180 -> ... keep per-instrument ok but push gross > 100000
    held[0] = _pos("NSE:RELIANCE", "190")  # 19000
    d = mgr.check_order(
        _order(qty="20"),
        reference_price=Decimal("100"),
        positions=[_pos("NSE:RELIANCE", "190"), *[_pos(s, "200") for s in ("B", "C", "D", "E")]],
        now=NOW,
    )
    # resulting RELIANCE 210 @100 = 21000 > per-instrument 20000 -> per-instrument fires first
    assert d.reason == "max_position_per_instrument exceeded"


def test_segment_cap() -> None:
    mgr = RiskManager(_cfg())
    held = [_pos(s, "190") for s in ("B", "C", "D")]  # 3 * 19000 = 57000 equity
    d = _check(mgr, _order(qty="100"), held)  # +10000 -> 67000 > 60000 segment cap
    assert d.reason == "per_segment_exposure_cap exceeded"


# --- de-risking bypass ---------------------------------------------------------


def test_reducing_bypasses_exposure_checks() -> None:
    mgr = RiskManager(_cfg(max_position_per_instrument="0.01"))  # tiny cap
    # hold long 100, SELL 50 reduces -> should pass despite tiny per-instrument cap
    d = _check(mgr, _order(side=Side.SELL, qty="50"), [_pos("NSE:RELIANCE", "100")])
    assert d.approved is True


def test_reducing_still_blocked_when_halted() -> None:
    mgr = RiskManager(_cfg())
    mgr.trip(KillTrigger.MANUAL)
    d = _check(mgr, _order(side=Side.SELL, qty="50"), [_pos("NSE:RELIANCE", "100")])
    assert d.approved is False


# --- kill switch ---------------------------------------------------------------


def test_consecutive_errors_trip() -> None:
    mgr = RiskManager(_cfg())
    mgr.record_error()
    mgr.record_error()
    assert mgr.is_halted is False
    mgr.record_error()  # 3rd
    assert mgr.is_halted is True
    assert mgr.halt_trigger is KillTrigger.CONSECUTIVE_ERRORS


def test_record_success_resets_errors() -> None:
    mgr = RiskManager(_cfg())
    mgr.record_error()
    mgr.record_error()
    mgr.record_success()
    mgr.record_error()
    mgr.record_error()
    assert mgr.is_halted is False


def test_daily_loss_trips() -> None:
    mgr = RiskManager(_cfg())
    mgr.update_pnl(realized=Decimal("-1500"), unrealized=Decimal("-600"))  # -2100 < -2000
    assert mgr.is_halted is True
    assert mgr.halt_trigger is KillTrigger.DAILY_LOSS


def test_rearm_clears_halt() -> None:
    mgr = RiskManager(_cfg())
    mgr.trip(KillTrigger.MANUAL)
    mgr.rearm()
    assert mgr.is_halted is False
    assert _check(mgr, _order(), []).approved is True


def test_halt_generation_one_per_distinct_trip() -> None:
    # The worker flattens once per generation, so a re-trip while already halted must
    # NOT bump it, but a fresh trip after a re-arm must (else the re-trip's flatten is
    # skipped — review BLOCKER 2).
    mgr = RiskManager(_cfg())
    assert mgr.halt_generation == 0
    mgr.trip(KillTrigger.DAILY_LOSS)
    g1 = mgr.halt_generation
    assert g1 == 1
    mgr.trip(KillTrigger.DAILY_LOSS)  # re-trip while halted (e.g. the per-mark re-check)
    assert mgr.halt_generation == g1  # unchanged
    mgr.rearm()
    assert mgr.halt_generation == g1  # re-arm does not reset
    mgr.trip(KillTrigger.DAILY_LOSS)  # a fresh trip after re-arm
    assert mgr.halt_generation == 2  # bumped -> the worker flattens again


def test_restored_halt_bumps_generation() -> None:
    mgr = RiskManager(_cfg())
    mgr.restore(halted=True, trigger=KillTrigger.DAILY_LOSS)
    assert mgr.is_halted is True and mgr.halt_generation == 1  # flatten once on boot


# --- step 10: stop discipline --------------------------------------------------


def test_require_stop_rejects_missing() -> None:
    mgr = RiskManager(_cfg(), require_stop=True)
    assert _check(mgr, _order(), []).reason == "missing protective stop"


def test_require_stop_enforces_max_loss() -> None:
    mgr = RiskManager(_cfg(), require_stop=True)
    # ref 100, stop 50, qty 100 -> worst 5000 > 1% of 100000 (1000)
    d = _check(mgr, _order(qty="100", stop="50"), [])
    assert d.reason == "max_loss_per_trade exceeded"


def test_require_stop_ok() -> None:
    mgr = RiskManager(_cfg(), require_stop=True)
    # ref 100, stop 99, qty 10 -> worst 10 <= 1000
    assert _check(mgr, _order(qty="10", stop="99"), []).approved is True


# --- margin (G10) --------------------------------------------------------------


def _cfg_margin(**over: Any) -> RiskConfig:
    # Raise the exposure caps so the margin estimate is the only binding check.
    return _cfg(
        max_gross_exposure="10.00",
        per_segment_exposure_cap="10.00",
        max_position_per_instrument="10.00",
        max_order_value="10.00",
        **over,
    )


def test_insufficient_margin_rejected() -> None:
    # 6000 @ 100 = 600000 notional / 5x equity leverage = 120000 > 100000 capital
    mgr = RiskManager(_cfg_margin())
    assert _check(mgr, _order(qty="6000"), []).reason == "insufficient margin"


def test_margin_within_capital_approved() -> None:
    # 4000 @ 100 = 400000 / 5x = 80000 < 100000
    mgr = RiskManager(_cfg_margin())
    assert _check(mgr, _order(qty="4000"), []).approved is True


def test_margin_disabled_skips_check() -> None:
    cfg = _cfg_margin()
    cfg = cfg.model_copy(update={"margin": cfg.margin.model_copy(update={"enabled": False})})
    mgr = RiskManager(cfg)
    assert _check(mgr, _order(qty="6000"), []).approved is True  # would breach if enabled


def test_reducing_order_skips_margin() -> None:
    # Closing a position never needs more margin -> allowed even past the estimate.
    mgr = RiskManager(_cfg_margin())
    pos = [_pos("NSE:RELIANCE", "6000")]
    assert _check(mgr, _order(side=Side.SELL, qty="6000"), pos).approved is True


def test_leverage_must_be_positive() -> None:
    with pytest.raises(ValueError, match="leverage"):
        RiskConfig.model_validate(
            {
                "base_capital": "100000",
                "limits": _cfg().limits.model_dump(),
                "margin": {"enabled": True, "leverage": {"equity": "0"}},
            }
        )


# --- real config ---------------------------------------------------------------


def test_real_risk_yaml_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALPHA_CONFIG_DIR", raising=False)
    cfg = load_risk_config()
    assert cfg.base_capital > 0  # a tunable (pilot size) — don't pin the exact value
    assert cfg.limits.max_order_value == Decimal("0.25")
    # cap() scales the %-limit off base_capital (the relationship, not an absolute)
    assert cfg.cap(cfg.limits.max_order_value) == cfg.base_capital * Decimal("0.25")
    assert cfg.margin.enabled is True
    assert cfg.margin.leverage["equity"] == Decimal("5")


# --- in-flight exposure reservation (ADR 0014) ---------------------------------


def test_working_order_reserves_segment_exposure() -> None:
    # Per-segment cap = 60000. A 100-share buy (10000) is fine on an empty book,
    # but with an in-flight 540-share buy reserved (54000) the same order would
    # take the segment to 64000 and is rejected — concurrent strategies can't both
    # pass against a book that ignores each other's unfilled orders.
    mgr = RiskManager(_cfg(per_segment_exposure_cap="0.60"))
    assert _check(mgr, _order(qty="100"), []).approved is True
    working = [
        WorkingExposure(
            venue=Venue.NSE,
            symbol="NSE:AAA",
            asset_class=AssetClass.EQUITY,
            side=Side.BUY,
            remaining_qty=Decimal("540"),
            price=Decimal("100"),
        )
    ]
    d = mgr.check_order(
        _order(qty="100"),
        reference_price=Decimal("100"),
        positions=[],
        now=NOW,
        working=working,
    )
    assert d.reason == "per_segment_exposure_cap exceeded"


def test_reducing_order_ignores_working_reservation() -> None:
    # A working BUY on the same symbol nets against a SELL; reducing orders skip
    # exposure checks regardless of reservations.
    mgr = RiskManager(_cfg(per_segment_exposure_cap="0.01"))  # tiny cap
    working = [
        WorkingExposure(
            venue=Venue.NSE,
            symbol="NSE:RELIANCE",
            asset_class=AssetClass.EQUITY,
            side=Side.BUY,
            remaining_qty=Decimal("100"),
            price=Decimal("100"),
        )
    ]
    d = mgr.check_order(
        _order(side=Side.SELL, qty="50"),
        reference_price=Decimal("100"),
        positions=[_pos("NSE:RELIANCE", "100")],
        now=NOW,
        working=working,
    )
    assert d.approved is True
