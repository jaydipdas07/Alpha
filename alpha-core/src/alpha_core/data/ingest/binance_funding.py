"""Pure transform: Binance ``/fapi/v1/fundingRate`` rows -> ``FundingRate`` records (M3.0).

No SDK, no network — the tested part of the funding ingest (the network glue is
``scripts/ingest_funding.py``), mirroring ``data/ingest/binance.py``. Each Binance row carries a
``fundingTime`` (epoch ms) and a ``fundingRate`` (a signed decimal string). The rate is parsed via
``str`` so the signed ``Decimal`` is exact (never through a float); the time is tz-aware UTC.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from alpha_core.core.enums import Venue
from alpha_core.data.funding import FundingRate


def funding_rows_to_rates(rows: Sequence[dict[str, Any]], *, symbol: str) -> list[FundingRate]:
    """Map Binance funding-rate rows to ``FundingRate`` records for ``symbol`` (venue BINANCE).

    ``symbol`` is passed in (not read from the row) so the store key matches the rest of the cold
    store; an empty page maps to ``[]``."""
    return [
        FundingRate(
            symbol=symbol,
            venue=Venue.BINANCE,
            funding_time=datetime.fromtimestamp(int(row["fundingTime"]) / 1000, tz=UTC),
            rate=Decimal(str(row["fundingRate"])),
        )
        for row in rows
    ]
