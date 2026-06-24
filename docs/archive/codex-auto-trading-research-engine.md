# Codex Design: Alpha Auto-Trading Research Engine

> Codex-authored planning artifact for the Alpha repo. This file is deliberately
> outside `docs/design/` and outside the Vega repo so it does not collide with
> Claude's Vega workflow. It is a companion to Alpha's `BUILD_PLAN.md` and
> `docs/PHASE0_HANDOFF.md`, not a replacement for either. Future agents should
> treat this as a design brief for task planning, while `BUILD_PLAN.md` remains
> the canonical Alpha plan until the operator says otherwise.

## 1. Executive Summary

Alpha should be built as a Lemma-first trading research and execution platform:
the Vault pod is mission control, the AI strategy-discovery brain, the approval
system, and the cockpit; an external `alpha-core` engine and worker handle data,
backtesting, paper trading, live execution, broker reconciliation, and kill
switches. Vega is not the target repo for new Codex work. Vega is the reference
implementation for safety-critical trading patterns.

The core product goal is optimistic but disciplined: search many strategy ideas
across many instruments and timeframes, especially 1-second strategies, while
rejecting fake edges before they reach capital. The system must discover
candidates, backtest them through a rigorous statistical gate, paper-trade
survivors, and make only approved strategies live-eligible. Actual live trading
remains human-gated.

The most important design choice is to use Lemma where it is strongest:

- Tables hold durable shared state, approvals, runs, metrics, commands, and
  worker heartbeat.
- Files under `/knowledge` hold manuals, broker docs, research notes, strategy
  writeups, and generated reports, with built-in RAG.
- Functions perform deterministic pod-side coordination and validation.
- Agents provide strategy generation, quant review, risk review, and operator
  copilot judgment.
- Workflows encode discovery, paper evaluation, and deployment approval.
- Schedules start nightly discovery, paper evaluation, heartbeat checks, and
  event reactions.
- A Vault app is the cockpit for research, approvals, data health, and live ops.
- A Telegram surface gives the operator alerts and chat control.

The pod never places live orders. It governs, observes, approves, and commands.
The worker alone holds live secrets and touches broker order APIs.

## 2. Existing Alpha Context

Alpha already has a finalized plan in `BUILD_PLAN.md`. The local files establish
these decisions:

- Repo: Alpha.
- Pod: Lemma Vault, id `019ef606-7b77-76f1-853a-978ddf819415`.
- Worker engine: hand-rolled `alpha-core`, porting Vega patterns.
- Phase-1 research: one shared backtest engine on a research box.
- Execution: external worker, one source of truth for live broker actions.
- Venue split: Delta for live crypto derivatives, Binance for free/testnet
  crypto research, Kite for Indian equities/F&O/currency derivatives.
- Secrets: live secrets stay on the worker; the pod may relay the daily Kite
  access token but not the Kite API secret.
- Lemma money type: verify DECIMAL/NUMERIC first; otherwise store money as
  string-encoded Decimal.
- Build environment: Mac Lemma CLI session.

The Codex design below preserves those decisions and turns them into an
implementation-facing product architecture.

## 3. What Vega Contributes

Vega should be mined for proven trading-engine patterns, not edited by Codex for
this Alpha planning work. Useful Vega references:

- Venue-agnostic strategy interface and strategy engine.
- Decimal money and timezone-aware UTC discipline.
- Cost model with Indian fee stack, crypto TDS, slippage stress.
- Risk manager with non-bypassable pre-trade checks.
- Latching kill switch and restart-safe state.
- Idempotent OMS and order finite-state machine.
- Broker-as-truth reconciliation.
- Paper/live strategy parity.
- 1-second live bar configuration.
- Options contracts, Greeks, roll/settle planning, and Greeks exposure caps.

Vega's main limitation for Alpha is not trading safety. It lacks the Lemma pod
brain: AI discovery, pod tables, workflows, approvals, RAG, and operator mission
control. Alpha builds those around a shared `alpha-core` engine.

## 4. Target Architecture

