"""F1 end-to-end pipeline smoke (the F2-review lesson: prove the production pair resolves
the family registry + threads the session gate BEFORE the pre-registration run)."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from alpha_core.core.enums import AssetClass, Venue
from alpha_core.core.models import Bar
from alpha_core.data.holdout import HoldoutStore, HoldoutWindow
from alpha_core.data.store import BarStore
from alpha_core.helpers.config import DiscoveryCellConfig, load_yaml
from alpha_core.research.cost_scenarios import futures_costed_equity_config
from alpha_core.research.f1_templates import F1_TEMPLATES
from alpha_core.research.promote import build_survivor_backtesters
from alpha_core.research.strategist import StrategyProposal
from alpha_core.risk.limits import load_risk_config
from alpha_core.scheduler.clock import MarketSchedule

IST = timezone(timedelta(hours=5, minutes=30))
SYMBOL = "NSE:NIFTY 50"
ONE_MIN = timedelta(minutes=1)


def _day(day: int, month: int = 6) -> list[Bar]:
    """Three 1m bars for one IST session day: the opener, a 10:00 check, the 15:05 flat."""
    out = []
    for hh, mm in ((9, 16), (10, 0), (15, 5)):
        close_ist = datetime(2026, month, day, hh, mm, tzinfo=IST)
        px = Decimal("100")
        out.append(
            Bar(
                symbol=SYMBOL,
                venue=Venue.NSE,
                asset_class=AssetClass.EQUITY,
                start=(close_ist - ONE_MIN).astimezone(UTC),
                interval=ONE_MIN,
                open=px,
                high=px + 1,
                low=px - 1,
                close=px,
                volume=Decimal("1000"),
            )
        )
    return out


def _cell() -> DiscoveryCellConfig:
    return DiscoveryCellConfig.model_validate(
        {
            "market": "EQUITY",
            "window": "nifty50-1m",
            "symbol": SYMBOL,
            "venue": "NSE",
            "interval_seconds": 60,
            "starting_cash": "1000000",
        }
    )


def test_f1_survivor_pair_resolves_the_registry_and_threads_the_gate(tmp_path: Path) -> None:
    # ~June 2026 weekdays (1-30, skipping weekends): enough sessions to clear the
    # rigor floor (2 * cpcv.n_groups bars) on both stores.
    weekdays = [d for d in range(1, 31) if datetime(2026, 6, d).weekday() < 5]
    research_bars = [b for d in weekdays[:16] for b in _day(d)]
    holdout_bars = [b for d in weekdays[16:] for b in _day(d)]

    research = BarStore(tmp_path / "research")
    research.write_bars(research_bars)
    holdout = HoldoutStore(tmp_path / "holdout")
    holdout.replace(
        holdout_bars,
        HoldoutWindow(
            start=holdout_bars[0].start, end=holdout_bars[-1].start + ONE_MIN, version="v1"
        ),
    )

    schedule = MarketSchedule(
        tz="Asia/Kolkata",
        open_time=time(9, 15),
        close_time=time(15, 30),
        no_new_entry=time(15, 10),
        square_off=time(15, 15),
    )
    in_sample, holdout_bt = build_survivor_backtesters(
        cell=_cell(),
        research_store=research,
        holdout_store=holdout,
        risk_config=load_risk_config(),
        cost_config=futures_costed_equity_config(load_yaml("costs.yaml")),
        templates=F1_TEMPLATES,
        schedule=schedule,
        intraday_square_off=True,
    )
    proposal = StrategyProposal(
        template="nifty_noise_breakout",
        params={"lookback_days": Decimal("14"), "conditioning": 0},
        market=AssetClass.EQUITY,
        window="nifty50-1m",
        trial_index=1,
        fingerprint="smoke",
    )
    # Both sides resolve the F1 registry and run end-to-end under the gate (a flat tape
    # produces a defined, all-zero-ish series — the wiring, not the edge, is under test).
    is_series = list(in_sample.run(proposal))
    ho_series = list(holdout_bt.run(proposal))
    assert len(is_series) == len(research_bars)  # one return per bar off the equity curve
    assert len(ho_series) == len(holdout_bars)
    # The SF4 threading is live on BOTH sides (rigor symmetry — review #176 F5).
    assert in_sample._schedule is schedule and holdout_bt._schedule is schedule  # type: ignore[attr-defined]
    assert in_sample._intraday_square_off and holdout_bt._intraday_square_off  # type: ignore[attr-defined]
