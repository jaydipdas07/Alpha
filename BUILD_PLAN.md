# Alpha — Finalized Plan (v2, refined)

> This is the **refined/finalized** companion to the original plan at
> `/Users/jaydipdas/.claude/plans/recursive-marinating-kurzweil.md`. It supersedes that doc
> where they differ. It is self-contained so the cloud **Ultraplan** session can read it by
> absolute path. **Build has NOT started — finalize first.**
> Reference repo (read-only, port patterns not code): Vega at `/Users/jaydipdas/Code/Vega`.
> Lemma pod: **Vault** (id `019ef606-7b77-76f1-853a-978ddf819415`), the active pod in the CLI.

## Context

We pressure-tested the original Alpha plan against (a) Lemma's actual primitives and (b) a
direct read of Vega's source. The plan was strong; this refinement closes the gaps that would
otherwise bite — chiefly **backtest≡live parity across the pod/worker split**, **statistical
rigor under AI-rate strategy generation**, and **a clean single source of truth for execution**.

**Vega code audit (the basis for the engine decision).** ~13k LOC src vs ~13.5k LOC tests
(>1:1), `fail_under=94`, `mypy --strict`, property + replay + chaos tests, 18 ADRs. Read in full:
`core/interfaces.py` (venue-agnostic `Strategy`/`BrokerAdapter` ABCs; idempotent `place_order`
with dedup-by-query), `risk/manager.py` (non-bypassable 10-step gate, latching kill-switch that
survives restart via `restore()`, in-flight working-exposure reservation), `execution/reconcile.py`
(broker-truth adopt-vs-halt with explained/unexplained drift separation + phantom-position
zeroing), `execution/costs.py` (itemized Indian cost stack + crypto TDS, price-taker, 2× stress),
`core/order_fsm.py` (complete 48-cell table, fill dedup, over-fill detection, caller-supplied
`now` for parity), `backtest/rigor.py` (look-ahead audit + walk-forward + 2× stress). **Verdict:
the engine is production-grade and de-risked.** Alpha's risk is NOT the engine — it is the AI
discovery brain and the statistics riding on top.

## Decisions locked this session

| # | Decision | Choice |
|---|---|---|
| Q1 | Worker engine | **Hand-rolled, port Vega patterns** into a shared `alpha-core` kernel. (NautilusTrader reconsidered only at Phase 5 if latency/scale demands change.) |
| Q2 | Where Phase-1 backtests run | **Single engine on a research box** (Mac-local first, cheap cloud later). Pod orchestrates; no money/keys/static-IP. Guarantees one engine = parity. |
| Q3 | Strategist code-gen freedom | **Both** — constrained (parameterize vetted templates + whitelisted primitives) as the default track from Phase 1b; **sandboxed free-form code-gen** as an additive track (Phase 1c), both feeding the *same* rigor gate and the *same* `alpha-core` execution path. |

**Why Q1 = hand-rolled (evidence-based).** (1) The hard, risky part of Alpha is the AI brain +
statistics, which Nautilus does nothing to solve. (2) Vega's `Strategy` ABC + `StrategyEngine` +
`CostModel` + `RiskManager` + `order_fsm.step` are *already* the exact shape of the shared kernel
Q2 needs (identical code in backtest and live, `now` injected for parity) — a ready-made blueprint.
(3) Nautilus would impose its strategy format on the strategist agent (fighting both Q3 tracks),
add a heavy learning curve, and buy speed Alpha doesn't need (SEBI ≤10 OPS, ~1s equity tick,
sub-second crypto are all well within a Python event engine). Nautilus only wins for microsecond
HFT, a large out-of-the-box venue/indicator library, or a from-zero build — none apply here.

**Open items resolved (recommendations; say the word to override):**
- Notification surface → **Telegram**. Pod name → **keep `Vault`** (repo=Alpha, pod=Vault).
- Paid data → **start free** (Binance WS/historical, Dukascopy forex, Kite historical via the
  ₹500 sub, NSE bhavcopy); buy TrueData/Polygon only when a specific coverage/frequency gap blocks
  a strategy.
- Capital staging → keep **1%→5%→25%→full**, each step gated on live-vs-paper tracking error within
  tolerance for N sessions **and** zero `risk_events`.