```text
                              Lemma Vault Pod
        ----------------------------------------------------------------
        Tables       Files/RAG       Functions       Agents
        Workflows    Schedules       App             Telegram Surface
             |             |              |              |
             | governance, research, approvals, commands |
             v
      ------------------------------------------------------------------
      Research Box / Worker Host
      ------------------------------------------------------------------
      Data capture -> Data lake -> alpha-core backtester -> rigor gate
                                      |
                                      v
                            paper worker / live worker
                                      |
                                      v
                          broker adapters and venues
                  Kite | Delta India | Binance testnet/data
```

Rules:

- The same `alpha-core` kernel runs backtest, paper, and live.
- The pod never runs a second lightweight backtester.
- The pod never places live orders.
- Raw high-frequency data stays in the worker data lake.
- The pod receives summaries, metrics, artifacts, and snapshots.
- Human approval is expressed through workflow FORM nodes and command rows.
- Fast risk controls live on the worker; pod controls are governance and audit.

## 5. Lemma Vault Pod Design

### 5.1 Shared Tables

Use shared tables for team-level trading state. Default to `enable_rls: false`
unless a row is truly personal.

- `strategies`: strategy definitions, family, source, template/code reference,
  current lifecycle status, owner notes.
- `strategy_versions`: immutable version records with params, code hash,
  prompt lineage, primitive set, and sandbox status.
- `datasets`: dataset versions, source, venue, symbols, timeframe, quality
  metrics, coverage, and eligibility for 1-second/sub-second research.
- `experiments`: search runs with strategy family, search cell, sampler, budget,
  seed, data version, engine version, and status.
- `backtests`: metrics and rigor outputs, including CPCV/PBO, deflated Sharpe,
  walk-forward, holdout flags, stress results, and artifact paths.
- `paper_runs`: paper deployment windows, venues, symbols, status, tracking
  error, rejects, halts, reconciliation state, and operator notes.
- `deployments`: approved strategy/version/risk config intended for paper or
  live eligibility.
- `risk_limits`: per-deployment risk settings, capital allocation, daily loss,
  exposure, order-rate, Greeks caps, and venue-specific overrides.
- `commands`: append-only operator or workflow commands for the worker, such as
  start paper, stop, flatten, clear halt, and emergency flatten.
- `worker_status`: heartbeat, mode, armed state, positions hash, build version,
  last reconcile timestamp, and current halt status.
- `orders`, `fills`, `positions`, `pnl_snapshots`: worker-reported state for the
  cockpit and audit.
- `risk_events`: kill-switch trips, limit breaches, stale-feed events,
  reconcile mismatches, manual interventions.
- `research_ledger`: keyed trial counters for deflated Sharpe and overfitting
  control, partitioned by market, strategy family, and data window.

Money fields should use native DECIMAL/NUMERIC if Lemma supports it. If not,
store lossless string Decimals and keep numeric ratios as FLOAT.

### 5.2 Files And RAG

Use files as the pod's knowledge and artifact layer:

- `/knowledge/brokers`: Kite, Delta, Binance, SEBI, exchange, and broker docs.
- `/knowledge/markets`: market-structure notes, instrument rules, tax/fee docs.
- `/knowledge/strategies`: strategy memos, rejected ideas, rationale, prompts.
- `/knowledge/risk`: risk policy, capital staging, incident runbooks.
- `/reports/backtests`: generated research reports.
- `/reports/paper`: paper trading review reports.
- `/reports/deployments`: final approval packets.

Agents should retrieve from scoped folders instead of broad pod-wide search.
Structured data remains in tables and the worker data lake, not as uploaded CSVs
for RAG.

### 5.3 Functions

Functions should be deterministic, auditable, and narrow:

- `request_backtest`: validates a candidate, writes an experiment request, and
  queues it for the research box.
- `record_backtest_result`: atomically writes backtest metrics, artifacts, and
  ledger updates.
- `start_paper_run`: validates promotion preconditions and writes the command
  row consumed by the worker.
