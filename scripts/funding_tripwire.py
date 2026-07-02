"""The funding-regime tripwire run (M3.0 basis follow-on) — is the basis desk relevant again?

Fetches the trailing window of LIVE public funding for the crypto panel universe (no keys),
assesses the cross-sectional spread against the basis family's entry bar
(``research/tripwire.py``; thresholds in ``config/research.yaml``), and alerts on a trigger —
Telegram when ``TELEGRAM_BOT_TOKEN``/``TELEGRAM_CHAT_ID`` are in the environment, the
structured log otherwise. Monitoring only: touches no research/holdout store, deploys nothing;
a trigger is a signal for [You] to consider re-opening the basis research, never an action.

Runnable anywhere with network (Mac cron / the box timer — deploy/systemd/funding-tripwire.*).
Wall-clock lives here at the edge (scripts own "now"; the engine never does). Run:

    uv run python scripts/funding_tripwire.py
"""

from __future__ import annotations

import sys
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

from alpha_core.observability.logging import get_logger
from alpha_core.observability.notify import Severity, notifier_from_env
from alpha_core.research.lease import ResearchConfig
from alpha_core.research.tripwire import assess_funding_regime, format_reading

sys.path.insert(0, str(Path(__file__).resolve().parent))  # sibling script import (as #130 does)
from ingest_funding import fetch_funding, panel_symbols


def main() -> None:
    log = get_logger("tripwire")
    config = ResearchConfig.from_config().funding_tripwire
    symbols = panel_symbols()
    now = datetime.now(tz=UTC)  # the edge owns wall-clock
    start = now - timedelta(days=config.lookback_days)

    rates_by_symbol = {}
    for symbol in symbols:
        try:
            rates_by_symbol[symbol] = fetch_funding(symbol, start=start, end=now)
        except (urllib.error.URLError, RuntimeError) as exc:
            # a delisted/invalid member (HTTP 400) or a blip must never abort the MONITOR —
            # a dead tripwire defeats its purpose. The symbol simply drops out of the
            # cross-section this run (the same tolerance ingest_funding.main applies).
            log.warning("tripwire_symbol_skipped", symbol=symbol, error=repr(exc))
        time.sleep(0.2)  # polite to the public endpoint

    reading = assess_funding_regime(rates_by_symbol, config=config)
    text = format_reading(reading)
    print(text)
    log.info(
        "tripwire_reading",
        triggered=reading.triggered,
        top_k_annual_rate=str(reading.top_k_annual_rate),
        threshold=str(reading.threshold),
        symbols=reading.symbols_assessed,
    )
    if reading.triggered:
        notifier_from_env().send(text, severity=Severity.WARNING)


if __name__ == "__main__":
    main()
