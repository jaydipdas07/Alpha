"""Instrument registry — the tradable universe + lot/tick rules (ADR 0002/0010).

`instruments.yaml` is the single source of truth for *what* may be traded and the
venue's quantization rules. The registry:

- **gates the universe** — an order for an unknown symbol is rejected (a strategy
  bug or a typo never reaches the venue), and
- **quantizes orders** — quantity is floored to a whole number of lots and price
  is snapped to the tick grid, so the venue never rejects an off-lot / off-tick
  order (ADR 0012 precision failure mode).

`InstrumentSpec` is the authoritative per-instrument record; the cost model's
`InstrumentMeta` is *derived* from it (`cost_meta`), so the two never drift.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from alpha_core.core.enums import AssetClass, OptionRight, Settlement, Venue
from alpha_core.core.models import OptionContract
from alpha_core.execution.costs import InstrumentMeta
from alpha_core.helpers.config import ConfigError, load_yaml
from alpha_core.helpers.decimal_utils import floor_to_lot, quantize_to_tick

# Kite exchange -> our AssetClass (the F&O bucket maps to INDEX_OPTION).
_KITE_EXCHANGE_TO_ASSET = {
    "NSE": AssetClass.EQUITY,
    "BSE": AssetClass.EQUITY,
    "NFO": AssetClass.INDEX_OPTION,
    "BFO": AssetClass.INDEX_OPTION,
}


class InstrumentSpec(BaseModel):
    """One tradable instrument's metadata + quantization rules."""

    model_config = ConfigDict(frozen=True, extra="ignore")  # tolerate extra yaml keys
    symbol: str
    asset_class: AssetClass
    # Venues this symbol actually trades on. Empty = any venue of its asset class
    # (fine for equity, where the venue is implied). Crypto venues share the
    # CRYPTO asset class but list different symbols, so they MUST be tagged, or a
    # Delta run would subscribe to Binance-spot symbols that don't exist (P19.1).
    venues: frozenset[Venue] = frozenset()
    lot_size: Decimal = Decimal("1")
    tick_size: Decimal = Decimal("0.05")
    circuit_band_pct: Decimal | None = None
    quote_currency: str = "INR"
    instrument_token: int | None = None  # Kite ticker subscription id (G13/P13.3)
    expiry: date | None = None  # F&O contract expiry (None for cash)
    option: OptionContract | None = None  # parsed option metadata (None for non-options, ADR 0017)

    def round_quantity(self, quantity: Decimal) -> Decimal:
        """Floor ``quantity`` to a whole number of lots (never round *up* into a
        bigger position than intended). Uses the shared primitive — one rounding
        rule for the whole codebase (``decimal_utils``)."""
        return floor_to_lot(quantity, self.lot_size)

    def round_price(self, price: Decimal) -> Decimal:
        """Snap ``price`` to the nearest tick (the shared ``decimal_utils`` rule)."""
        return quantize_to_tick(price, self.tick_size)

    def cost_meta(self) -> InstrumentMeta:
        """The cost model's view of this instrument (derived, never duplicated)."""
        return InstrumentMeta(asset_class=self.asset_class, tick_size=self.tick_size)