- `evaluate_paper_run`: computes paper-vs-backtest divergence and readiness.
- `issue_command`: writes command rows with idempotency and authorization checks.
- `clear_halt`: permits re-arm only after clean worker reconciliation.
- `refresh_broker_token`: relays the daily Kite access token only.
- `compute_risk_check`: deterministic review of proposed deployment limits.

Do not wrap a single agent call in a function. Do not use functions for direct
single-row writes unless coordinated validation or multiple writes are needed.

### 5.4 Agents

Use a small set of focused agents with typed outputs:

- `strategist`: proposes strategies and parameter ranges. Default mode is
  constrained templates and whitelisted primitives. Free-form code generation is
  allowed only after OS-level sandboxing exists.
- `quant_analyst`: reviews backtests, CPCV/PBO, deflated Sharpe, holdout usage,
  sensitivity, regime behavior, and paper-vs-backtest divergence.
- `risk_officer`: evaluates capital, exposure, order rate, margin, Greeks, and
  kill-switch settings for a deployment.
- `desk`: operator copilot in the app and Telegram surface; can call the other
  agents/tools but does not bypass approvals.

Every agent output that feeds a workflow or table should have an output schema.
The strategist must never see the locked holdout.

### 5.5 Workflows

Use workflows where Alpha needs durable checkpoints and human approvals.

- `discovery_cycle`: nightly or manual workflow. Selects a search cell, invokes
  strategist, requests backtests, loops over candidates, records results, and
  promotes only robust survivors to paper-candidate status.
- `paper_evaluation`: scheduled workflow. Reviews paper runs when enough time or
  trades have accumulated, asks quant_analyst for divergence analysis, and marks
  passed/failed.
- `deployment_approval`: DATASTORE-triggered when a paper run passes. Runs risk
  review, one-shot holdout if still unused, then presents a FORM to the operator.
  Approval writes a deployment or command row; it does not arm live directly.
- `incident_review`: triggered by severe `risk_events`. Summarizes the event,
  affected deployments, positions, reconciler output, and next safe action.

### 5.6 Schedules And Triggers

- `nightly_discovery`: TIME schedule for discovery_cycle.
- `paper_evaluator`: TIME schedule for paper_evaluation.
- `worker_watchdog`: TIME schedule to inspect `worker_status` and alert on stale
  heartbeat or pod-vs-worker divergence.
- `deployment_trigger`: DATASTORE schedule on paper pass events.
- `risk_event_alert`: DATASTORE schedule on severe risk events.
- `daily_kite_token_check`: TIME schedule to confirm token freshness and prompt
  the operator through Telegram if needed.

### 5.7 App And Surface

The Vault app is the product surface. It should use `watchChanges` rather than
polling for table updates.

App pages:

- Overview: P&L, deployments, worker health, data freshness, active risks.
- Discovery: experiments, candidate queue, rejected-candidate visibility.
- Backtests: leaderboard, equity curves, drawdowns, DSR/PBO/holdout status.
- Data: dataset coverage, quality, gaps, capture jobs, instrument metadata.
- Paper: active paper runs, divergence, rejects, halts, approval readiness.
- Approvals: workflow FORM inbox and deployment packets.
- Risk: limits, kill events, flatten/clear-halt command UI with confirmation.
- Copilot: desk agent chat grounded in current pod state and `/knowledge`.

Telegram surface:

- Alerts for stale worker, kill switch, paper pass/fail, approval pending, token
  freshness, and severe reconciliation issues.
- Operator chat with `desk` for status and safe command initiation.

## 6. Worker And Data Architecture

### 6.1 alpha-core

`alpha-core` is the shared Python kernel used by research, paper, and live:

- Strategy contract.
- Bar and event replay.
- Feature and primitive library.
- Cost model.
- Fill simulation.
- Risk gate.
- OMS/order state pattern.
- Rigor gate integrations.
- Clock injection for parity.

Both research and worker import this package. There must be no separate pod-side
toy backtester.

### 6.2 Data Lake

High-resolution data belongs outside the pod, on the research/worker host:

```text
data_lake/
  raw/kite/
  raw/delta/
  raw/binance/
  normalized/ticks/
  normalized/quotes/
  normalized/trades/
  normalized/order_book/
  normalized/bars/
  metadata/instruments/
  metadata/dataset_versions/
```

Storage defaults:

- Parquet for cold normalized data.
- DuckDB for research queries.
- ClickHouse later for warm/high-volume worker queries if needed.

Dataset metadata is summarized into Lemma `datasets`; the raw data itself does
not cross into the pod.

### 6.3 Canonical Market Data

Capture and normalize:

- trades,
- best bid/ask quotes,
- top-N order book snapshots where available,
- order book deltas where reliable,
- bars at 1s, 5s, 15s, 1m, 5m, 15m, 1h, 1d,
- instrument metadata and option contract snapshots.

Each dataset version must record coverage, gaps, duplicate rate, stale intervals,
outliers, bid/ask inversions, corporate-action status, and whether it is eligible
for 1-second or sub-second research.

### 6.4 1-Second Data Policy

Valid 1-second backtests require true tick/trade/quote/order-book data or
replayed live-captured data. Kite minute candles are useful for minute and daily
research, not proof of a 1-second strategy. Binance streams are the cheapest
place to start building 1-second crypto research data. Kite and Delta 1-second
datasets should be built by capture first, with paid data considered only when a
specific strategy or coverage gap justifies the cost.

## 7. Venue And Instrument Strategy

### Kite

Primary Indian venue for:

- NSE cash equities,
- Nifty and BankNifty index options,
- selected stock options later,
- exchange-traded currency derivatives where legal and broker-supported.

Use Kite WebSocket for live capture and Kite historical for minute/daily backfill.
Daily access token relay is allowed through Vault; API secret stays off-pod.

### Delta India

Primary live crypto derivatives venue:

- BTC and ETH options first,
- SOL options if liquidity and metadata are adequate,
- perps for hedging and directional strategies.

Validate tick size, lot size, multipliers, settlement, funding, margin, and API
rate behavior before paper promotion.

### Binance

Research and testing venue:

- cheap public data,
- 1-second kline/trade/depth streams,
- testnet paper,
- broad crypto experimentation before Delta-specific validation.

Do not assume Binance results transfer to Delta without fee, liquidity, funding,
contract, and execution-model adjustment.

### Future Brokers

Dhan is the first Indian backup candidate to evaluate. Add another broker only
when it solves a concrete gap: better data, better reliability, broader
instruments, lower costs, or redundancy for an already proven strategy.

## 8. Research And Rigor

The gate must be stricter than Vega's because Alpha's AI system can generate many
candidates quickly.

Required gates:

- look-ahead audit,
- realistic fees, taxes, spread, slippage, and latency,
- 2x and 3x slippage stress,
- walk-forward stability,
- combinatorial purged cross-validation with embargo,
- probability of backtest overfitting,
- deflated Sharpe using a keyed persistent trial ledger,
- locked one-shot holdout,
- instrument and regime holdouts,
- parameter sensitivity,
- capacity and turnover checks,
- paper-vs-backtest divergence review.

The trial ledger must be keyed by market, strategy family, and data window. A
single global count is useful for audit but too blunt for DSR.

Control strategies must calibrate the gate before nightly AI discovery:

- random strategy must fail,
- deliberately overfit strategy must fail,
- known-decent strategy should pass structural checks even if not promoted.

## 9. Promotion Lifecycle

Use this lifecycle everywhere:

```text
proposed -> backtest_requested -> backtest_failed
         -> backtest_passed -> paper_requested -> paper_running
         -> paper_failed -> paper_passed -> approval_pending
         -> live_eligible -> live_armed
```

Rules:

- `live_eligible` is not live.
- `live_armed` requires explicit operator approval and worker live gate.
- Failed candidates remain visible so the filter can be audited.
- Every promotion records the exact strategy version, dataset version, engine
  version, cost model version, risk config, and operator approval.

## 10. Risk And Capital Defaults

Default pilot posture:

