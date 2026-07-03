"""Option-chain selection (ADR 0017, OPT-2) — pick tradable contracts from the registry.

A pure filter over ``InstrumentSpec``s (the venue's instrument master, already
parsed): underlying + expiry window + right + strike band. Strategies/loops call
this instead of parsing venue symbols (the core never parses a venue string);
whatever it returns is quantized/gated by the same registry + risk machinery as
any other instrument. ``reference`` (the caller's injected today) anchors DTE.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal

from alpha_core.core.enums import OptionRight
from alpha_core.execution.instruments import InstrumentSpec


def select_chain(
    specs: Iterable[InstrumentSpec],
    underlying: str,
    *,
    reference: date,
    min_dte: int = 0,
    max_dte: int,
    rights: Sequence[OptionRight] | None = None,
    strike_lo: Decimal | None = None,
    strike_hi: Decimal | None = None,
) -> list[InstrumentSpec]:
    """The contracts of ``underlying`` expiring within ``[min_dte, max_dte]`` calendar
    days of ``reference``, optionally filtered by right and strike band — sorted by
    (expiry, strike, right) so selection is deterministic for a given master."""
    if min_dte < 0 or max_dte < min_dte:
        raise ValueError("require 0 <= min_dte <= max_dte")
    wanted = set(rights) if rights is not None else None
    picked: list[InstrumentSpec] = []
    for spec in specs:
        contract = spec.option
        if contract is None or contract.underlying != underlying:
            continue
        dte = (contract.expiry - reference).days
        if not min_dte <= dte <= max_dte:
            continue
        if wanted is not None and contract.right not in wanted:
            continue
        if strike_lo is not None and contract.strike < strike_lo:
            continue
        if strike_hi is not None and contract.strike > strike_hi:
            continue
        picked.append(spec)
    picked.sort(key=lambda s: (s.option.expiry, s.option.strike, s.option.right.value))  # type: ignore[union-attr]
    return picked


def nearest_expiry(
    specs: Iterable[InstrumentSpec], underlying: str, *, reference: date, min_dte: int = 0
) -> date | None:
    """The chain's earliest expiry at least ``min_dte`` days out, or None."""
    expiries = {
        s.option.expiry
        for s in specs
        if s.option is not None
        and s.option.underlying == underlying
        and (s.option.expiry - reference).days >= min_dte
    }
    return min(expiries) if expiries else None
