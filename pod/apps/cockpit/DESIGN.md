# Cockpit — design

> The Phase-2 product: Alpha's mission-control dashboard, a Lemma **app** in the
> Vault pod. `lemma-builder`'s apps method asks for this doc before code — it is
> the contract the views are built against, and the M2.1→M2.5 decomposition.

## Purpose & persona

**One operator (you).** The cockpit is where the human watches the autonomous
discovery → paper → live pipeline and exercises the gates that are deliberately
human: **approving a strategy for go-live**, and **arming the kill-switch /
flattening**. It is a **read + command surface**, never an executor:

- It **reads** the pod's book/governance tables over the SDK (`watchChanges` for
  live updates — never polling).
- It **writes** only governance intents: FORM approvals, and `commands` rows
  (arm / flatten / clear-halt). The **worker** executes; the pod never places a
  live order (**TEST-8**). The cockpit is never on the money path.
- It **never** surfaces the holdout partition (**TEST-3**) — no view reads it.
- Money/price/qty render from **string-encoded `Decimal`** columns (B5); the UI
  formats, it never does float math on money.

## The first 30 seconds

The default screen is **Overview** — system health, not a landing page: worker +
research heartbeats, last reconcile, data freshness, open `risk_events`, and a
live equity/P&L chart. If something is wrong (stale heartbeat, open risk event,
armed kill-switch), it is visible here immediately.

## Page map

| View | Purpose | Reads (tables) | Actions | Milestone |
|---|---|---|---|---|
| **Overview** | Health + live status at a glance | `worker_status`, `research_status`, `risk_events`, `pnl_snapshots` | — | M2.1 / M2.2 |
| **Strategies** | Discovered + deployed strategies & lifecycle | `strategies`, `deployments` | — | M2.4 |
| **Discovery** | Nightly discovery cycles | `discovery_runs`, `research_ledger` | — | M2.4 |
| **Backtests** | Every candidate **incl. rejected** | `backtests` | filter/sort | M2.3 |
| **Approvals** | Human go-live inbox (Workflow B FORMs) | workflow run waits | **submit FORM** | M2.4 |
| **Exchanges** | Venues + tradable universe | `config/venues`, `config/instruments` | — | M2.4 |
| **Risk** | Kill-switch, halts, open events | `risk_events`, `worker_status`, `commands` | **arm / flatten / clear-halt** → `commands` | M2.4 (controls live in Phase 3) |
| **Config** | The tunables (read-only) | `config/{risk,costs,rigor}` | — | M2.4 |
| **Desk** (copilot) | Chat over the agents | agent conversations | chat | M2.5 |

## Per-page scenarios

- **Triage (Overview):** open cockpit → a worker heartbeat is stale → its tile is
  amber/`STALE` → click through to Risk to see the latest `risk_events`.
- **Inspect the filter (Backtests):** open Backtests → see promoted **and**
  rejected candidates with Sharpe / DSR / PBO / maxDD / cost-sensitivity → sort by
  DSR → confirm rejects sit below the threshold (the rigor gate is *visibly*
  working, not a black box).
- **Approve a go-live (Approvals):** a Workflow B run waits on a FORM → operator
  reads the risk-officer summary + one-shot holdout-gate result → **approves** →
  the run issues the start `command`; the worker (not the pod) acts on it.
- **Intervene (Risk):** open Risk → **arm** the kill-switch (or **flatten**) →
  writes a `commands` row; the worker + deadman execute. The cockpit only signals.

## Layout & states

- **Shell:** fixed left **sidebar** (brand + the view nav + signed-in user), a
  **topbar** (active view title + blurb + a live connection pill), scrollable
  content. In-app state navigation (no URL routing — the app runs in the pod
  shell's iframe). Terminal aesthetic: near-black surfaces, **monospace tabular
  figures** for ids/timestamps/money, one green accent, **status as bordered
  badges** (never color alone). Responsive to 375px.
- **Designed states everywhere:** skeletons (not spinners) while loading; helpful
  empty copy naming the next action / milestone; visible, actionable error
  alerts; an unauthenticated state (handled by `AuthGuard`). Every stub view
  states what lands there and in which milestone — no dead "coming soon".

## Build decomposition (M2.x → B2.x)

- **B2.1a — shell** *(this PR):* scaffold (Vite + React + `lemma-sdk`), the
  authenticated shell (sidebar nav over all 8 views, terminal tokens, designed
  states), Overview as a live **connectivity proof** (signed-in user + pod table
  inventory over the SDK), frontend CI. Other views are designed stubs.
- **B2.1b — live Overview:** `useLiveRecords` / `useWatchChanges` tiles + the
  equity chart → completes M2.1 ("watchChanges + charts").
- **M2.2** Overview system-health strip (heartbeats / reconcile / freshness /
  open `risk_events`). **M2.3** Backtests incl. rejected. **M2.4** Approvals FORM
  + Strategies / Discovery / Exchanges / Risk / Config. **M2.5** `desk` copilot +
  Telegram. **2.GATE** walk one scenario; message `desk` on Telegram.

## Deploy

The app lives **inside the pod bundle** (`pod/apps/cockpit/cockpit.json` +
`source/`), so `lemma pods import pod/` builds + ships it. For iteration:
`npm run dev` in `source/` (auto-authed via the dev-token Vite plugin; in the
agent browser use same-origin proxy mode). The outward **deploy** to the live pod
(`lemma apps deploy` / full `lemma pods import`) is an **operator-authorized**
step — see `pod/README.md` — not run unattended.