- Secrets → **live secrets stay on the worker** (gitignored, à la Vega); the pod relays only the
  **daily Kite access token** (2FA on the Mac → token written to an RLS row → worker reads it). The
  API *secret* never leaves the Mac/worker. Composio reserved for ancillary connectors.

## Architecture (refined)

```
        ┌────────────  VAULT POD (Lemma — Mission Control + AI Brain)  ────────────┐
 you ──▶│ Tables(book) · Files(/knowledge RAG) · Functions(mkt-data, orchestrate)   │
(browser│ Agents: strategist · quant-analyst · risk-officer · desk(copilot)         │
 /TG)   │ Workflows: A discovery-cycle · B deployment-approval   Schedules(slow)    │
        │ App: cockpit (watchChanges + Chart.js) · Surface: telegram                │
        └──▲────────────────────────────────────────────────────┬──────────────────┘
   reads:  │ approved deployments + risk_limits + commands         │ SDK writes(batched):
   writes: │ workflow approvals                                    ▼ orders/fills/positions/
        ┌──┴─ RESEARCH BOX (Phase 1+, no keys) ──┐   ┌── EXECUTION WORKER (Phase 3+, VPS) ──┐
        │ alpha-core backtester (THE one engine)  │   │ alpha-core LIVE: venue adapters as    │
        │ rigor: lookahead+WF+CPCV+DSR+holdout    │   │ isolated procs (binance/delta/kite),  │
        │ data plane: Parquet/DuckDB(cold)        │   │ WS→downsample→strategy→risk→OMS→broker│
        └─────────────────────────────────────────┘   │ +ClickHouse(warm), heartbeat, ≤10 OPS │
        the SAME alpha-core kernel runs in both ───────┘ reconcile(broker=truth), kill-switch  │
                                                       └───────────────────────────────────────┘
```

### Key tightenings (these change the build)

1. **One engine: the `alpha-core` Python package.** A single library holding the `Strategy`
   contract, bar construction, indicator/primitive library, fill simulation, the cost model, and
   the risk gate. **Both** the research-box backtester and the worker import it — this is the
   parity guarantee. There is no second "light JOB-function backtester." (Ports Vega
   `core/interfaces.py`, `strategy/engine.py`, `execution/costs.py`, `risk/manager.py`,
   `core/order_fsm.py` as design references.)
2. **The pod never places live orders.** Execution + broker-truth live only on the worker. The
   pod's broker functions reduce to **market data** (`fetch_historical`, `get_quote`) and
   **read-for-reconcile** (`get_positions`); intent to trade is expressed via the `commands` table.
   This removes a dual-source-of-truth hazard.
3. **Explicit data boundary (what crosses the SDK).** Worker → pod: orders/fills/positions on
   event (idempotent), `pnl_snapshots` on a cadence (per-bar/per-minute), optionally throttled
   `signals`; **never raw ticks**. Raw ticks/bars stay in the worker's ClickHouse; the pod's `bars`
   table is downsampled-for-charts only.
