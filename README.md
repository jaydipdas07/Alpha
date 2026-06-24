# Alpha

AI-driven multi-asset algorithmic trading platform. A Lemma **Vault** pod is mission control —
the AI strategy-discovery brain, interactive dashboard, and human approval gate — paired with an
external always-on **execution worker**. Markets: crypto derivatives (Delta live / Binance data) and
Indian equities, F&O & currency derivatives (**Kite + Dhan + Upstox**). **Paper-first; every go-live
is human-approved per strategy.**

The execution/backtest engine is decided by a **Phase-0 co-equal A/B bake-off** — a NautilusTrader
shell vs lifting the operator's own proven **Vega** engine wholesale — chosen on backtest≡live
parity, integration friction, and time-to-market (current evidence favors the Vega lift).

**Paper-first; every go-live is human-approved per strategy.** The build has not started.

## Repository docs

Read in this order; each has one job (single source of truth — don't duplicate across them):

| Doc | Role |
|---|---|
| **[CLAUDE.md](CLAUDE.md)** | **Operating contract** — read first, every session. Cardinal invariants, never-do list, config convention, session-continuity protocol. |
| **[TASKS.md](TASKS.md)** | **Execution work queue** — phase-by-phase tasks (ID · owner · done-when) + the live **⏳ Pending tracker** at the end (start here for "what's next"). |
| **[docs/DESIGN_v4.md](docs/DESIGN_v4.md)** | **Canonical design doc** — architecture, decisions E1–E5, roadmap, the completed Vega deep-review. Supersedes BUILD_PLAN where they differ. |
| **[BUILD_PLAN.md](BUILD_PLAN.md)** | v3 hardened backbone (the R1–R14 adversarial hardening); the substrate DESIGN_v4 amends. |
| **[docs/PHASE0_HANDOFF.md](docs/PHASE0_HANDOFF.md)** | Build-**environment** & readiness handoff (B1–B5, readiness tiers, first moves on the Mac CLI). Not a design doc. |
| **[docs/archive/](docs/archive/)** | Superseded / orphan docs kept for provenance (e.g. the Codex "Lemma-first" brief). |
