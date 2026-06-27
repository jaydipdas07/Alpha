# Alpha

AI-driven multi-asset algorithmic trading platform. A Lemma **Vault** pod is mission control —
the AI strategy-discovery brain, interactive dashboard, and human approval gate — paired with an
external always-on **execution worker**. Markets: crypto derivatives (Delta live / Binance data) and
Indian equities, F&O & currency derivatives (**Kite + Dhan + Upstox**). **Paper-first; every go-live
is human-approved per strategy.**

The execution/backtest engine was chosen by a **Phase-0 co-equal A/B bake-off** — a NautilusTrader
shell vs lifting the operator's own proven **Vega** engine wholesale — on backtest≡live parity,
integration friction, and time-to-market: **Track B (the Vega lift) won** (0.GATE, ADR 0001).

**Phases 0–1 are complete** — engine lifted; the rigor + AI-discovery machine is built, calibrated,
and deployed nightly on the research box; **Phase 2 (the cockpit) is next** (see `TASKS.md`).

## Repository docs

Read in this order; each has one job (single source of truth — don't duplicate across them):

| Doc | Role |
|---|---|
| **[CLAUDE.md](CLAUDE.md)** | **Operating contract** — read first, every session. Cardinal invariants, never-do list, config convention, session-continuity protocol. |
| **[TASKS.md](TASKS.md)** | **Execution work queue** — phase-by-phase tasks (ID · owner · done-when) + the live **⏳ Pending tracker** at the end (start here for "what's next"). |
| **[docs/DESIGN_v4.md](docs/DESIGN_v4.md)** | **Canonical design doc** — architecture, decisions E1–E5, roadmap, the completed Vega deep-review. Supersedes BUILD_PLAN where they differ. |
| **[BUILD_PLAN.md](BUILD_PLAN.md)** | v3 hardened backbone (the R1–R14 adversarial hardening); the substrate DESIGN_v4 amends. |
| **[docs/PHASE0_HANDOFF.md](docs/PHASE0_HANDOFF.md)** | Build-**environment** & readiness handoff (B1–B5, readiness tiers, first moves on the Mac CLI). Not a design doc. |
| **[docs/adr/](docs/adr/)** | Architecture Decision Records — the durable "why" behind each gate (0001 engine bake-off → Vega lift, 0002 1a-gate calibration, 0003 Phase-1 completion). |
| **[docs/](docs/) (operational)** | How-to guides: `nightly-discovery.md` (research-box cron + `/knowledge`), `research-host.md` (the AWS box), `knowledge/strategies.md` (the RAG corpus). |
| **[docs/archive/](docs/archive/)** | Superseded / orphan docs kept for provenance (e.g. the Codex "Lemma-first" brief). |
