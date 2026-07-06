"""NIFTY intraday noise-area breakout (F1, the intraday-mandate headline family —
`docs/research/intraday-edge-survey-2026-07.md` §5).

The Zarattini/Concretum "noise area" design adapted to the NSE session: a day's move only
counts as information once it exceeds the *typical* move for that time of day — the noise
boundary. Per check instant (each HH:00 / HH:30 IST bar close), the boundary is the trailing
``lookback_days``-day average of ``|close(t) / day_open - 1|`` at that same time of day; a
close beyond ``day_open x (1 ± boundary)`` enters WITH the break (long above, short below).
One entry per day, exit flat at ``flat_time`` (EOD flat — the survey-registered form; no
intraday stop knob), everything else is the engine's risk gate.

Conditioning (the searched variant axis, survey-pinned): ``conditioning=0`` — none;
``conditioning=1`` — the Gao first-half-hour sign gate: entries must AGREE with the sign of
the 09:15→``fhh_end`` return (documented first-half-hour → rest-of-day momentum); before
``fhh_end`` no conditioned entry can fire, and a dead-flat first half hour yields no entries
that day. (The NGE/gamma-sign variant from the survey needs the EOD-chain gamma pipeline —
a FUTURE pre-registration with its own trials ledger, deliberately not a knob here.)

Time discipline: decisions at ``bar.start + bar.interval`` only (bar-time, TEST-1); IST is a
fixed +05:30 offset (no tzdata in the kernel — the ``data.ingest.kite`` precedent). The noise
history for a check instant is updated AFTER today's decision at that instant, so a day's own
move never sits inside the boundary that judges it (no self-reference). Day state resets on
the IST date rollover; a position still open at rollover (a tape gap swallowed the EOD-flat
bar AND the engine square-off) is flattened on the new day's first processed bar — never a
silent overnight ride.

Session-tail honesty: ``flat_time`` (default 15:05 IST) sits BEFORE the segment's
``no_new_entry_time`` (15:10, ``instruments.yaml``), so the exit lands on a bar the SF4
session gate still shows the strategy; the fold runs ``intraday_square_off=True`` (15:15) as
the deployment-mirroring backstop. Tunables: ``config/strategies/nifty_noise_breakout.yaml``;
the vetted (tiny, exhaustive) discovery ranges live in ``research/f1_templates.py``.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from alpha_core.core.enums import OrderType, Side
from alpha_core.core.interfaces import Strategy
from alpha_core.core.models import Bar, Signal, Tick
from alpha_core.helpers.config import load_yaml

_IST = timezone(timedelta(hours=5, minutes=30))  # fixed offset — IST has no DST


class NiftyNoiseBreakoutConfig(BaseModel):
    """Noise-area breakout tunables (``config/strategies/nifty_noise_breakout.yaml``)."""

    model_config = ConfigDict(extra="forbid")
    strategy_id: str = "nifty_noise_breakout"
    lookback_days: int = 14  # trailing days in the noise boundary (searched: {14, 90})
    conditioning: int = 0  # 0 = none, 1 = first-half-hour sign gate (searched: {0, 1})
    fhh_end: str = "09:45"  # IST close instant defining the first half hour (family-fixed)
    flat_time: str = "15:05"  # IST: exit at the first processed close >= this (family-fixed)
    quantity: Decimal = Decimal("20")  # fixed shares/units — the F2 constant-quantity idiom


@dataclass
class _DayState:
    ist_date: date
    day_open: Decimal | None = None  # the day's first-seen bar OPEN (the 09:15 auction print)
    fhh_sign: int | None = None  # sign of the 09:15->fhh_end return; None until known
    entered_today: bool = False


@dataclass
class _State:
    # per check-instant (hour, minute) -> trailing |move| history (one append per day)
    noise: dict[tuple[int, int], deque[float]] = field(default_factory=dict)
    day: _DayState | None = None
    side: Side | None = None  # open position side (None = flat)


class NiftyNoiseBreakout(Strategy):
    """Noise-area breakout with optional first-half-hour conditioning (module docstring)."""

    def __init__(self, config: NiftyNoiseBreakoutConfig | None = None) -> None:
        self._cfg = config or NiftyNoiseBreakoutConfig()
        if self._cfg.lookback_days < 2:
            raise ValueError("lookback_days must be >= 2")
        if self._cfg.conditioning not in (0, 1):
            raise ValueError("conditioning must be 0 (none) or 1 (first-half-hour sign)")
        self._fhh_end = time.fromisoformat(self._cfg.fhh_end)
        self._flat_time = time.fromisoformat(self._cfg.flat_time)
        if self._fhh_end >= self._flat_time:
            raise ValueError("fhh_end must be before flat_time")
        self._state: dict[str, _State] = {}

    @classmethod
    def from_config(cls) -> NiftyNoiseBreakout:
        raw = load_yaml("strategies/nifty_noise_breakout.yaml")
        return cls(NiftyNoiseBreakoutConfig.model_validate(raw))

    def on_bar(self, bar: Bar) -> Sequence[Signal]:
        st = self._state.setdefault(bar.symbol, _State())
        now = bar.start + bar.interval  # the decision instant (bar close), tz-aware UTC
        now_ist = now.astimezone(_IST)

        # --- IST day rollover: reset day state; flatten a gap-orphaned position loudly ----
        if st.day is None or st.day.ist_date != now_ist.date():
            st.day = _DayState(ist_date=now_ist.date())
            if st.side is not None:
                return [self._exit(bar, st, "gap-day flatten (EOD bar was missing)")]
        if st.day.day_open is None:
            st.day.day_open = bar.open  # first-seen bar of the IST day

        # --- capture the first-half-hour sign at its close instant ------------------------
        if st.day.fhh_sign is None and now_ist.time() >= self._fhh_end:
            ret = bar.close - st.day.day_open
            st.day.fhh_sign = 0 if ret == 0 else (1 if ret > 0 else -1)

        # --- EOD flat ----------------------------------------------------------------------
        if now_ist.time() >= self._flat_time:
            if st.side is not None:
                return [self._exit(bar, st, "EOD flat")]
            return []

        # --- check instants: each HH:00 / HH:30 IST close ----------------------------------
        if now_ist.minute not in (0, 30) or now_ist.second != 0:
            return []
        key = (now_ist.hour, now_ist.minute)
        history = st.noise.setdefault(key, deque(maxlen=self._cfg.lookback_days))
        move = abs(float(bar.close) / float(st.day.day_open) - 1.0)  # statistics plane only

        signals: list[Signal] = []
        if (
            st.side is None
            and not st.day.entered_today
            and len(history) == self._cfg.lookback_days
        ):
            boundary = sum(history) / len(history)
            direction: Side | None = None
            if move > boundary:  # beyond the noise area for this time of day
                direction = Side.BUY if bar.close > st.day.day_open else Side.SELL
            if direction is not None and self._cfg.conditioning == 1:
                want = 1 if direction is Side.BUY else -1
                if st.day.fhh_sign is None or st.day.fhh_sign != want:
                    direction = None  # unconditioned-by-fhh (too early / flat / disagrees)
            if direction is not None:
                st.side, st.day.entered_today = direction, True
                signals.append(
                    self._signal(bar, direction, "noise-area break", Decimal(1))
                )
        # today's move enters the history AFTER the decision — never judges itself
        history.append(move)
        return signals

    def on_tick(self, tick: Tick) -> Sequence[Signal]:
        return []

    def _exit(self, bar: Bar, st: _State, reason: str) -> Signal:
        assert st.side is not None
        exit_side = Side.SELL if st.side is Side.BUY else Side.BUY
        st.side = None
        return self._signal(bar, exit_side, reason, None)

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
