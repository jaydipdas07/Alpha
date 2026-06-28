# ADR 0004 — 3.GATE: the live safety machinery + the human approval pipeline are demonstrated; real-edge paper P&L is deferred to vendor data (M3.0)

- Status: **Accepted** — operator ratification by [You] (Jaydip Das) on 2026-06-28
- Date: 2026-06-28
- Deciders: Jaydip Das ([You]) / Claude Code
- Depends on: ADR 0001 (engine → Vega lift), ADR 0003 (1b.GATE / Phase-1 closure + the M3.0 deferral), `docs/DESIGN_v4.md`, `TASKS.md`

## Context

Phase 3 built the **paper pipeline + the safety machinery**: the portable safety core
(`alpha_core` — the latching kill-switch + `restore()`, broker-truth reconcile, the OMS-derived P&L
fold over immutable fills, idempotent client-order-ids, the independent deadman) and the **worker**
(the sole executor: adapter factory + bar builder + run loop + the pod-backed command bus), deployed
as `alpha-worker` + `alpha-deadman` systemd units on the research box against **Delta India perp
testnet** (`BTC/USD:USD`). M3.7 then added the **human approval pipeline** (Workflow B).

3.GATE asked for three things: (a) the **kill-the-worker-mid-position deadman test** (TEST-5); (b) the
full **approval pipeline** (a discovered survivor reaches a per-strategy human FORM and, on approval,
is deployed); and (c) **paper P&L ≈ backtest** on a real strategy.

What was demonstrated, live:

1. **The safety drill** (CC, earlier 2026-06-28) — one run on real Delta-testnet perps showed all
   five: derived P&L from the fill stream (TEST-2), idempotent COIDs (TEST-6), the kill-switch
   flatten (TEST-4), broker-truth reconcile (TEST-7), and the headline — a **real perp position + a
   stale worker heartbeat → the independent deadman flattened it via REST → broker flat (TEST-5)**.
   The account was left clean (0 positions / 0 orders).

2. **The full approval pipeline** (now, end-to-end on the live Vault pod + the live testnet worker) —
   a TEST strategy ran **strategy → paper → risk-officer/holdout gate → human FORM**, and on [You]'s
   FORM approval the chain completed: `finalize_deployment` issued a `start` command on the pod
   `commands` table → the worker polled it within ~1s, executed it, and acked it
   (`status: done`). The two cardinal invariants this path exists to protect held throughout: the
   **holdout read stayed off-pod and only a scalar verdict crossed onto the agent-excluded
   `approval_requests` table (TEST-3)**, and the **pod only issued the command — the worker was the
   sole executor (TEST-8)**.

The demonstration used a **synthetic, explicitly-labelled** holdout verdict (`origin=demo`) precisely
so that requirement (c) — paper P&L matching a backtest on a *real* edge — is not faked. As ADR 0003
established, no genuine deployable edge exists in the build's **free** data; a real one needs the
data **vendor** feed the operator will provide at **M3.0**.

## Decision

**3.GATE is ratified on the live demonstration of the safety machinery and the human approval
pipeline.** Phase 3's purpose — that paper-trading is *governed*: non-bypassable risk, a dead worker
self-flattens, the broker is truth, and nothing reaches a venue without a per-strategy human approval
that the pod can issue but never execute — is proven end-to-end on real testnet infrastructure.

**Real-edge paper P&L ≈ backtest is deferred to M3.0**, the same explicit, transparent deferral as
1b.GATE (ADR 0003): showing that a *genuine* survivor's forward paper return tracks its backtest
needs market data with a real edge, which is the M3.0 vendor on-ramp ([You] provides the source/keys;
[CC] builds the ingest adapters + runs discovery; a real survivor then flows through this same,
now-proven, Workflow B). This is not a faked result — the pipeline is proven; the one piece not yet
shown is honestly gated on data that does not exist in the build yet.

## Consequences

- **Phase 4 (first real money — Delta live, staged) is the next phase, and it is fully [You]-gated.**
  Going live is always a fresh per-strategy human FORM, never CC; it additionally needs live keys, a
  static-IP VPS, exchange-side cancel-on-disconnect, and the SEBI long-poles — none of which this gate
  touches. The money / live-gate / secrets guardrails are unchanged.
- **M3.0 (the vendor on-ramp) remains the one open [CC] build in Phase 3**, blocked on operator data;
  it carries the deferred real-edge paper P&L (this gate) *and* the deferred 1b.GATE real promotion
  (ADR 0003) — one data dependency satisfies both.
- **Operational:** the worker↔pod pod-sync token is 60-minute and the worker reads `.env` only at
  startup, so a long-lived worker needs the deferred token-persistence polish (a Mac relay + a
  worker-side periodic re-read) before an unattended demo; a manual re-stage covers a supervised one.
- The cockpit Approvals view can optionally be wired to the agent-excluded `approval_requests` table
  so the human sees the holdout verdict in-app (it is read under the operator's identity, never an
  agent's — TEST-3 holds).
- Phases 0–3 are now closed bar the M3.0 data dependency; the build's centre of gravity moves to the
  human-gated path to first real money.
