"""Kite Connect data-plane transforms -> core Bars + the daily-token staleness check (M3.0).

Pure, SDK-free helpers for the Indian-equities (1-minute+) leg: ``candles_to_bars`` (the
``KiteConnect.historical_data()`` OHLCV rows) and ``access_token_is_stale`` (the daily-token
rollover predicate). The **SDK-using** parts — the 2FA reauth flow (``login_url`` +
``generate_session``), the ``.env`` token persistence, and the network ``historical_data`` fetch —
live in ``scripts/ingest_kite.py``, never here (same split as ``binance.py`` / ``yahoo.py``: no
broker SDK in the kernel). The staleness math is pure (a fixed +05:30 IST offset + an injected
``now``), so it is unit-tested in CI beside the candle transform. Kite timestamps are **IST**
(``Asia/Kolkata``); they are converted to tz-aware **UTC** so the cold store is uniform (the engine
never reads a non-UTC time). The caller passes the internal store symbol (e.g. ``NSE:RELIANCE``) and
the bar interval in seconds (60 for ``minute``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar


def candles_to_bars(
    candles: Sequence[Mapping[str, Any]], *, symbol: str, interval_seconds: int
) -> list[Bar]:
    """``KiteConnect.historical_data()`` rows -> ``Bar``s for ``symbol`` (NSE equity).

    Each row is a mapping ``{date, open, high, low, close, volume}`` — ``date`` a tz-aware datetime
    (the SDK returns IST). Rows that fail ``Bar``'s price contract are skipped: a missing OHLCV
    field, a **non-positive price** (all-zero glitch rows — seen live: TCS, 2015-era), or
    **incoherent OHLC** (``low`` above / ``high`` below the other prices — seen live: HDFCBANK,
    same archive). Such rows carry no trustworthy price — which field is the glitch is unknowable —
    so skipping leaves an honest gap, never a fabricated or "repaired" price. The skip predicate
    mirrors ``Bar``'s own validators exactly, so no further *price-shaped* glitch can crash a
    multi-hour pull, while anything outside that contract still fails loud. A **non-empty input
    yielding zero bars raises**: an all-garbage batch is feed garbage (or a wrong-instrument read),
    and silently returning "no data" would surface as a confusing too-few-bars error far downstream
    (the fail-loud ingest norm). ``interval_seconds`` sets ``Bar.interval`` (60 for ``minute``).
    """
    interval = timedelta(seconds=interval_seconds)
    bars: list[Bar] = []
    for c in candles:
        ts, o, h, low, close, v = (
            c.get("date"),
            c.get("open"),
            c.get("high"),
            c.get("low"),
            c.get("close"),
            c.get("volume"),
        )
        if ts is None or o is None or h is None or low is None or close is None or v is None:
            continue  # missing OHLCV field (also narrows each field for the price guard below)
        if not isinstance(ts, datetime):
            raise TypeError(f"Kite candle 'date' must be a datetime, got {type(ts).__name__}")
        if ts.tzinfo is None:
            raise ValueError(f"Kite candle 'date' must be tz-aware (IST); got naive {ts!r}")
        if o <= 0 or h <= 0 or low <= 0 or close <= 0:
            continue  # zero-price glitch row — no price information, skip (see docstring)
        if h < max(o, low, close) or low > min(o, h, close):
            continue  # OHLC-incoherent glitch row — mirrors Bar's coherence validator
        bars.append(
            Bar(
                symbol=symbol,
                venue=Venue.NSE,
                asset_class=AssetClass.EQUITY,
                start=ts.astimezone(UTC),  # IST -> UTC; the store/engine is uniform UTC
                interval=interval,
                open=Decimal(str(o)),
                high=Decimal(str(h)),
                low=Decimal(str(low)),
                close=Decimal(str(close)),
                volume=Decimal(str(v)),  # NSE volume is whole shares (Kite gives an int)
            )
        )
    if candles and not bars:
        raise ValueError(
            f"kite ingest: all {len(candles)} candle rows for {symbol} were unusable "
            "(missing fields, non-positive prices, or incoherent OHLC) — feed garbage "
            "or a wrong-instrument read"
        )
    return bars


# Kite Connect access tokens are issued per login and expire at ~06:00 IST the next morning
# (Zerodha's documented daily invalidation); a fresh one needs an interactive 2FA login. IST has no
# DST, so a fixed +05:30 offset is exact (no tzdata dependency in the kernel).
_IST = timezone(timedelta(hours=5, minutes=30))
KITE_TOKEN_EXPIRY_HOUR_IST = 6  # the daily ~06:00 IST rollover after which a token is dead


def access_token_is_stale(token_at: datetime, *, now: datetime) -> bool:
    """Has the daily Kite token rolled over since it was minted? (pure, SDK-free, no I/O).

    A token minted at ``token_at`` is valid until the first 06:00 IST that falls after it; once an
    06:00-IST boundary lies in ``(token_at, now]`` the token is stale and the ingest script must
    re-run the 2FA login (the human) + ``generate_session`` exchange (the script). Both datetimes
    must be tz-aware — ``now`` is *injected*, never read from the wall clock here (the engine
    invariant). Erring toward *stale* is the safe direction: a spurious re-auth costs one login,
    while a stale token used against the API just errors.
    """
    if token_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("access_token_is_stale needs tz-aware datetimes (token_at, now)")
    now_ist = now.astimezone(_IST)
    boundary = now_ist.replace(hour=KITE_TOKEN_EXPIRY_HOUR_IST, minute=0, second=0, microsecond=0)
    if now_ist < boundary:  # before today's 06:00 IST -> the live expiry boundary is yesterday's
        boundary -= timedelta(days=1)
    return token_at < boundary
