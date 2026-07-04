"""The funding-window family — vectorized 1m folds around perp funding settlements (the
intraday-mandate family FW, `docs/research/intraday-edge-survey-2026-07.md` §2.4/§5).

Hypothesis (practitioner-documented): positions get adjusted around the 8-hourly funding
settlement — when funding is meaningfully positive, longs derisk INTO the timestamp (price
drifts down) and the pressure lifts AFTER it (price rebounds); symmetrically for negative
funding. Two pre-registered templates:

- ``funding_pre_drift`` — fade the payers into settlement: hold ``-sign(prev settled rate)``
  over ``[ts - pre_minutes, ts)`` when ``|prev rate| >= min_funding_bps``. **Look-ahead
  discipline:** the rate settling AT ``ts`` is only final at ``ts``, so the pre-window may
  condition ONLY on the PREVIOUS settlement's rate (known 8h earlier; funding is highly
  autocorrelated — that is the tradeable form of the hypothesis).
- ``funding_rebound`` — ride the post-settlement relief: hold ``+sign(rate settled at ts)``
  over ``(ts + 1m, ts + post_minutes]`` when ``|rate| >= min_funding_bps``. The rate IS final
  at ``ts``; entry is deferred one full 1m bar past ``ts`` (live would enter seconds after).

Fold mechanics (float statistics plane; money never feeds back): per-bar close-to-close
returns x a {-1,0,+1} position series built from the funding events; taker costs charged per
side at each window's entry and exit bars (``costs.yaml`` ``crypto_perp`` trading_fee +
slippage — no magic numbers). Windows never overlap (max 60m vs 8h cadence). The returns
series spans every bar (zeros outside windows), so the quant-analyst's OOS slicing sees
calendar time, not cherry-picked windows.

TEST-3 wiring: ``build_funding_window_backtesters`` returns an (in-sample, holdout) pair —
in-sample reads the sealed research ``BarStore``, holdout reads the gate-only ``HoldoutStore``
— the same one-tested-place pattern as ``build_panel_backtesters``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import numpy as np
from pydantic import BaseModel, ConfigDict

from alpha_core.core.enums import Venue
from alpha_core.core.models import Bar
from alpha_core.data.funding import FundingStore
from alpha_core.data.holdout import HoldoutStore
from alpha_core.data.store import BarStore
from alpha_core.helpers.config import load_yaml
from alpha_core.research.discovery import Backtester
from alpha_core.research.strategist import DecimalRange, StrategyProposal, StrategyTemplate

# The pre-registered universe: liquid USDT perps with NO prior minute-scale seal (the 8 majors'
# 1m boundaries are floored at ~2026-06 from the M3.0 sweeps — a 6-day holdout is no gate), all
# panel members with full funding coverage. window label -> Binance symbol.
FUNDING_WINDOW_CELLS: dict[str, str] = {
    "linkusdt-1m-fw": "LINKUSDT",
    "ltcusdt-1m-fw": "LTCUSDT",
    "bchusdt-1m-fw": "BCHUSDT",
    "etcusdt-1m-fw": "ETCUSDT",
    "filusdt-1m-fw": "FILUSDT",
    "atomusdt-1m-fw": "ATOMUSDT",
}
_INTERVAL_S = 60


def taker_cost_per_side() -> float:
    """Per-side taker cost fraction from ``costs.yaml`` (crypto_perp trading_fee + bps
    slippage) — one config home, no magic numbers."""
    cfg = load_yaml("costs.yaml")
    segment = cast(dict[str, dict[str, object]], cfg["segments"])["crypto_perp"]
    trading = cast(dict[str, object], segment["trading_fee"])
    slippage = cast(dict[str, dict[str, object]], cfg["slippage"])["crypto_perp"]
    return float(cast(float, trading["pct"])) + float(cast(int, slippage["value"])) / 10_000.0


class FundingPreDriftConfig(BaseModel):
    """Fade-into-settlement tunables (vetted ranges in ``FUNDING_WINDOW_TEMPLATES``)."""

    model_config = ConfigDict(extra="forbid")
    pre_minutes: int = 60
    min_funding_bps: Decimal = Decimal("1")


class FundingReboundConfig(BaseModel):
    """Post-settlement relief tunables (vetted ranges in ``FUNDING_WINDOW_TEMPLATES``)."""

    model_config = ConfigDict(extra="forbid")
    post_minutes: int = 60
    min_funding_bps: Decimal = Decimal("1")


class _PreDriftSpec:
    """Validate-by-construction (the ``StrategyTemplate.build`` contract)."""

    def __init__(self, config: FundingPreDriftConfig) -> None:
        if not 1 <= config.pre_minutes <= 240:
            raise ValueError("pre_minutes must be in [1, 240]")
        if config.min_funding_bps <= 0:
            raise ValueError("min_funding_bps must be positive")
        self.config = config


class _ReboundSpec:
    def __init__(self, config: FundingReboundConfig) -> None:
        if not 1 <= config.post_minutes <= 240:
            raise ValueError("post_minutes must be in [1, 240]")
        if config.min_funding_bps <= 0:
            raise ValueError("min_funding_bps must be positive")
        self.config = config


FUNDING_WINDOW_TEMPLATES: dict[str, StrategyTemplate] = {
    # Grids are the pre-registration: {30,60} minutes x {1,3} bps-per-8h — 4 configs each,
    # 8 per cell, 48 across the 6-cell universe. Widening = a NEW pre-registration.
    "funding_pre_drift": StrategyTemplate(
        "funding_pre_drift",
        "funding_pre_drift",
        FundingPreDriftConfig,
        _PreDriftSpec,
        {
            "pre_minutes": DecimalRange(Decimal("30"), Decimal("60"), Decimal("30")),
            "min_funding_bps": DecimalRange(Decimal("1"), Decimal("3"), Decimal("2")),
        },
    ),
    "funding_rebound": StrategyTemplate(
        "funding_rebound",
        "funding_rebound",
        FundingReboundConfig,
        _ReboundSpec,
        {
            "post_minutes": DecimalRange(Decimal("30"), Decimal("60"), Decimal("30")),
            "min_funding_bps": DecimalRange(Decimal("1"), Decimal("3"), Decimal("2")),
        },
    ),
}

# bars provider: symbol -> time-ordered 1m Bars (research or holdout — injected, TEST-3).
BarsProvider = Callable[[str], list[Bar]]


def _arrays(bars: list[Bar]) -> tuple[np.ndarray, np.ndarray]:
    """(epoch-seconds int64, close float64) from time-ordered bars."""
    ts = np.array([int(b.start.timestamp()) for b in bars], dtype=np.int64)
    close = np.array([float(b.close) for b in bars], dtype=np.float64)
    return ts, close


class FundingWindowBacktester:
    """A ``discovery.Backtester`` running the funding-window fold on one store side."""

    def __init__(self, bars_provider: BarsProvider, funding: FundingStore) -> None:
        self._bars_provider = bars_provider
        self._funding = funding
        self._cost_side = taker_cost_per_side()
        self._cache: dict[str, tuple[np.ndarray, np.ndarray]] = {}

    def _series(self, symbol: str) -> tuple[np.ndarray, np.ndarray]:
        if symbol not in self._cache:
            bars = self._bars_provider(symbol)
            if len(bars) < 3:
                raise ValueError(f"funding-window fold: too few bars for {symbol} ({len(bars)})")
            self._cache[symbol] = _arrays(bars)
        return self._cache[symbol]

    def run(self, proposal: StrategyProposal) -> Sequence[float]:
        symbol = FUNDING_WINDOW_CELLS.get(proposal.window)
        if symbol is None:
            raise ValueError(
                f"unknown funding-window cell {proposal.window!r}; "
                f"known: {sorted(FUNDING_WINDOW_CELLS)}"
            )
        template = FUNDING_WINDOW_TEMPLATES[proposal.template]
        spec = template.build(proposal.params)
        ts, close = self._series(symbol)
        t_lo = datetime.fromtimestamp(int(ts[0]), tz=UTC)
        t_hi = datetime.fromtimestamp(int(ts[-1]) + _INTERVAL_S, tz=UTC)
        rates = self._funding.read(symbol=symbol, venue=Venue.BINANCE, start=t_lo, end=t_hi)
        events = np.array([int(r.funding_time.timestamp()) for r in rates], dtype=np.int64)
        values = np.array([float(r.rate) for r in rates], dtype=np.float64)

        if isinstance(spec, _PreDriftSpec):
            width_s = spec.config.pre_minutes * 60
            min_rate = float(spec.config.min_funding_bps) / 10_000.0
            # condition on the PREVIOUS settlement's rate (the one known before the window)
            cond = values[:-1]
            anchor = events[1:]  # windows end at the NEXT settlement
            starts, ends = anchor - width_s, anchor
            sides = -np.sign(cond)
            keep = np.abs(cond) >= min_rate
        else:
            rebound = cast(_ReboundSpec, spec)
            width_s = rebound.config.post_minutes * 60
            min_rate = float(rebound.config.min_funding_bps) / 10_000.0
            # the rate settled AT ts is known at ts; enter one full 1m bar later
            starts, ends = events + _INTERVAL_S, events + _INTERVAL_S + width_s
            sides = np.sign(values)
            keep = np.abs(values) >= min_rate

        starts, ends, sides = starts[keep], ends[keep], sides[keep]
        # per-bar simple returns; bar i's return accrues to a position held over (i-1 -> i)
        rets = np.zeros_like(close)
        rets[1:] = close[1:] / close[:-1] - 1.0
        pos = np.zeros_like(close)
        cost = np.zeros_like(close)
        n = len(ts)
        for s, e, side in zip(starts, ends, sides, strict=True):
            if side == 0.0:
                continue
            a = int(np.searchsorted(ts, s, side="left"))  # first bar with start >= window open
            b = int(np.searchsorted(ts, e, side="left"))  # first bar with start >= window close
            a = max(a, 1)  # bar 0 has no return
            if b - a < 1 or a >= n:
                continue  # window falls in a data gap — no phantom trade
            # entry reference is close[a-1]: if that bar sits across a gap, the gap jump would
            # be booked as (phantom) window P&L — require a fresh reference or skip the window.
            if ts[a] - ts[a - 1] > 3 * _INTERVAL_S:
                continue
            pos[a:b] = side
            cost[a] += self._cost_side  # entry at close[a-1]
            cost[b - 1] += self._cost_side  # exit at close[b-1]
        strat = pos * rets - cost
        return cast(Sequence[float], strat)


def build_funding_window_backtesters(
    *,
    research_store: BarStore,
    holdout_store: HoldoutStore,
    funding_store: FundingStore,
) -> tuple[Backtester, Backtester]:
    """The TEST-3-critical pair: in-sample <- sealed research store; holdout <- the gate-only
    ``HoldoutStore`` (one tested place, the ``build_panel_backtesters`` pattern)."""

    def _research(symbol: str) -> list[Bar]:
        return research_store.read_bars(
            symbol=symbol, venue=Venue.BINANCE, interval_seconds=_INTERVAL_S
        )

    def _holdout(symbol: str) -> list[Bar]:
        return holdout_store.read_holdout(
            symbol=symbol, venue=Venue.BINANCE, interval_seconds=_INTERVAL_S
        )

    return (
        FundingWindowBacktester(_research, funding_store),
        FundingWindowBacktester(_holdout, funding_store),
    )
