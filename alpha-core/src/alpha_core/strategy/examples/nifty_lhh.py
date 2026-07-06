"""NIFTY last-half-hour intraday momentum, optionally NGE-gated (the Baltussen form —
`docs/research/intraday-edge-survey-2026-07.md` §5's F1 conditioning axis, its own family).

The literature's cleanest intraday-momentum statement (Gao et al.; Baltussen-van Bekkum-Da):
the day-so-far return predicts the last half hour, and the effect concentrates on days when
option dealers are net SHORT gamma (their delta hedging trades WITH the market into the
close). Registered form: at the ``signal_time`` close (14:35 IST), enter in the direction of
``close/day_open - 1``; exit flat at ``flat_time`` (15:05 IST, before the 15:10 no-new-entry
gate). One entry per day, no stop (the engine risk gate stands), MIS-REQUIRED like the noise
family: on gap days the engine's flatteners own the book and the strategy resets silently.

Conditioning (the searched axis): ``0`` — unconditioned; ``1`` — enter ONLY when the
injected NGE sign for TODAY is negative (short-gamma days). The sign map is built by
``research/nge.py`` and is ALREADY shifted (day D's key = sign of the previous session's
chain — the look-ahead fence lives in the pipeline, not here); a day missing from the map
yields no conditioned trade (no data = no signal, conservative). The map is injected at
CONSTRUCTION (the template factory closes over it) — proposal params stay tiny and
fingerprintable; the series' provenance is pinned by the driver at run time.

A dead-flat day (``close == day_open`` at the signal instant) has no direction — no trade.
Tunables: ``config/strategies/nifty_lhh_momentum.yaml``; the vetted (2-config, exhaustive)
space lives in ``research/nge_templates.py``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, time, timedelta, timezone
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from alpha_core.core.enums import OrderType, Side
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Signal, Tick
from alpha_core.helpers.config import load_yaml

_IST = timezone(timedelta(hours=5, minutes=30))  # fixed offset — IST has no DST


class NiftyLhhMomentumConfig(BaseModel):
    """Last-half-hour momentum tunables (``config/strategies/nifty_lhh_momentum.yaml``)."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = "nifty_lhh_momentum"
    conditioning: int = 0  # 0 = none, 1 = short-gamma days only (searched: {0, 1})
    signal_time: str = "14:35"  # IST close instant the day-so-far sign is read (family-fixed)
    flat_time: str = "15:05"  # IST: exit at the first processed close >= this (family-fixed)
    quantity: Decimal = Decimal("20")  # fixed units — the F1/F2 constant-quantity idiom


@dataclass
class _DayState:
    ist_date: date
    day_open: Decimal | None = None
    entered_today: bool = False


class NiftyLhhMomentum(Strategy):
    """Last-half-hour momentum with optional short-gamma gating (module docstring)."""

    def __init__(
        self,
        config: NiftyLhhMomentumConfig | None = None,
        *,
        nge_sign_for_day: Mapping[str, int] | None = None,
    ) -> None:
        self._cfg = config or NiftyLhhMomentumConfig()
        if self._cfg.conditioning not in (0, 1):
            raise ValueError("conditioning must be 0 (none) or 1 (short-gamma days only)")
        self._signal_time = time.fromisoformat(self._cfg.signal_time)
        self._flat_time = time.fromisoformat(self._cfg.flat_time)
        if self._signal_time >= self._flat_time:
            raise ValueError("signal_time must be before flat_time")
        if self._cfg.conditioning == 1 and nge_sign_for_day is None:
            raise ValueError("conditioning=1 requires an injected nge_sign_for_day map")
        self._nge = nge_sign_for_day or {}
        self._day: _DayState | None = None
        self._side: Side | None = None

    @classmethod
    def from_config(cls) -> NiftyLhhMomentum:
        # Deployment builder (registry): the UNCONDITIONED form only — a conditioned
        # deployment must be wired with a live NGE feed, which does not exist yet
        # (a research-plane artifact today; the constructor enforces this).
        raw = load_yaml("strategies/nifty_lhh_momentum.yaml")
        return cls(NiftyLhhMomentumConfig.model_validate(raw))

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        now_ist = (bar.start + bar.interval).astimezone(_IST)

        # IST rollover: reset; a still-tracked side means a gap day — the ENGINE's
        # flatteners own that book (MIS-required); reset silently, never emit (#179 F2).
        if self._day is None or self._day.ist_date != now_ist.date():
            self._day = _DayState(ist_date=now_ist.date())
            self._side = None
        if self._day.day_open is None:
            self._day.day_open = bar.open

        if now_ist.time() >= self._flat_time:
            if self._side is not None:
                return [self._exit(bar)]
            return []

        if self._side is not None or self._day.entered_today or now_ist.time() < self._signal_time:
            return []
        self._day.entered_today = True  # one signal read per day, trade or not
        if self._cfg.conditioning == 1 and self._nge.get(now_ist.date().isoformat()) != -1:
            return []  # not a (known) short-gamma day — stand down
        if bar.close == self._day.day_open:
            return []  # dead-flat day-so-far: no direction
        direction = Side.BUY if bar.close > self._day.day_open else Side.SELL
        self._side = direction
        return [self._signal(bar, direction, "last-half-hour momentum", Decimal(1))]

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []

    def _exit(self, bar: Bar) -> Signal:
        assert self._side is not None
        exit_side = Side.SELL if self._side is Side.BUY else Side.BUY
        self._side = None
        return self._signal(bar, exit_side, "EOD flat", None)

    def _signal(self, bar: Bar, side: Side, reason: str, score: Decimal | None) -> Signal:
        return Signal(
            strategy_id=self._cfg.strategy_id,
            symbol=bar.symbol,
            asset_class=bar.asset_class,
            side=side,
            quantity=self._cfg.quantity,
            order_type=OrderType.MARKET,
            created_at=bar.start + bar.interval,
            reason=reason,
            score=score,
        )