class InstrumentRegistry:
    """The tradable universe, keyed by symbol."""

    def __init__(self, specs: dict[str, InstrumentSpec]) -> None:
        self._specs = specs

    @classmethod
    def from_config(cls) -> InstrumentRegistry:
        """Build from ``config/instruments.yaml`` (ADR 0011)."""
        raw = load_yaml("instruments.yaml").get("instruments") or {}
        specs = {
            symbol: InstrumentSpec.model_validate({"symbol": symbol, **meta})
            for symbol, meta in raw.items()
        }
        return cls(specs)

    @classmethod
    def from_kite_dump(cls, rows: list[dict[str, Any]]) -> InstrumentRegistry:
        """Build the live universe from the Kite instrument master (P13.3).

        Filters to tradable equity (``EQ``) and index options (``CE``/``PE``) on
        NSE/BSE/NFO/BFO; carries the real ``instrument_token`` (for the ticker),
        ``lot_size``, ``tick_size``, and F&O ``expiry``. Other rows (indices,
        currency/commodity, futures for now) are skipped.
        """
        specs: dict[str, InstrumentSpec] = {}
        for row in rows:
            spec = _spec_from_kite_row(row)
            if spec is not None:
                specs[spec.symbol] = spec
        return cls(specs)

    @classmethod
    def from_ccxt_markets(
        cls,
        markets: Iterable[dict[str, Any]] | dict[str, dict[str, Any]],
        venue: Venue,
    ) -> InstrumentRegistry:
        """Build an **options** registry from ccxt ``load_markets()`` output (ADR 0017
        OPT-5). Accepts the symbol→market mapping or an iterable of market dicts; keeps
        only active option markets, each parsed into an ``InstrumentSpec`` carrying its
        ``OptionContract`` and tagged with ``venue``.

        The chain is then selected with ``options.chain`` (OPT-2), polled per-contract
        by the REST adapter, and cash-settled via reconciliation — all unchanged. The
        live ``load_markets()`` call + testnet validation is the gated go-live step
        ([You] confirms Delta options availability/convention; P16.13/P16.14)."""
        rows = markets.values() if isinstance(markets, dict) else markets
        specs: dict[str, InstrumentSpec] = {}
        for market in rows:
            spec = _spec_from_ccxt_option_market(market, venue)
            if spec is not None:
                specs[spec.symbol] = spec
        return cls(specs)

    @classmethod
    def from_kite_json(cls, path: str | Path) -> InstrumentRegistry:
        """Build from a cached Kite dump written by ``scripts/refresh_instruments.py``."""
        rows = json.loads(Path(path).read_text())
        if not isinstance(rows, list):
            raise ConfigError(f"{path} must be a JSON list of Kite instrument rows")
        return cls.from_kite_dump(rows)

    def token_map(self) -> dict[str, int]:
        """``{symbol: instrument_token}`` for instruments that carry one (Kite ticker)."""
        return {
            symbol: spec.instrument_token
            for symbol, spec in self._specs.items()
            if spec.instrument_token is not None
        }

    def is_known(self, symbol: str) -> bool:
        return symbol in self._specs

    def get(self, symbol: str) -> InstrumentSpec:
        spec = self._specs.get(symbol)
        if spec is None:
            raise ConfigError(f"unknown instrument {symbol!r} (not in instruments.yaml)")
        return spec

    def cost_metas(self) -> dict[str, InstrumentMeta]:
        """The cost model / paper-broker instrument map derived from the registry."""
        return {symbol: spec.cost_meta() for symbol, spec in self._specs.items()}

    def specs(self) -> list[InstrumentSpec]:
        """All instrument specs (e.g. for the F&O contract-chain resolver)."""
        return list(self._specs.values())

    def symbols_for(
        self, asset_classes: Collection[AssetClass], venue: Venue | None = None
    ) -> list[str]:
        """The tradable universe for a run: symbols whose asset class is in the set
        AND — when ``venue`` is given — that trade on that venue. A symbol tagged
        with ``venues`` is only included for those venues; an untagged symbol
        matches any venue of its asset class (P19.1, so a Delta run never
        subscribes to Binance-spot symbols)."""
        allowed = set(asset_classes)
        out = []
        for s, spec in self._specs.items():
            if spec.asset_class not in allowed:
                continue
            if venue is not None and spec.venues and venue not in spec.venues:
                continue
            out.append(s)
        return out

    def __len__(self) -> int:
        return len(self._specs)


def _parse_expiry(value: object) -> date | None:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        return date.fromisoformat(value)
    return None


def option_contract_from_kite_row(row: dict[str, Any]) -> OptionContract | None:
    """Build an :class:`OptionContract` from a Kite instrument-master row's structured
    fields (ADR 0017) — robust, vs. fragile parsing of the tradingsymbol string.
    Returns None unless the row is a CE/PE with a strike + expiry."""
    itype = str(row.get("instrument_type", ""))
    if itype not in ("CE", "PE"):
        return None
    expiry = _parse_expiry(row.get("expiry"))
    if expiry is None or row.get("strike") in (None, "", 0):
        return None
    return OptionContract(
        underlying=str(row.get("name") or row.get("tradingsymbol", "")),
        right=OptionRight.CALL if itype == "CE" else OptionRight.PUT,
        strike=Decimal(str(row["strike"])),
        expiry=expiry,
        lot_size=int(Decimal(str(row["lot_size"]))),
        settlement=Settlement.CASH,  # NSE index options are cash-settled
    )


