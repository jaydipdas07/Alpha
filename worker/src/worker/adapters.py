"""Adapter factory — build the live broker adapter for a configured venue (M3.1).

Constructs a ccxt exchange (sandbox mode for testnet) and wraps it in the
venue-agnostic ``CcxtAdapter`` (which lives in alpha-core — the *kernel* never
imports ccxt; the SDK is the worker's). The **live gate** is enforced here: the
factory refuses to build a non-testnet (live) adapter unless the environment has
explicitly opened the gate (``allow_live`` + ``mode=live``) — going live is a
human FORM step, never CC's (CLAUDE.md never-do, TEST-8).
"""

from __future__ import annotations

import os

from alpha_core.adapters.crypto_ccxt import CcxtAdapter
from alpha_core.data.kite_feed import KiteTickerFeed
from worker.config import EnvConfig, VenueConfig


def _keys(key_env: str) -> tuple[str, str]:
    """Read ``<key_env>_API_KEY`` / ``_API_SECRET`` from the environment (.env, B3)."""
    api_key = os.environ.get(f"{key_env}_API_KEY", "")
    secret = os.environ.get(f"{key_env}_API_SECRET", "")
    if not api_key or not secret:
        raise RuntimeError(f"missing {key_env}_API_KEY / {key_env}_API_SECRET in the environment")
    return api_key, secret


def build_adapter(venue: VenueConfig, env: EnvConfig) -> CcxtAdapter:
    """Build the ``CcxtAdapter`` for ``venue`` under ``env`` — live-gate-guarded.

    Refuses a live (non-testnet) venue unless the gate is explicitly open; reads
    the venue's API key/secret from the environment; puts a testnet venue into ccxt
    sandbox mode. Streaming venues (Binance ccxt.pro) push order events; non-streaming
    (Delta) are REST-polled by the adapter."""
    # LIVE GATE (defence in depth — EnvConfig + active_venue also guard this).
    if not venue.testnet and not (env.allow_live and env.mode == "live"):
        raise PermissionError(
            f"refusing to build a LIVE adapter for {venue.exchange}: the live gate is shut "
            "(allow_live=false / mode!=live). Going live is a human FORM step (CLAUDE.md)."
        )
    api_key, secret = _keys(venue.key_env)

    # Pick the ccxt module by the streaming flag (lazy import — keep ccxt off
    # import-time + out of the kernel). The real websocket watch_* methods live only
    # in ccxt.pro; ccxt.async_support stubs them as NotSupported, so a streaming venue
    # MUST use ccxt.pro or its tick/order feed dies on the first watch call. ccxt.pro
    # classes subclass async_support, so they keep every REST method too.
    if venue.streaming:
        import ccxt.pro as ccxt
    else:
        import ccxt.async_support as ccxt

    if not hasattr(ccxt, venue.exchange):
        where = "ccxt.pro (no websocket support?)" if venue.streaming else "ccxt"
        raise RuntimeError(f"unknown exchange {venue.exchange!r} in {where}")
    exchange_cls = getattr(ccxt, venue.exchange)
    exchange = exchange_cls(
        {
            "apiKey": api_key,
            "secret": secret,
            "enableRateLimit": True,
            "options": {
                "defaultType": venue.market_type,
                # Reconcile fetches ALL open orders (no symbol) to diff against broker
                # truth; Binance raises a stricter-rate-limit *warning* as an error for
                # that — acknowledge it (the worker's order volume is low). Venues that
                # don't define this option ignore it.
                "warnOnFetchOpenOrdersWithoutSymbol": False,
                # A market BUY carries a base quantity (the OMS always sizes in base
                # units), not a quote cost. Without this, Binance spot reads a market-buy
                # amount as quote (USDT) and demands a price. (Perps ignore the option.)
                "createMarketBuyOrderRequiresPrice": False,
            },
        }
    )
    if venue.testnet:
        exchange.set_sandbox_mode(True)
        if venue.testnet_url is not None:  # the venue's sandbox isn't ccxt's default testnet
            exchange.urls["api"] = {"public": venue.testnet_url, "private": venue.testnet_url}
    return CcxtAdapter(exchange=exchange, venue=venue.venue, streaming=venue.streaming)


