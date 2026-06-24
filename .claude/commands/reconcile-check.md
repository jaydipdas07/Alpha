---
description: Run a read-only reconcile against the venue and report broker-vs-local drift
argument-hint: "[--adapter paper|delta|binance|kite|dhan|upstox]"
allowed-tools: Bash(uv run alpha reconcile*), Bash(grep*), Read
---

Run a **read-only** reconcile (broker = source of truth) and report any drift between the broker and
the local book. Do **not** pass `--flatten` — this command is diagnostic only; flattening is an
operator action (and the worker's autonomous path).

> **Availability:** reconcile lands with the worker in **Phase 3** (`M3.2`), lifted from Vega's
> `execution/reconcile.py`. Until `alpha reconcile` exists, say so and point at `M3.2`.

## Run

Run `uv run alpha reconcile $ARGUMENTS` (read-only; no `--flatten`).

## Report

- Orders / positions / fills where broker and local disagree (with the ids).
- The reconcile **status**: CLEAN / RECOVERABLE (explained drift adopted) / HALTED (unexplained drift),
  and what triggered it.
- **Phantom positions** — any local-nonzero symbol the broker no longer reports (must be zeroed, M2).

A "CLEAN with adoptions" result means the local book was **divergent and recovered** — surface the
adoptions explicitly rather than just trusting CLEAN, and confirm the caller actually applied them
(broker-truth, TEST-7). Never trip the kill-switch or place orders from this command.