- INR 25,000 to INR 100,000 equivalent starting capital.
- 2% daily kill switch until evidence supports widening.
- 15-20% max position per instrument.
- 100% max gross exposure unless explicitly approved.
- Conservative max orders per minute.
- No leverage increases in early pilot.
- No naked option shorts unless margin, settlement, Greeks, and flatten behavior
  are modeled and approved.

Worker-local risk remains authoritative. Pod commands request actions; the worker
must still validate every action through its local risk system.

## 11. Implementation Phases

### C0: Codex Planning Alignment

Done when:

- this Codex doc exists in Alpha `docs/`,
- no Vega files are changed,
- `docs/design/` is absent,
- Alpha's existing plan is acknowledged,
- Lemma Vault is explicitly part of the design.

### C1: Lemma Vault Schema Plan

Design table schemas, enum states, file folders, function names, agent names,
workflow names, schedules, app pages, grants, and seed data.

Done when:

- each resource has a stable name,
- shared vs RLS table choices are documented,
- money storage type is verified,
- a final pod bundle plan can be generated.

### C2: alpha-core Skeleton

Create the shared engine package with interfaces, clocks, market data models,
cost model hooks, risk hooks, and empty strategy primitives.

Done when:

- research and worker can import the same package,
- parity tests prove the same strategy path is used in backtest and paper.

### C3: Data Lake And Capture

Implement Binance capture first, then Kite and Delta capture.

Done when:

- 1-second Binance datasets are replayable,
- dataset quality summaries are written to Vault,
- Kite/Delta capture paths produce normalized metadata and bars.

### C4: Experiment Registry And Rigor Gate

Implement experiment records, backtest result ingestion, keyed trial ledger, and
rigor outputs.

Done when:

- every backtest is reproducible from stored versions,
- control strategies calibrate the gate,
- failed candidates remain queryable.

### C5: Lemma Agents And Workflows

Build strategist, quant_analyst, risk_officer, desk, discovery_cycle,
paper_evaluation, and deployment_approval.

Done when:

- workflows run end-to-end on seed data,
- FORM approval gates work,
- agents have typed outputs and scoped grants.

### C6: Vault Cockpit

Build the app pages and Telegram surface.

Done when:

- operator can see data health, candidates, failed filters, approvals, paper
  runs, worker heartbeat, and risk events,
- UI updates via table subscriptions.

### C7: Paper Worker And Promotion

Wire worker paper mode to Vault commands and status.

Done when:

- a backtest survivor can be paper-traded,
- paper results are evaluated,
- passing paper creates approval_pending but not live_armed.

## 12. First Task List To Derive Later

Start with:

1. Verify Lemma numeric column support for money.
2. Draft Vault table schema and enums.
3. Draft `/knowledge` and `/reports` folder layout.
4. Draft pod function, agent, workflow, schedule, app, and surface resource list.
5. Scaffold `alpha-core`.
6. Implement Binance data capture to Parquet.
7. Implement deterministic tick-to-1-second bar aggregation.
8. Add dataset quality summary ingestion into Vault.
9. Add experiment and backtest registry writes.
10. Implement the first calibrated rigor gate with control strategies.

## 13. Non-Interference Rules

- Do not edit Vega unless the operator explicitly asks.
- Do not edit Claude-owned Vega task files from Codex.
- Keep Codex planning in Alpha and clearly named Codex files.
- Do not expose `.env` values; report only presence.
- Do not place live orders.
- Do not flip live gates.
- Do not treat synthetic 1-second data as proof.
- Do not let pod workflows bypass worker-local risk.
- Preserve Alpha's existing `BUILD_PLAN.md` decisions unless the operator
  explicitly changes them.

## 14. Final Position

Alpha should use Lemma aggressively for what Lemma is good at: durable shared
state, RAG, AI agents, human approvals, workflows, schedules, app UI, and
surfaces. It should use an external `alpha-core` and worker for what must stay
outside the pod: high-frequency data, compute-heavy backtests, live secrets,
broker order APIs, fast risk controls, reconciliation, and deadman flattening.

That split gives Alpha the best of both systems: a powerful AI-driven mission
control layer in Vault and a disciplined execution engine modeled after Vega.