def build_kite_ticker_feed(venue: VenueConfig, env: EnvConfig) -> KiteTickerFeed:
    """Build the LIVE Kite market-data feed for paper execution (M4.5) — data only,
    no order surface. Reads ``KITE_API_KEY`` + ``KITE_ACCESS_TOKEN`` from the
    environment (the daily token ``scripts/ingest_kite.py``'s 2FA flow stages; a
    stale one is warned loudly here and then fails at connect with Kite's own
    error). Instrument tokens come from the Kite instrument master — the gitignored
    cache when present, else fetched once and cached (network at the edge)."""
    import json
    from datetime import UTC, datetime, timedelta
    from pathlib import Path

    from kiteconnect import KiteConnect, KiteTicker  # worker dep — never the kernel's

    from alpha_core.data.ingest.kite import access_token_is_stale
    from alpha_core.execution.instruments import InstrumentRegistry
    from alpha_core.observability.logging import get_logger

    log = get_logger("kite_factory")
    prefix = venue.key_env  # the ccxt _keys idiom: env names derive from venues.yaml
    api_key = os.environ.get(f"{prefix}_API_KEY", "")
    access_token = os.environ.get(f"{prefix}_ACCESS_TOKEN", "")
    if not api_key or not access_token:
        raise RuntimeError(
            f"missing {prefix}_API_KEY / {prefix}_ACCESS_TOKEN in the environment — run "
            "the daily 2FA flow (scripts/ingest_kite.py) to stage a fresh token"
        )
    token_at_raw = os.environ.get(f"{prefix}_ACCESS_TOKEN_AT", "").strip()
    if token_at_raw:
        try:
            stale = access_token_is_stale(
                datetime.fromisoformat(token_at_raw), now=datetime.now(UTC)
            )
        except ValueError:
            stale = False  # a malformed stamp is a warning problem, not a build problem
            log.debug("kite_token_stamp_unparseable", raw=token_at_raw)
        if stale:
            log.warning(
                "kite_token_probably_stale",
                hint="tokens die ~06:00 IST daily; rerun scripts/ingest_kite.py",
            )

    cache = Path(env.kite_instruments_cache)
    # #160(d): an mtime bound on the cached master — F&O contracts churn weekly, so a
    # use-forever cache eventually serves dead tokens. Wall-clock is fine here: this is
    # boot-time network glue at the edge, never the engine.
    fresh = False
    if cache.is_file():
        age = datetime.now(UTC) - datetime.fromtimestamp(cache.stat().st_mtime, tz=UTC)
        fresh = age <= timedelta(hours=env.kite_instruments_cache_max_age_hours)
    if fresh:
        registry = InstrumentRegistry.from_kite_json(cache)
        log.info("kite_instruments_cached", path=str(cache))
    else:
        try:
            rows = KiteConnect(api_key=api_key, access_token=access_token).instruments("NSE")
        except Exception as exc:
            if not cache.is_file():
                raise
            # A refetch blip must not brick a boot when a (stale) master exists — proceed
            # on it, LOUDLY. Equity tokens are years-stable; a symbol the stale master
            # doesn't know still fails fast at the token-map check below.
            log.warning("kite_instruments_stale_fallback", path=str(cache), error=repr(exc))
            registry = InstrumentRegistry.from_kite_json(cache)
        else:
            cache.parent.mkdir(parents=True, exist_ok=True)
            # Kite rows carry date objects — stringify; the registry parses them back.
            cache.write_text(json.dumps(rows, default=str), encoding="utf-8")
            registry = InstrumentRegistry.from_kite_dump(rows)
            log.info("kite_instruments_fetched", rows=len(rows), cached=str(cache))

    tokens = registry.token_map()
    missing = [s for s in env.symbols if s not in tokens]
    if missing:
        raise RuntimeError(
            f"no Kite instrument token for {missing} — not in the NSE instrument master "
            f"(check the symbol spelling, or delete {cache} to refresh the dump)"
        )
    ticker = KiteTicker(api_key, access_token)
    return KiteTickerFeed(
        ticker,
        token_by_symbol={s: tokens[s] for s in env.symbols},
        venue=venue.venue,
    )
