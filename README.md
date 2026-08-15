# Alpha

[![CI](https://github.com/jaydipdas07/Alpha/actions/workflows/ci.yml/badge.svg)](https://github.com/jaydipdas07/Alpha/actions/workflows/ci.yml)

AI-driven multi-asset algorithmic trading platform: an autonomous strategy-discovery and
backtesting engine whose survivors are paper-traded and then deployed live — with **every go-live
individually human-approved**. Markets: crypto derivatives (Delta live / Binance data) and Indian
equities, F&O & currency derivatives (Zerodha Kite, with Dhan/Upstox adapters planned).

The system is split into a **mission-control pod** (strategy-discovery agents, dashboard, human
approval gate) and an always-on **execution worker** — the sole component that ever touches a
broker. A thin shared kernel (`alpha-core`) carries the strategy contract and the
risk/cost/reconcile logic into both the backtester and the live worker, which is the parity
guarantee the whole design rests on.

Engineering properties the codebase proves with tests:

- **Backtest ≡ live** — the same strategy and execution code runs in both; time is injected, never
  read from the wall clock.
- **Money is never a float** — `Decimal` end to end; P&L is a pure fold over deduped, immutable
  fills.
- **Non-bypassable risk** — every order passes the halt gate first; a tripped kill-switch latches
  and survives restart.
- **Idempotent, audited orders** — client-generated IDs, dedup-by-query, an append-only
  hash-chained audit log.
- **The broker is the source of truth** — reconcile on startup and periodically; unexplained drift
  halts, never guesses.
- **Holdout isolation** — validation data is sealed off from every discovery agent; only a
  one-shot gate may read it. The holdout market data itself is never committed (see
  `options_holdout/README.md`).

## Performance

The event-driven backtester replays historical bars through the **same** signal → risk-gate →
order-FSM → fill → Decimal-P&L path a live run takes: ~6,000 minute-bars/sec (165 µs/bar)
sustained on a laptop, deterministic across runs. Reproduce with
`uv run python scripts/bench_backtest.py`.

## Documentation

| Doc | Role |
|---|---|
| **[docs/DESIGN_v4.md](docs/DESIGN_v4.md)** | **Canonical design doc** — architecture, locked decisions, roadmap. |
| **[docs/adr/](docs/adr/)** | Architecture Decision Records — the durable "why" behind each phase gate (engine bake-off, rigor calibration, paper-safety gate, …). |
| **[BUILD_PLAN.md](BUILD_PLAN.md)** | The hardened build backbone (R1–R14 adversarial hardening); the substrate DESIGN_v4 amends. |
| **[docs/PHASE0_HANDOFF.md](docs/PHASE0_HANDOFF.md)** | Build-environment & readiness handoff. |
| **[docs/](docs/) (operational)** | How-to guides: nightly discovery, the research host, the strategy-knowledge corpus. |
| **[docs/archive/](docs/archive/)** | Superseded docs kept for provenance. |

Build status is tracked in `TASKS.md`, not here.

## Development process

This project is built with agentic tooling (Claude Code) that I direct: every change lands as a
reviewed pull request, every architectural decision is recorded in an ADR, and the safety-critical
gates — live trading, credentials, capital, and the agent's own guardrails — are human-only by
explicit contract. Two files formalize that process: **[CLAUDE.md](CLAUDE.md)** is the agent
operating contract (the engineering invariants and the never-do list the tooling is bound by), and
**[TASKS.md](TASKS.md)** is the execution work queue and session-continuity record. The commit
history is the audit trail of how the process runs in practice.

## License

[MIT](LICENSE). Live-trading configuration, credentials, market data, and the research-holdout
data are deliberately not part of the repository — in the working tree or anywhere in its history
(verified with gitleaks over all commits).