def _spec_from_kite_row(row: dict[str, Any]) -> InstrumentSpec | None:
    """Map one Kite instrument-dump row to an ``InstrumentSpec`` (None if unsupported)."""
    exchange = str(row.get("exchange", ""))
    asset = _KITE_EXCHANGE_TO_ASSET.get(exchange)
    if asset is None:
        return None
    itype = str(row.get("instrument_type", ""))
    if asset is AssetClass.EQUITY and itype != "EQ":
        return None
    if asset is AssetClass.INDEX_OPTION and itype not in ("CE", "PE"):
        return None  # skip futures/other for the index-option pilot
    return InstrumentSpec(
        symbol=f"{exchange}:{row['tradingsymbol']}",
        asset_class=asset,
        lot_size=Decimal(str(row["lot_size"])),
        tick_size=Decimal(str(row["tick_size"])),
        instrument_token=int(row["instrument_token"]),
        expiry=_parse_expiry(row.get("expiry")),
        option=option_contract_from_kite_row(row),
    )


def _ccxt_option_expiry(market: dict[str, Any]) -> date | None:
    """Expiry date from a ccxt option market — the ISO ``expiryDatetime`` if present,
    else the ``expiry`` epoch-ms. None if neither is valid."""
    iso = market.get("expiryDatetime")
    if isinstance(iso, str) and iso:
        try:
            return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(UTC).date()
        except ValueError:
            return None
    ms = market.get("expiry")
    if isinstance(ms, int | float) and ms > 0:
        return datetime.fromtimestamp(ms / 1000, tz=UTC).date()
    return None


def option_contract_from_ccxt_market(market: dict[str, Any]) -> OptionContract | None:
    """Build an :class:`OptionContract` from a ccxt **unified option-market** dict
    (ADR 0017 OPT-5, Delta) — the structured venue view, not symbol-string parsing.
    Returns None unless the market is an *active* option with a strike, expiry, and
    call/put type. Crypto options trade in whole contracts (lot 1); the underlying
    amount per contract is the ``contractSize`` multiplier, and they cash-settle."""
    if market.get("option") is not True or market.get("active") is False:
        return None
    opt_type = str(market.get("optionType") or "").lower()
    if opt_type not in ("call", "put"):
        return None
    expiry = _ccxt_option_expiry(market)
    strike = market.get("strike")
    underlying = market.get("base")
    if expiry is None or strike in (None, "", 0) or not underlying:
        return None
    contract_size = market.get("contractSize")
    return OptionContract(
        underlying=str(underlying),
        right=OptionRight.CALL if opt_type == "call" else OptionRight.PUT,
        strike=Decimal(str(strike)),
        expiry=expiry,
        lot_size=1,  # crypto options trade in whole contracts; size lives in multiplier
        settlement=Settlement.CASH,  # crypto options cash-settle (USD/USDT)
        multiplier=Decimal(str(contract_size)) if contract_size else Decimal(1),
    )


def _spec_from_ccxt_option_market(market: dict[str, Any], venue: Venue) -> InstrumentSpec | None:
    """Map one ccxt option market to an ``InstrumentSpec`` (None if not a valid option).

    ``lot_size``/``tick_size`` are taken from the market's amount/price precision
    (best-effort; the exact Delta tick is a [You] go-live verification — R3-x)."""
    option = option_contract_from_ccxt_market(market)
    symbol = market.get("symbol")
    if option is None or not symbol:
        return None
    precision = market.get("precision") or {}
    amount_limits = (market.get("limits") or {}).get("amount") or {}
    lot = amount_limits.get("min") or precision.get("amount") or 1
    tick = precision.get("price") or "0.01"
    return InstrumentSpec(
        symbol=str(symbol),
        asset_class=AssetClass.INDEX_OPTION,  # the options segment (NSE + crypto, one path)
        venues=frozenset({venue}),
        lot_size=Decimal(str(lot)),
        tick_size=Decimal(str(tick)),
        expiry=option.expiry,
        option=option,
    )
