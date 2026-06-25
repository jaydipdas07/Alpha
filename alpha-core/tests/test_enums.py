"""Enum tests (ADR 0002)."""

from __future__ import annotations

from alpha_core.core.enums import AssetClass, OrderState, OrderType, Side, Venue


def test_string_valued() -> None:
    assert Side.BUY == "BUY"
    assert OrderType.STOP_LIMIT == "STOP_LIMIT"
    assert AssetClass.INDEX_OPTION == "INDEX_OPTION"
    assert Venue.PAPER == "PAPER"


def test_terminal_states() -> None:
    terminal = {
        OrderState.FILLED,
        OrderState.REJECTED,
        OrderState.CANCELLED,
        OrderState.EXPIRED,
    }
    for state in OrderState:
        assert state.is_terminal == (state in terminal)


def test_rollout_order() -> None:
    assert list(AssetClass) == [AssetClass.EQUITY, AssetClass.CRYPTO, AssetClass.INDEX_OPTION]
