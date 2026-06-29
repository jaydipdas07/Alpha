#!/usr/bin/env python
"""Ingest Kite Connect 1-minute NSE history into the cold store (M3.0 Indian leg).

The network + daily-2FA-auth glue around the pure ``alpha_core.data.ingest.kite`` transforms
(``candles_to_bars`` + ``access_token_is_stale``). Kite access tokens expire ~06:00 IST every
morning and a fresh one needs an **interactive** 2FA login, so this script is **two-phase** — and
the human does the login while the script does the programmatic token exchange (the never-do list:
CC never enters a login / credential, and the access-token value is never printed or committed):

    # Phase 1 — token stale/absent: print the login URL for the HUMAN, then stop (exit 2).
    uv run python scripts/ingest_kite.py
    #   -> open the printed URL, log in; the redirect URL carries ?request_token=XXXX

    # Phase 2 — exchange the human-provided request_token + persist the fresh token, then ingest.
    uv run python scripts/ingest_kite.py --request-token XXXX

    # Just check staleness (pure, no network, no SDK) — exit 0 fresh / 2 stale.
    uv run python scripts/ingest_kite.py --check

With a valid token it resolves each store symbol (``NSE:RELIANCE``) to its Kite instrument token
via the instruments dump, paginates ``historical_data(token, from, to, "minute")`` within Kite's
60-day-per-request cap, folds the IST rows to tz-UTC ``Bar``s, and writes them to the cold store.
The default universe is every NSE equity in ``config/instruments.yaml`` (override ``--symbols``);
the store root honors ``ALPHA_COLD_ROOT`` and re-runs are idempotent (``write_bars`` overwrites
the same series window). Exit codes: 0 ok · 1 error · 2 human reauth required.

Mac-CLI / network — **not a CI test** (the pure transforms in ``data/ingest/kite.py`` are the tested
part; this is the SDK glue). ``kiteconnect`` is lazy-imported so ``--check`` needs no SDK.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from alpha_core.data.ingest.kite import access_token_is_stale, candles_to_bars
from alpha_core.data.store import BarStore
from alpha_core.helpers.config import load_yaml

if TYPE_CHECKING:  # the SDK is lazy-imported (see _new_kite); only the type is referenced here
    from kiteconnect import KiteConnect

_ROOT = Path(__file__).resolve().parents[1]
STORE_ROOT = Path(os.environ.get("ALPHA_COLD_ROOT") or (_ROOT / "data_cold"))

# Fixed past window (reproducible — a fixed end, never "now"); ~7 weeks of recent 1-minute intraday,
# mirroring the crypto 5m cell window. Override with --from/--to for a deeper pull.
DEFAULT_FROM = "2026-05-01"
DEFAULT_TO = "2026-06-21"

_INTERVAL_SECONDS = {"minute": 60, "day": 86400}
# Kite caps one historical_data request's span per interval — paginate wider windows.
_KITE_MAX_SPAN_DAYS = {"minute": 60, "day": 2000}


def _new_kite(api_key: str) -> KiteConnect:
    """Construct a KiteConnect (lazy SDK import — the staleness/--check path stays SDK-free)."""
    from kiteconnect import KiteConnect

    return KiteConnect(api_key=api_key)


def _read_env(path: Path) -> dict[str, str]:
    """Parse ``KEY=VALUE`` lines from ``.env`` (comments/blanks skipped); values are not logged."""
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    for line in path.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")  # split on the FIRST '=' (values may contain '=')
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def _persist_env(path: Path, updates: dict[str, str]) -> None:
    """Replace-or-append each key in ``.env``, preserving every other line/comment/order.

    The ``.env`` is gitignored (B3); the values written here (the access token) are never
    printed or committed — only persisted to the local file."""
    lines = path.read_text().splitlines() if path.is_file() else []
    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition("=")[0].strip() if "=" in stripped and stripped[0] != "#" else None
        if key is not None and key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)
    out.extend(f"{key}={value}" for key, value in remaining.items())
    path.write_text("\n".join(out) + "\n")


def _token_is_stale(env: dict[str, str], *, now: datetime) -> bool:
    """Is the .env Kite access token absent or rolled over since it was minted?"""
    if not env.get("KITE_ACCESS_TOKEN", "").strip():
        return True  # no token yet -> must auth
    token_at_raw = env.get("KITE_ACCESS_TOKEN_AT", "").strip()
    if not token_at_raw:
        return True  # token present but undateable -> re-auth, never guess its age
    try:
        token_at = datetime.fromisoformat(token_at_raw)
    except ValueError:
        return True  # unparseable timestamp -> stale
    if token_at.tzinfo is None:
        token_at = token_at.replace(tzinfo=UTC)  # legacy naive value -> assume the stored UTC
    return access_token_is_stale(token_at, now=now)


def _print_login_url(api_key: str) -> None:
    """Phase 1: print the Kite login URL for the HUMAN to complete the 2FA login in a browser."""
    kc = _new_kite(api_key)
    print("\n=== Kite reauth required (daily token stale or absent) ===")
    print("1. Open this login URL in a browser and log in — the HUMAN does this step:\n")
    print(f"   {kc.login_url()}\n")
    print("2. After login you are redirected to your registered redirect URL with")
    print("   ?request_token=XXXX&action=login&status=success in the address bar.")
    print("3. Copy that request_token and re-run this script to exchange it:\n")
    print("   uv run python scripts/ingest_kite.py --request-token <request_token>\n")
    print("The script does the token exchange programmatically; it never sees your login.")


def _exchange_request_token(
    *, api_key: str, api_secret: str, request_token: str, env_path: Path
) -> str:
    """Phase 2: exchange the human-provided request_token for an access token + persist it.

    The access-token value is written to the gitignored ``.env`` and **never printed** (only its
    length, as a sanity signal)."""
    kc = _new_kite(api_key)
    data = kc.generate_session(request_token, api_secret=api_secret)
    access_token = str(data["access_token"])
    minted_at = datetime.now(UTC)
    _persist_env(
        env_path,
        {"KITE_ACCESS_TOKEN": access_token, "KITE_ACCESS_TOKEN_AT": minted_at.isoformat()},
    )
    print(
        f"[auth] fresh Kite access token persisted to {env_path} at {minted_at.isoformat()} "
        f"(value not shown; len={len(access_token)})"
    )
    return access_token


def _nse_universe(symbols_arg: list[str] | None) -> list[str]:
    """The NSE store symbols to ingest: ``--symbols``, else every NSE equity in instruments.yaml."""
    if symbols_arg:
        return symbols_arg
    instruments = load_yaml("instruments.yaml").get("instruments", {})
    return sorted(
        sym
        for sym, meta in instruments.items()
        if isinstance(meta, dict) and meta.get("asset_class") == "EQUITY" and sym.startswith("NSE:")
    )


def _resolve_tokens(kc: KiteConnect, store_symbols: list[str]) -> dict[str, int]:
    """Map each store symbol (``EXCHANGE:TRADINGSYMBOL``) to its Kite instrument_token via the dump.

    Fetches each exchange's instruments() dump once. A symbol the dump doesn't list fails fast
    (a config gap) rather than silently ingesting nothing for it."""
    by_exchange: dict[str, set[str]] = {}
    for sym in store_symbols:
        exchange, _, tradingsymbol = sym.partition(":")
        if not tradingsymbol:
            raise ValueError(
                f"store symbol {sym!r} must be EXCHANGE:TRADINGSYMBOL (e.g. NSE:RELIANCE)"
            )
        by_exchange.setdefault(exchange, set()).add(tradingsymbol)
    tokens: dict[str, int] = {}
    for exchange, wanted in by_exchange.items():
        dump = kc.instruments(exchange)  # list of {tradingsymbol, instrument_token, ...}
        found = {
            row["tradingsymbol"]: int(row["instrument_token"])
            for row in dump
            if row["tradingsymbol"] in wanted
        }
        missing = wanted - found.keys()
        if missing:
            raise ValueError(f"instruments not found on {exchange}: {sorted(missing)}")
        for tradingsymbol, token in found.items():
            tokens[f"{exchange}:{tradingsymbol}"] = token
    return tokens


def _date_chunks(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    """Split ``[start, end]`` into inclusive sub-windows no wider than Kite's per-request cap."""
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(cursor + timedelta(days=max_days - 1), end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


def _fetch_bars(
    kc: KiteConnect, *, token: int, store_symbol: str, interval: str, start: date, end: date
) -> list:  # list[Bar] — Bar is alpha_core's; kept unannotated to avoid an import here
    """Paginate ``historical_data`` over ``[start, end]`` and fold the IST rows to tz-UTC Bars."""
    interval_seconds = _INTERVAL_SECONDS[interval]
    bars = []
    for chunk_start, chunk_end in _date_chunks(start, end, _KITE_MAX_SPAN_DAYS[interval]):
        candles = kc.historical_data(
            token, chunk_start.isoformat(), chunk_end.isoformat(), interval
        )
        bars.extend(
            candles_to_bars(candles, symbol=store_symbol, interval_seconds=interval_seconds)
        )
    return bars


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Ingest Kite 1-minute NSE history into the cold store."
    )
    ap.add_argument(
        "--symbols", nargs="*", help="store symbols (default: NSE equities in instruments.yaml)"
    )
    ap.add_argument("--interval", choices=sorted(_INTERVAL_SECONDS), default="minute")
    ap.add_argument("--from", dest="start", default=DEFAULT_FROM, help="YYYY-MM-DD (inclusive)")
    ap.add_argument("--to", dest="end", default=DEFAULT_TO, help="YYYY-MM-DD (inclusive)")
    ap.add_argument(
        "--request-token", help="exchange this human-provided request_token, then ingest"
    )
    ap.add_argument("--reauth", action="store_true", help="force the login-URL print even if fresh")
    ap.add_argument(
        "--check", action="store_true", help="report token staleness and exit (no network)"
    )
    ap.add_argument("--env-file", default=str(_ROOT / ".env"), help="path to the .env (gitignored)")
    args = ap.parse_args()

    env_path = Path(args.env_file)
    env = _read_env(env_path)
    api_key = env.get("KITE_API_KEY", "").strip()
    api_secret = env.get("KITE_API_SECRET", "").strip()
    if not api_key or not api_secret:
        print(f"[error] KITE_API_KEY / KITE_API_SECRET missing from {env_path}", file=sys.stderr)
        return 1

    now = datetime.now(UTC)
    stale = _token_is_stale(env, now=now)

    if args.check:
        state = "STALE — reauth required" if stale else "fresh"
        at = env.get("KITE_ACCESS_TOKEN_AT", "<unset>")
        print(f"Kite token: {state} (KITE_ACCESS_TOKEN_AT={at})")
        return 2 if stale else 0

    # phase 2: a human-provided request_token takes precedence over staleness
    if args.request_token:
        access_token = _exchange_request_token(
            api_key=api_key,
            api_secret=api_secret,
            request_token=args.request_token,
            env_path=env_path,
        )
    elif stale or args.reauth:  # phase 1: stop and let the human log in
        _print_login_url(api_key)
        return 2
    else:
        access_token = env["KITE_ACCESS_TOKEN"].strip()

    store_symbols = _nse_universe(args.symbols)
    if not store_symbols:
        print(
            "[error] no NSE equity symbols to ingest (check instruments.yaml or --symbols)",
            file=sys.stderr,
        )
        return 1
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)

    kc = _new_kite(api_key)
    kc.set_access_token(access_token)
    tokens = _resolve_tokens(kc, store_symbols)

    store = BarStore(STORE_ROOT)
    print(f"=== Kite {args.interval} ingest {start}..{end} -> {STORE_ROOT} ===")
    total = 0
    for store_symbol in store_symbols:
        bars = _fetch_bars(
            kc,
            token=tokens[store_symbol],
            store_symbol=store_symbol,
            interval=args.interval,
            start=start,
            end=end,
        )
        on_disk = store.write_bars(bars)
        print(f"[kite] {store_symbol} {args.interval}: {len(bars)} bars -> {on_disk} on disk")
        total += len(bars)

    if total == 0:
        print("no bars ingested (empty window or no history for the universe)")
        return 1
    print(f"=== done — {total} bars over {len(store_symbols)} symbols -> {store.root} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