4. **Money across the boundary is never a float.** Store prices/PnL in Lemma as **integer minor
   units or strings** (verify Lemma's numeric column types in Phase 0 and pick accordingly).
   Inside `alpha-core`, `Decimal` throughout; tz-aware UTC; `now` injected (never wall-clock in the
   engine) — exactly Vega's parity discipline.
5. **Worker liveness is first-class.** New `worker_status` table (last_seen, mode, armed state,
   positions hash, build version) + a schedule that alerts on stale heartbeat / pod-vs-worker
   position divergence. (Original plan named a watchdog but gave it no home.)
6. **Split the lifecycle into two workflows + an evaluator** (don't park one run for a multi-week
   paper window): see Workflows below.

## The rigor gate — the genuinely new work (crown-jewel integrity)

Vega's gate (look-ahead + walk-forward + 2× stress) was calibrated for **human-rate** proposal.
An LLM strategist proposing dozens of candidates nightly **inverts the multiple-testing regime**,
so Vega's gate is necessary but far from sufficient. Alpha **adds** (net-new; not in Vega):

- **Combinatorial Purged Cross-Validation (CPCV)** with embargo — replace/augment the simple
  sequential walk-forward; report **PBO** (probability of backtest overfitting).
- **Deflated Sharpe Ratio** fed by a **global, persistent trial ledger** — the cumulative count of
  *all* candidates ever tested against the relevant data, not per-`discovery_run`. Without the
  global ledger, DSR is theater. (Store the running trial count; every backtest reads/increments it.)
- **Locked one-shot holdout** — a final time window the strategist **never** sees and the research
  loop **never** tunes against; used **exactly once** per strategy as the last gate before the
  approval FORM. Recorded on the `backtests` row.
- **Reproducibility for audit** — every backtest records strategy version/params, dataset
  version/hash, `alpha-core` engine version, cost-model version, and RNG seed.
- **Calibration before automation** — before the strategist is trusted (Phase 1a→1b gate), run the
  full gate against **control strategies**: a deliberately overfit one it must **reject**, a random
  one it must **reject**, a known-decent one it passes. Trust the filter before automating it.

## Pod design (Vault bundle) — refinements to the original tables/functions/agents

Tables stay as in the original plan (shared unless noted), with these **changes/additions**:
- `discovery_runs`: `trial_count` is the **per-run** count; add a singleton/`research_ledger` row
  (or table) for the **global cumulative trial count** that DSR consumes.
- `backtests`: add `cpcv_pbo`, `deflated_sharpe`, `holdout_used` BOOL + `holdout_metrics` JSON,
  `dataset_version`, `engine_version`, `cost_model_version`, `seed`. Money fields as minor-units/string.
- **New `worker_status`** (shared): `worker_id`, `last_seen`, `mode` ENUM(paper/live), `armed` BOOL,
  `positions_hash`, `build_version`, `detail` JSON.
- `broker_credentials` (RLS): used primarily as the **daily Kite token relay** (`access_token`,
  `token_refreshed_at`), not as the live secret store.
- Idempotency keys: `orders.idempotency_key` unique; `fills` unique on (order_id, venue_fill_id);
  `pnl_snapshots` unique on (deployment_id, ts) — DATASTORE triggers are at-least-once.

Functions (refined):
- **Market data only** in the pod broker layer: `fetch_historical(exchange,symbol,freq,start,end)`,
  `get_quote`, `get_positions` (read, for reconcile/dashboard). **Drop `place_order`/`cancel_order`
  from the pod's live path** — execution is worker-only; the pod issues `commands`.
- Orchestration: `request_backtest` (writes a request the research box runs; results return via
  SDK), `start_paper_run`, `evaluate_paper_run`, `issue_command`, `arm_kill_switch`/`flatten_all`,
  **`clear_halt`** (re-arm; succeeds only if reconcile is clean — ports Vega's `rearm`),
  `refresh_broker_token` (token relay), `compute_risk_check`.
- The heavy backtest itself runs in `alpha-core` on the research box, **not** inside a Lemma
  function (avoids JOB time limits and a second engine).

Agents (the crown jewel) — refined:
- **`strategist`** — two tracks sharing one rigor gate: **(default) constrained** — parameterize a
  human-vetted template library + whitelisted indicators/primitives; **(additive) sandboxed
  free-form** code-gen executed only in a hardened, no-I/O sandbox. Proposes from economic rationale
  + in-sample data; **never sees the holdout**. Emits originality check + trial-ledger increment.
- **`quant-analyst`** — one agent, two modes (backtest robustness; paper divergence): CPCV/PBO,
  DSR vs trial count, look-ahead/survivorship, cost sensitivity, regime stability → promote/reject/revise.
- **`risk-officer`** — proposed `risk_limits` per deployment (sizing, exposure, kill thresholds).
- **`desk`** — copilot in app + Telegram; the other three as sub-agent tools; book read + confirm-gated actions.

Workflows (split):
- **A. `discovery-cycle`** (nightly schedule): `request_backtest` data prep → AGENT strategist →
  LOOP candidates { `request_backtest`(full rigor incl CPCV/DSR, holdout untouched) → AGENT
  quant-analyst → DECISION promote? } → for survivors `start_paper_run` (worker, paper). Terminates
  (no multi-week wait inside the run).
- **Evaluator** (schedule, e.g. daily): `evaluate_paper_run` → when window complete, AGENT
  quant-analyst (paper-vs-backtest divergence) → DECISION paper_validated? → on pass set status
  (DATASTORE-triggers Workflow B).
- **B. `deployment-approval`** (DATASTORE trigger on `paper_runs.status=passed`): AGENT risk-officer
  → **one-shot holdout backtest** (final locked-window gate) → **FORM "Approve deployment"** (assigned
  to you; the durable human gate; app Approvals inbox + Telegram) → DECISION approved? →
  `issue_command(start)`. Fast kill-switch stays worker-local; pod = governance/audit.

Schedules (minute-floor): `daily-reauth` (~08:45 IST token relay) · `reconcile` · `risk-sweep`
(coarse) · `nightly-discovery`→Workflow A · `paper-evaluator` · DATASTORE on
`deployments(pending_approval)`→notify · DATASTORE on `worker_status`(stale)/`risk_events`→alert.

App `cockpit` (Vite + lemma-sdk, `watchChanges`, Chart.js): Overview (P&L, equity, positions, risk
**+ system-health strip: worker heartbeat, last reconcile, data freshness, open risk_events**) ·
Strategies · Discovery · Backtests (Sharpe/DSR/PBO/maxDD/cost-sensitivity, **incl. rejected
candidates so the filter is visibly working**) · Approvals (FORM inbox) · Exchanges & Instruments ·
Risk (arm kill-switch/flatten/clear-halt) · Config · Copilot. Surface: `telegram`.

## Worker design (fresh build; Vega patterns as reference)

Python on a static-IP VPS (SEBI). Imports `alpha-core` (the shared kernel). Venue adapters as
**isolated processes** (Binance, Delta via ccxt, Kite via pykiteconnect), each with own
lock/state/metrics/risk budget. Holds WS → ingest → downsample → strategy → **non-bypassable
pre-trade risk + latching kill-switch (fast, survives restart)** → idempotent OMS (order FSM) →
broker → reconcile (broker=truth). Reports to Vault via `Pod.from_env()` (batched). Polls
`commands`; reads approved `deployments` + `risk_limits`; writes `worker_status` heartbeat.
**Add perp funding-rate accrual** (Vega's per-order `CostModel` doesn't carry it) into both the
backtest and live P&L. ClickHouse (warm) + Parquet/DuckDB (cold).

## Vega-lift matrix (port-heavy vs clean-sheet)

| Subsystem | Action | Vega reference |
|---|---|---|
| Strategy contract, engine, parity discipline (`now` injection) | **Port-heavy** | `core/interfaces.py`, `strategy/engine.py`, `core/order_fsm.py` |
| Risk gate + latching kill-switch + restart-safety + in-flight reservation | **Port-heavy** | `risk/manager.py`, `risk/limits.py` |
| Reconcile (adopt/halt, drift separation, phantom zeroing) + re-arm-on-clean | **Port-heavy** | `execution/reconcile.py` |
| Cost model (Indian stack + TDS, price-taker, 2× stress) | **Port-heavy**, + add funding accrual | `execution/costs.py` |
| OMS idempotency + audit | **Port-heavy** | `execution/oms.py`, `execution/state.py` |
| Look-ahead/walk-forward/stress | **Port** then **extend** with CPCV/DSR/holdout | `backtest/rigor.py` |
| Venue adapters (kite/ccxt) | **Port patterns** (token flow, normalization) | `adapters/kite.py`, `adapters/crypto_ccxt.py` |
| Lemma brain (tables/agents/workflows/app), pod↔worker SDK integration, ClickHouse data plane, AI rigor (CPCV/DSR/trial ledger/holdout) | **Clean-sheet** | n/a (Vega has none) |

## Sharpened phased roadmap

- **Phase 0 — Foundation.** `git init` Alpha; scaffold `pod/` (pod.json + all tables + file folders
  + seed) + `worker/` skeleton + **`alpha-core/`** package skeleton + `docs/`. **Verify Lemma money
  column types** (pick minor-units vs string). Verify tables/RLS/FKs.
- **Phase 1a — Trusted single backtester.** `alpha-core` kernel on the research box: data ingest
  (free sources) → Parquet/DuckDB → backtester with full rigor (look-ahead, CPCV+PBO, DSR+global
  trial ledger, locked holdout, cost model incl. funding). **Calibrate against control strategies**
  (overfit→reject, random→reject, decent→pass). *Gate: do not proceed to 1b until the filter is
  proven.*
- **Phase 1b — AI discovery (constrained track).** `strategist` (templates+primitives) +
  `quant-analyst`, Workflow A `discovery-cycle`, `nightly-discovery`, `/knowledge` RAG. **Delivers
  autonomous discovery + rigorous backtesting, no money/keys.**
- **Phase 1c — Sandboxed free-form code-gen track.** Hardened no-I/O sandbox; same rigor gate +
  same `alpha-core`. Additive; gated behind 1b stability.
- **Phase 2 — Cockpit + copilot + surface.** Vite dashboard, `watchChanges`, Chart.js + system-health
  strip + rejected-candidates view, `desk`, Telegram. Hero moment visible.
- **Phase 3 — Paper + approval gate + worker (paper mode).** Worker on VPS in PAPER (Binance +
  Delta testnet + Kite replay); `start_paper_run`/`evaluate_paper_run`, evaluator + Workflow B,
  `risk-officer`, deploy-approval FORM, `commands`, `worker_status` heartbeat, worker-local + pod
  kill-switch + `clear_halt`, `reconcile`. Verify full paper pipeline incl. approval.
- **Phase 4 — Live (gated, staged).** Worker → LIVE: complete **SEBI algo registration + static IP**
  (in force *now* — today is past 1 Apr 2026), daily re-auth, ≤10 OPS. Staged capital
  1%→5%→25%→full on live-vs-paper match. Full kill-switch + audit. Add Kite currency derivatives + more crypto.
- **Phase 5 — Scale/future.** More venue adapters; portfolio optimization; RL/LLM-agent discovery;
  **reconsider NautilusTrader** only if latency/scale demands change.

## Cross-cutting guardrails (day 1)
Paper-first + per-strategy human FORM approval; fail-closed live (approval + worker arm-token).
Backtest rigor as above. Risk: non-bypassable pre-trade caps + price collars + data-freshness +
latching kill-switch w/ auto-flatten (worker-fast + pod governance) + immutable audit; Decimal
money (minor-units/string at the Lemma boundary) / tz-UTC. Compliance: SEBI ≤10 OPS + static IP +
algo-registration + daily re-auth (in force now); SEBI brokers + FIU Delta; no offshore forex;
crypto via derivatives; CA sign-off on tax. Pluggability: `exchanges` registry + adapter dispatch
(pod) + isolated-process adapters (worker); every tunable a config row surfaced in Cockpit.

## Verification (per-layer, bottom-up) + success criteria
1. **alpha-core / rigor:** control-strategy calibration passes (overfit & random rejected, decent
   passes); a backtest is bit-for-bit reproducible from its recorded version/seed.
2. **Tables:** `lemma records create` + `lemma query run`; confirm columns/FKs/RLS + money type.
3. **Files:** upload a `/knowledge` doc, `stat`→COMPLETED, `files search`.
4. **Functions:** run `fetch_historical`, `request_backtest`, `clear_halt`; confirm grants.
5. **Agents:** `lemma agents chat strategist "propose a mean-reversion strategy for BTC-perp 5-min"`.
6. **Workflows:** run `discovery-cycle`; trip Workflow B; submit the approval FORM; inspect runs.
7. **Schedules:** fire `nightly-discovery` once; confirm `worker_status`-stale alert; pause tests.
8. **App/Surface:** walk one Cockpit scenario; message `desk` on Telegram.
9. **Whole thing:** discovery → strategist proposes → rigor (CPCV/DSR/look-ahead-clean, holdout
   untouched) → quant-analyst promotes → paper run → divergence in tolerance → risk-officer limits →
   one-shot holdout gate → **approval FORM → you approve** → command → worker (paper) starts →
   orders/fills/positions/heartbeat flow back → **Cockpit live P&L** → arm kill-switch flattens &
   latches; `clear_halt` only re-arms on clean reconcile.

## Non-bundled setup (README)
Broker accounts (Kite Connect sub + key, Delta keys, Binance data keys); live secrets on the
worker; daily Kite token relay (Mac 2FA → RLS row); Telegram bot token + surface; research box
(Phase 1) then VPS + static IP + ClickHouse/Parquet (Phase 3); `/knowledge` uploads; `seed/seed.sh`
(incl. a rejected candidate + a passing one); CA consult on crypto-derivative + algo-income tax.

## Remaining items to confirm
- Lemma money column type (decide in Phase 0 verification).
- CPCV embargo size + DSR significance threshold + control-strategy definitions (decide in Phase 1a).
- Capital-staging promotion tolerance + session count (decide before Phase 4).
- How this file feeds the canonical plan: either merge into
  `recursive-marinating-kurzweil.md` (post-approval, outside plan mode) or point the cloud Ultraplan
  session at this path.
