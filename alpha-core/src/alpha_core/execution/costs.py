"""Transaction cost & slippage model (ADR 0008).

A pure model: given an order's side/qty/price context, return the exact all-in
cost as a per-component ``CostBreakdown``. Price-taker (cross the full spread,
never mid), plus a per-side slippage buffer, plus the per-segment Indian cost
stack (brokerage, STT, exchange txn, GST, SEBI, stamp duty) and the crypto 1%
TDS. All ``Decimal``; no I/O. Parameters come from ``config/costs.yaml`` — no
magic numbers. The 2x slippage stress test is the ``stress`` flag.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from alpha_core.core.enums import AssetClass, Side

# Maps an asset class to its slippage key and cost-stack segment key in costs.yaml.
_SLIPPAGE_KEY = {
    AssetClass.EQUITY: "equity",
    AssetClass.CRYPTO: "crypto",
    AssetClass.INDEX_OPTION: "index_option",
}
_SEGMENT_KEY = {
    AssetClass.EQUITY: "equity_intraday",
    AssetClass.CRYPTO: "crypto",
    AssetClass.INDEX_OPTION: "index_option",
}
_BPS = Decimal(10000)
_TWO = Decimal(2)


class CostModelError(Exception):
    """Raised when cost inputs/config are insufficient (fail fast)."""


class InstrumentMeta(BaseModel):
    """Minimal instrument metadata the cost model needs (subset of instruments.yaml)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    asset_class: AssetClass
    tick_size: Decimal | None = None  # required for tick-denominated slippage


class CostBreakdown(BaseModel):
    """The all-in cost decomposition for one order (quote currency)."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    effective_fill_price: Decimal
    spread_cost: Decimal
    slippage_cost: Decimal
    brokerage: Decimal
    stt: Decimal
    exchange_txn: Decimal
    gst: Decimal
    sebi: Decimal
    stamp_duty: Decimal
    tds: Decimal
    total: Decimal


def _d(value: object) -> Decimal:
    """Coerce a config rate constant to Decimal.

    Config rates come from yaml and are parsed as ``float``; converting via
    ``str`` makes them exact (e.g. 0.0003503 -> Decimal("0.0003503")) without
    float drift. This is config parsing, not money arithmetic.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int | str):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    raise CostModelError(f"non-numeric cost parameter: {value!r}")


class CostModel:
    """Computes all-in costs from the parsed ``costs.yaml`` mapping."""

    def __init__(self, config: dict[str, Any]) -> None:
        self._slip = config["slippage"]
        self._segments = config["segments"]
        self._stress_multiplier = _d(self._slip.get("stress_multiplier", 2))

    # --- price-taker fill + slippage ------------------------------------------

    def _base_and_spread_cost(
        self,
        side: Side,
        quantity: Decimal,
        asset_class: AssetClass,
        bid: Decimal | None,
        ask: Decimal | None,
        ltp: Decimal | None,
        meta: InstrumentMeta,
    ) -> tuple[Decimal, Decimal]:
        """Touch price (crossing the full spread) and the spread cost vs mid."""
        sign = Decimal(1) if side is Side.BUY else Decimal(-1)
        if bid is not None and ask is not None:
            mid = (bid + ask) / _TWO
            base = ask if side is Side.BUY else bid
            return base, abs(base - mid) * quantity
        if ltp is None:
            raise CostModelError("need a bid/ask quote or an LTP — never assume mid")
        ds = self._slip["default_spread"]
        if asset_class is AssetClass.INDEX_OPTION:
            if meta.tick_size is None:
                raise CostModelError("index option needs tick_size for default spread")
            full_spread = _d(ds["index_option_ticks"]) * meta.tick_size
        else:
            full_spread = ltp * _d(ds[_SLIPPAGE_KEY[asset_class]])
        half = full_spread / _TWO
        base = ltp + sign * half
        return base, half * quantity

    def _slippage_buffer(
        self, base: Decimal, asset_class: AssetClass, meta: InstrumentMeta, stress: bool
    ) -> Decimal:
        """Per-unit adverse slippage buffer (bps or ticks)."""
        spec = self._slip[_SLIPPAGE_KEY[asset_class]]
        if spec["type"] == "bps":
            buffer = base * _d(spec["value"]) / _BPS
        elif spec["type"] == "ticks":
            if meta.tick_size is None:
                raise CostModelError("tick-denominated slippage needs tick_size")
            buffer = _d(spec["value"]) * meta.tick_size
        else:
            raise CostModelError(f"unknown slippage type: {spec['type']}")
        return buffer * self._stress_multiplier if stress else buffer

    # --- Indian cost stack -----------------------------------------------------

    @staticmethod
    def _component(spec: dict[str, Any], side: Side, value: Decimal) -> Decimal:
        """One fee component on turnover ``value``, honoring side and mode."""
        comp_side = spec.get("side", "both")
        if comp_side != "both" and comp_side != side.value.lower():
            return Decimal(0)
        mode: Literal["pct", "flat", "min"] = spec.get("mode", "pct")
        pct = _d(spec.get("pct", 0)) * value
        flat = _d(spec.get("flat", 0))
        if mode == "flat":
            return flat
        if mode == "min":
            return min(pct, flat)
        return pct

    def _stack(self, asset_class: AssetClass, side: Side, value: Decimal) -> dict[str, Decimal]:
        seg = self._segments[_SEGMENT_KEY[asset_class]]
        out = {
            "brokerage": Decimal(0),
            "stt": Decimal(0),
            "exchange_txn": Decimal(0),
            "sebi": Decimal(0),
            "stamp_duty": Decimal(0),
            "gst": Decimal(0),
            "tds": Decimal(0),
        }
        # crypto uses trading_fee in place of brokerage
        if "trading_fee" in seg:
            out["brokerage"] = self._component(seg["trading_fee"], side, value)
        for key in ("brokerage", "stt", "exchange_txn", "sebi", "stamp_duty", "tds"):
            if key in seg:
                out[key] = self._component(seg[key], side, value)
        if "gst" in seg:
            base = sum((out[k] for k in seg["gst"].get("on", [])), Decimal(0))
            out["gst"] = _d(seg["gst"]["pct"]) * base
        return out

    # --- public API ------------------------------------------------------------

    def estimate(
        self,
        *,
        side: Side,
        quantity: Decimal,
        bid: Decimal | None = None,
        ask: Decimal | None = None,
        ltp: Decimal | None = None,
        instrument: InstrumentMeta,
        stress: bool = False,
    ) -> CostBreakdown:
        """Return the all-in cost breakdown for an order (pure)."""
        if quantity <= 0:
            raise CostModelError("quantity must be positive")
        ac = instrument.asset_class
        base, spread_cost = self._base_and_spread_cost(
            side, quantity, ac, bid, ask, ltp, instrument
        )
        buffer = self._slippage_buffer(base, ac, instrument, stress)
        sign = Decimal(1) if side is Side.BUY else Decimal(-1)
        effective = base + sign * buffer
        slippage_cost = buffer * quantity
        value = quantity * effective
        stack = self._stack(ac, side, value)
        total = spread_cost + slippage_cost + sum(stack.values(), Decimal(0))
        return CostBreakdown(
            effective_fill_price=effective,
            spread_cost=spread_cost,
            slippage_cost=slippage_cost,
            brokerage=stack["brokerage"],
            stt=stack["stt"],
            exchange_txn=stack["exchange_txn"],
            gst=stack["gst"],
            sebi=stack["sebi"],
            stamp_duty=stack["stamp_duty"],
            tds=stack["tds"],
            total=total,
        )
