"""Instrument registry tests (G9) — universe gate + lot/tick quantization."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from alpha_core.core.enums import AssetClass
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.execution.instruments import InstrumentRegistry, InstrumentSpec
from alpha_core.helpers.config import ConfigError


def _spec(**over: object) -> InstrumentSpec:
    base: dict[str, object] = {
        "symbol": "NSE:RELIANCE",
        "asset_class": AssetClass.EQUITY,
        "lot_size": Decimal("1"),
        "tick_size": Decimal("0.05"),
    }
    base.update(over)
    return InstrumentSpec.model_validate(base)


def test_round_quantity_floors_to_lot() -> None:
    spec = _spec(lot_size=Decimal("50"))  # F&O-style lot
    assert spec.round_quantity(Decimal("125")) == Decimal("100")  # 2 lots
    assert spec.round_quantity(Decimal("49")) == Decimal("0")  # below one lot


def test_round_price_snaps_to_tick() -> None:
    spec = _spec(tick_size=Decimal("0.05"))
    assert spec.round_price(Decimal("2500.123")) == Decimal("2500.10")
    assert spec.round_price(Decimal("2500.13")) == Decimal("2500.15")


def test_cost_meta_is_derived() -> None:
    spec = _spec(tick_size=Decimal("0.05"))
    meta = spec.cost_meta()
    assert isinstance(meta, InstrumentMeta)
    assert meta.asset_class is AssetClass.EQUITY
    assert meta.tick_size == Decimal("0.05")


def test_registry_gates_unknown() -> None:
    reg = InstrumentRegistry({"NSE:RELIANCE": _spec()})
    assert reg.is_known("NSE:RELIANCE") is True
    assert reg.is_known("NSE:UNKNOWN") is False
    with pytest.raises(ConfigError):
        reg.get("NSE:UNKNOWN")


def _kite_rows() -> list[dict[str, object]]:
    return [
        {  # tradable equity
            "instrument_token": 256265,
            "tradingsymbol": "RELIANCE",
            "exchange": "NSE",
            "instrument_type": "EQ",
            "lot_size": 1,
            "tick_size": 0.05,
            "expiry": "",
        },
        {  # index option (kept)
            "instrument_token": 12345678,
            "tradingsymbol": "NIFTY24JUN24000CE",
            "exchange": "NFO",
            "instrument_type": "CE",
            "lot_size": 75,
            "tick_size": 0.05,
            "expiry": "2026-06-25",
        },
        {  # index (skipped — not tradable)
            "instrument_token": 256,
            "tradingsymbol": "NIFTY 50",
            "exchange": "NSE",
            "instrument_type": "EQ",  # but it's actually an index... real dumps use INDICES segment
            "lot_size": 0,
            "tick_size": 0,
            "expiry": "",
        },
        {  # NFO future (skipped for the option pilot)
            "instrument_token": 999,
            "tradingsymbol": "NIFTY24JUNFUT",
            "exchange": "NFO",
            "instrument_type": "FUT",
            "lot_size": 75,
            "tick_size": 0.05,
            "expiry": "2026-06-25",
        },
        {  # MCX commodity (skipped — unsupported exchange)
            "instrument_token": 111,
            "tradingsymbol": "GOLD",
            "exchange": "MCX",
            "instrument_type": "FUT",
            "lot_size": 100,
            "tick_size": 1,
            "expiry": "2026-08-05",
        },
    ]


def test_from_kite_dump_filters_and_maps() -> None:
    reg = InstrumentRegistry.from_kite_dump(_kite_rows())
    assert reg.is_known("NSE:RELIANCE")
    assert reg.is_known("NFO:NIFTY24JUN24000CE")
    assert not reg.is_known("NFO:NIFTY24JUNFUT")  # future skipped
    assert not reg.is_known("MCX:GOLD")  # unsupported exchange skipped
    opt = reg.get("NFO:NIFTY24JUN24000CE")
    assert opt.asset_class is AssetClass.INDEX_OPTION
    assert opt.lot_size == Decimal("75")
    assert opt.expiry == date(2026, 6, 25)
    assert opt.instrument_token == 12345678


def test_token_map_from_dump() -> None:
    reg = InstrumentRegistry.from_kite_dump(_kite_rows())
    tokens = reg.token_map()
    assert tokens["NSE:RELIANCE"] == 256265
    assert tokens["NFO:NIFTY24JUN24000CE"] == 12345678


def test_from_kite_json_roundtrip(tmp_path: Path) -> None:
    import json

    p = tmp_path / "kite.json"
    p.write_text(json.dumps(_kite_rows()))
    reg = InstrumentRegistry.from_kite_json(p)
    assert reg.is_known("NSE:RELIANCE")


def test_config_universe_has_no_tokens() -> None:
    # the curated config universe carries no instrument tokens (Kite-only)
    assert InstrumentRegistry.from_config().token_map() == {}


def test_registry_from_config_loads_universe() -> None:
    reg = InstrumentRegistry.from_config()
    assert reg.is_known("NSE:RELIANCE")
    assert reg.is_known("NSE:HDFCBANK")
    assert len(reg) >= 4
    # the derived cost-meta map is usable by the paper broker / cost model
    metas = reg.cost_metas()
    assert metas["NSE:RELIANCE"].tick_size == Decimal("0.05")


def test_symbols_for_filters_by_asset_class() -> None:
    from alpha_core.core.enums import AssetClass

    reg = InstrumentRegistry.from_config()
    crypto = reg.symbols_for({AssetClass.CRYPTO})
    equity = reg.symbols_for({AssetClass.EQUITY, AssetClass.INDEX_OPTION})
    assert "BTC/USDT" in crypto and "NSE:RELIANCE" not in crypto
    assert "NSE:RELIANCE" in equity and "BTC/USDT" not in equity


def test_symbols_for_filters_by_venue() -> None:
    # P19.1: crypto venues share the CRYPTO asset class but list different
    # symbols — a Delta run must not get Binance-spot symbols.
    from alpha_core.core.enums import AssetClass, Venue

    reg = InstrumentRegistry.from_config()
    delta = reg.symbols_for({AssetClass.CRYPTO}, venue=Venue.DELTA)
    binance = reg.symbols_for({AssetClass.CRYPTO}, venue=Venue.BINANCE)
    # The filtering property (resilient to adding Delta symbols): a Delta run sees the
    # Delta-tagged USD-settled perps and never a Binance-spot symbol, and vice versa.
    assert "BTC/USD:USD" in delta and "BTC/USDT" not in delta
    assert all(s.endswith("/USD:USD") for s in delta)  # Delta-tagged only
    assert "BTC/USDT" in binance and "BTC/USD:USD" not in binance
    # untagged equity matches any venue of its asset class
    assert "NSE:RELIANCE" in reg.symbols_for({AssetClass.EQUITY}, venue=Venue.NSE)


def test_symbols_for_no_venue_is_unfiltered() -> None:
    # Back-compat: omitting venue returns all symbols of the asset class.
    from alpha_core.core.enums import AssetClass

    reg = InstrumentRegistry.from_config()
    crypto = reg.symbols_for({AssetClass.CRYPTO})
    assert {"BTC/USDT", "BTC/USD:USD"} <= set(crypto)
