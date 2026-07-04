"""F4 forward recorder — Binance USDT-M forceOrder (liquidation) stream → daily JSONL.

The DEPLOYABLE event source for the liquidation-reversion family: the um forceOrder
websocket (NB Binance downsamples it to at most one order per symbol per second — a KNOWN
property of the live signal, which is exactly why the family must be judged on THIS source:
the cm daily archive ended 2024-10-14 and never covered um at all). Runs forever, appends one
JSON line per event to ``data_liq_live/YYYY-MM-DD.jsonl`` (UTC-dated, rotation by date),
reconnects with backoff. Research-plane: no keys, public stream, no orders.

Deploy: ``deploy/systemd/liq-recorder.service`` (staged, NOT enabled — [You] enables at the
next box deploy; ~a few MB/day, MemoryMax-capped).

    uv run python scripts/record_liquidations.py [--symbols BTCUSDT,ETHUSDT]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import ccxt.pro as ccxtpro  # type: ignore[import-untyped]

OUT_ROOT = Path(
    os.environ.get("ALPHA_LIQ_LIVE_ROOT") or (Path(__file__).resolve().parents[1] / "data_liq_live")
)
_BACKOFF_S = 5.0


def _append(root: Path, event: dict[str, object]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    with (root / f"{day}.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, separators=(",", ":"), default=str) + "\n")


async def record(symbols: list[str]) -> None:
    exchange = ccxtpro.binance({"options": {"defaultType": "future"}})
    try:
        while True:
            try:
                liquidations = await exchange.watch_liquidations_for_symbols(symbols)
                for liq in liquidations:
                    _append(
                        OUT_ROOT,
                        {
                            "recorded_at": datetime.now(UTC).isoformat(),
                            "symbol": liq.get("symbol"),
                            "side": liq.get("side"),
                            "price": liq.get("price"),
                            "amount": liq.get("amount"),
                            "timestamp": liq.get("timestamp"),
                        },
                    )
            except Exception as exc:  # reconnect-with-backoff: the recorder must outlive blips
                print(
                    f"[liq-recorder] stream error, retrying in {_BACKOFF_S}s: {exc!r}", flush=True
                )
                await asyncio.sleep(_BACKOFF_S)
    finally:
        await exchange.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Record Binance um forceOrder liquidations")
    ap.add_argument("--symbols", default="BTC/USDT:USDT,ETH/USDT:USDT")
    args = ap.parse_args()
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    print(f"=== liq recorder -> {OUT_ROOT} ({symbols}) ===", flush=True)
    asyncio.run(record(symbols))
    return 0


if __name__ == "__main__":
    sys.exit(main())
