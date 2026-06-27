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

    import ccxt.async_support as ccxt  # lazy: keep ccxt off import-time + out of the kernel

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
            },
        }
    )
    if venue.testnet:
        exchange.set_sandbox_mode(True)
    return CcxtAdapter(exchange=exchange, venue=venue.venue, streaming=venue.streaming)
