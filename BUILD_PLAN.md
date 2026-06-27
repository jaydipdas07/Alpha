# Alpha — Finalized Plan (v3, hardened)

> This is the **refined/finalized** companion to the original plan at
> `/Users/jaydipdas/.claude/plans/recursive-marinating-kurzweil.md`. It supersedes that doc
> where they differ. It is self-contained so the cloud **Ultraplan** session can read it by
> absolute path. **Build status lives in `TASKS.md`** (Phases 0–1 complete, Phase 2 next) — this doc is the hardened backbone it executes against, not a live tracker.
> Reference repo: Vega at `/Users/jaydipdas/Code/Vega`. **You own Vega outright** (`Proprietary`,
> authored by you) — verified this session, so code may be **lifted wholesale**, not just patterned.
> **`docs/DESIGN_v4.md` E1 supersedes Q1 below**: the engine is decided by a Phase-0 co-equal A/B
> bake-off (NautilusTrader-shell vs lift-Vega), not pre-locked to hand-rolled.
> Lemma pod: **Vault** (id `019ef606-7b77-76f1-853a-978ddf819415`), the active pod in the CLI.
>
> **v3 (this pass)** adds an adversarial-pressure-test hardening layer on top of v2 — see
> **"v3 Hardening refinements"** below and the two new locked decisions (D1, D2). v3 does not
> change the v2 architecture (pod↔worker split, one-engine parity, the rigor gate are all
> correct); it closes failure modes that lose money or fail *invisibly*. The v3 section is the
> authoritative delta where it touches anything in v2.

## Context

We pressure-tested the original Alpha plan against (a) Lemma's actual primitives and (b) a
direct read of Vega's source. The plan was strong; this refinement closes the gaps that would
otherwise bite — chiefly **backtest≡live parity across the pod/worker split**, **statistical
rigor under AI-rate strategy generation**, and **a clean single source of truth for execution**.

**Vega code audit (VERIFIED this session against the real code; basis for the engine decision).**
src **12,981 LOC** vs tests **13,506 LOC** (>1:1), `fail_under=94`, `mypy --strict`, hypothesis
property + replay (`test_paper_loop`) + chaos tests, **17 numbered ADRs** (0001–0015, 0017, 0018;
0016 missing; 0000 is a template — "18 ADRs" counted files). Verified exact:
`core/interfaces.py` (venue-agnostic `Strategy`/`BrokerAdapter` ABCs; idempotent `place_order` with
`find_order_id` dedup-by-query), `risk/manager.py` (non-bypassable **10-step** gate, halt always
first, latching kill-switch surviving restart via `restore()`, in-flight working-exposure
reservation; `update_pnl(realized,unrealized)` is the kill hook → R13 funding is an additive feed),
`execution/reconcile.py` (broker-truth adopt-vs-halt with explained/unexplained drift separation +
phantom-position zeroing → `trip(RECONCILIATION_MISMATCH)`), `execution/costs.py` (itemized Indian
cost stack + crypto TDS, price-taker, 2× stress — **no perp funding, Alpha adds**),
`core/order_fsm.py` (complete **8×6 = 48-cell** table, every cell named, fill dedup, over-fill
detection, caller-supplied `now`), `backtest/rigor.py` (look-ahead + walk-forward + 2× stress **+ a
bonus cross-instrument portfolio-rigor suite**), `backtest/runner.py` (**event-driven backtester
that reuses the exact live kernel with `FakeClock` — parity without Nautilus**), `execution/state.py`
(event-sourced, append-only + **SHA-256 hash-chained audit log**). **Gems v3 under-used:** ~7
tradeable example strategies (template library for the constrained strategist), options/Greeks,
F&O roll scheduler, paper adapter, the property/replay suite (reusable as a differential oracle).
**Gaps Alpha must add:** perp funding into P&L+gate; Greeks wired into the pre-trade gate (calc
exists, not gated); multi-venue/multi-strategy (Vega = one `active_adapter`/process); market
calendar + live instrument master; shared multi-process state store. **Verdict: the engine is
production-grade, de-risked, AND yours to lift wholesale.** Alpha's risk is NOT the engine — it is
the AI discovery brain and the statistics riding on top.

## Decisions locked this session

| # | Decision | Choice |
|---|---|---|
| Q1 | Worker engine | **Superseded by `docs/DESIGN_v4.md` E1** — engine decided by a Phase-0 **co-equal A/B bake-off**: Track A NautilusTrader-shell + Vega-pattern overlays; Track B **lift Vega wholesale** (your own tested code). No preordained primary/fallback; evidence favors Track B (≈6–8 wk vs ≈14–16, ~80% lower risk). The shared `alpha-core` kernel + the backtest≡live parity guarantee are unchanged either way. |
| Q2 | Where Phase-1 backtests run | **Single engine on a research box** (Mac-local first, cheap cloud later). Pod orchestrates; no money/keys/static-IP. Guarantees one engine = parity. |
| Q3 | Strategist code-gen freedom | **Both** — constrained (parameterize vetted templates + whitelisted primitives) as the default track from Phase 1b; **sandboxed free-form code-gen** as an additive track (Phase 1c), both feeding the *same* rigor gate and the *same* `alpha-core` execution path. |
| D1 | Emergency-flatten when the worker process is dead | **Deadman + exchange-side.** An independent "deadman" process (separate supervisor, same VPS) watches `worker_status` + a pod `commands.emergency_flatten` row and calls broker REST cancel-all/flatten **directly** — it does NOT depend on the main worker. PLUS exchange-side protections that survive client death: Binance/Delta cancel-on-disconnect WS deadman timers + broker-side reduce-only stops persisted per open position. The pod *trips* the deadman; it never places orders. (See R1.) |
| D2 | Phase-1c free-form code-gen | **Keep, hard-gated on true OS-level isolation** (not "no-I/O"). The constrained track (1b) ships first regardless; 1c is gated behind the isolation work in R3, not merely "1b stability." |

**Why a measured bake-off, not a presumption (refined by the Vega review; see `docs/DESIGN_v4.md`
E1).** The original v3 instinct — don't presume Nautilus; the hard, risky part of Alpha is the AI
brain + statistics, which Nautilus does nothing to solve — is **largely vindicated**. The deep Vega
read-through confirmed Vega's `Strategy` ABC + `StrategyEngine` + `CostModel` + `RiskManager` +
`order_fsm.step` + its **parity-proven backtester** are *already* the exact shared kernel Q2 needs
(identical code in backtest and live, `now` injected) — and, crucially, **it is your own proprietary
code, liftable wholesale**, so "Q1 = hand-rolled" is more accurately **"lift Vega."** Nautilus still
imposes its strategy format/learning curve on the strategist agent and buys speed Alpha doesn't need
(SEBI ≤10 OPS, ~1s equity tick, seconds-to-minutes crypto are well within a Python event engine);
its only wins (microsecond HFT, a big out-of-the-box venue/indicator library) don't apply. But
rather than pre-decide, **DESIGN_v4 E1 runs both as a time-boxed Phase-0 A/B** (Nautilus-shell vs
Vega-lift) and picks on parity + integration friction + time-to-market. **Decided → Track B (the
Vega lift): 0.GATE, `docs/adr/0001`.** (The original evidence favored the lift — ≈6–8 wk, ~80% lower risk.)

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

## Vega-lift matrix (VERIFIED this session; you own Vega → lift, don't just pattern)

| Subsystem | Action | Effort | Vega reference |
|---|---|---|---|
| Core models/enums/errors/interfaces (ABCs; Decimal/tz/immutable) | **lift-wholesale** | trivial | `core/{interfaces,models,enums,errors}.py` |
| Order FSM (48-cell, dedup, over-fill, `now`) | **lift-wholesale** | trivial | `core/order_fsm.py` (+ tests) |
| Risk gate (10-step, latching kill+`restore`, working-exposure) | **lift-wholesale** | low | `risk/manager.py`, `risk/limits.py` |
| Reconcile (adopt/halt, drift separation, phantom zeroing, re-arm-on-clean) | **lift-wholesale** | low | `execution/reconcile.py` |
| Cost model (Indian stack + TDS, price-taker, 2× stress) **+ add funding** | **lift + extend** | low (+med funding) | `execution/costs.py`, `config/costs.yaml` |
| OMS + positions + state-store (event-sourced, SHA-256 audit, restore) | **lift-wholesale** | low | `execution/{oms,positions,state}.py` |
| Backtester + rigor (lookahead/WF/stress + portfolio rigor) **+ extend** CPCV/PBO/DSR/holdout/keyed-ledger | **lift + extend** | low lift / **high extend** | `backtest/{runner,rigor}.py` |
| Strategy engine + registry + ~7 example templates | **lift / port-pattern** | low | `strategy/engine.py`, `registry.py`, `strategy/examples/*` |
| Options Greeks (calc exists; **wire into pre-trade gate = net-new**) | **port + extend** | medium | `options/{risk,greeks,chain}.py` |
| Adapters: Delta/Binance (ccxt) + Kite (token flow) | **port-pattern** | medium | `adapters/{crypto_ccxt,kite,factory,paper}.py`, `helpers/kite_auth.py` |
| **Dhan + Upstox adapters** | **clean-sheet** | medium | n/a (build to `BrokerAdapter` ABC) |
| **Multi-venue + multi-strategy orchestration** (Vega = single-adapter/process) | **clean-sheet** | high | `portfolio/loop.py` as pattern only |
| **Indian market calendar + live instrument master** | **clean-sheet** | low–med | n/a |
| **Perp funding accrual → P&L + risk gate** (R13) | **clean-sheet** | medium | `costs.py` ext + `risk.update_pnl` feed |
| **AI rigor** (CPCV/PBO, Deflated Sharpe, keyed global trial ledger, roll-forward holdout, population calibration) | **clean-sheet** | high | n/a — the crown jewel |
| **Lemma brain** (tables/agents/workflows/app), pod↔worker SDK, deadman, ClickHouse data plane | **clean-sheet** | high | n/a (Vega has none) |
| Live event loop | **spike-decided** (Nautilus-owns *or* lift `live.py`) | depends | `live.py`, `app.py` |

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

## v3 Hardening refinements (adversarial pressure-test)

Additive hardening over v2, ordered by severity. The build-blockers are **R1** (dead-worker
flatten — loses real money), **R3** (sandbox RCE), and the pair **R4+R5** (DSR scope + holdout
decay) — the last two are the most dangerous because they degrade *silently*: the system slowly
stops promoting anything (looks like "the market got hard"; is actually counter inflation /
holdout exhaustion).

- **R1 — SEV-1 — Dead-worker emergency flatten (D1).** Pod was stripped of `place_order`;
  `flatten_all` runs *on the worker*, so a dead worker process (crash/OOM/reboot) leaves live
  positions with no flatten path — `worker_status`-stale only alerts a human with no working
  button. **Fix:** add a **deadman process** (separate supervisor unit, independent of the worker
  event loop) that polls `worker_status` liveness + a new `commands.emergency_flatten` signal and
  calls broker REST cancel-all/flatten + reduce-only. Configure **exchange-side cancel-on-disconnect
  / WS deadman timers** (Binance, Delta) and persist **reduce-only stops** per open position so the
  broker enforces a floor even if worker *and* deadman are gone. Document an explicit **RTO** +
  manual-broker runbook as last fallback.
- **R2 — SEV-1 — Single-VPS failover / RTO.** One static-IP VPS holds engine + ClickHouse warm
  state + latching kill-switch; SEBI's static-IP pin discourages naive standby. **Fix (Phase 4):**
  register a **floating/elastic IP** as the SEBI-registered IP (confirm acceptable with broker/SEBI
  up front) so it reattaches to a standby without an unregistered-IP breach; keep kill-switch +
  position state in a restart-safe store the standby `restore()`s from (Vega has `restore()`);
  write RTO/RPO + runbook. The R1 deadman must live on infra independent of the main worker.
- **R3 — SEV-1 — Free-form code-gen isolation (D2).** "No-I/O sandbox" is not hardening — Python is
  not sandboxable in-process (`().__class__.__bases__[0].__subclasses__()` escapes restricted
  globals to `os`/files/subprocess); even no-I/O leaves CPU/mem exhaustion, mutation of `alpha-core`
  internals or the **trial ledger**, and **holdout** reads if it shares the backtester process.
  **Fix (Phase 1c gate):** true OS-level isolation — each free-form strategy in a **separate process
  inside a locked-down container** (gVisor or microVM/Firecracker), **seccomp**, **no network
  namespace**, read-only rootfs, tmpfs scratch, hard **cgroups** CPU/mem/wall limits. Engine runs
  the strategy as a **pure function `(bars, params) -> signals`** over a serialization/IPC boundary,
  passed **only the in-sample slice**, unable to address the holdout or the ledger. Phase-1a control
  test must prove generated code cannot reach holdout/ledger/network.
- **R4 — SEV-2 — DSR trial-ledger scope (keyed, not singleton).** A single global trial counter
  mixes BTC-perp / NIFTY / USDINR searches → **over-penalizes** a novel strategy in one market by
  unrelated trials in another, **under-penalizes** a tight family dominated by other families' count,
  and silently tightens over the project's life. **Fix:** make the planned `research_ledger`
  singleton a **keyed table** partitioned by `(market/instrument-class, strategy-family,
  data-window)`; DSR consumes the count for **its search cell**, not the global sum (keep a global
  count for audit only). Define the cell taxonomy in Phase 1a; make increments **atomic** (concurrent
  research-box pool).
- **R5 — SEV-2 — Holdout decay across strategies.** "One-shot **per strategy**" is still a fixed
  window queried across hundreds of strategies; deploying only holdout-passers selects on the
  holdout at the **population** level → it becomes training data by selection. **Fix:** treat the
  holdout as a **consumable budget** — **roll the window forward** as wall-clock advances (new
  strategies face genuinely unseen future data) and lean on the **paper-forward window as the true
  OOS**; record `holdout_window_version` on `backtests` + a holdout-level multiple-testing count in
  the keyed ledger; optionally hard-cap uses then force a new window. A holdout failure is
  **terminal-reject, never revise**.
- **R6 — SEV-2 — "Strategist never sees holdout" must be enforced by data access, not policy.**
  Leak vectors: `backtests.holdout_metrics` surfaced in the cockpit / readable by table-read agents
  + RAG; the quant-analyst→revise→strategist relay is an optimization loop over the holdout; one
  Parquet/DuckDB store with no in-sample-vs-holdout ACL. **Fix:** (1) **physically separate** the
  holdout partition into a store the strategist's tools + RAG **cannot read** (separate DuckDB file
  / distinct RLS scope); backtester process is its only reader. (2) Hard rule: holdout results
  **never re-enter Workflow A** for any strategy (v2 already runs it in Workflow B — keep it there).
  (3) **Redact** `holdout_metrics` from agent-readable views + RAG ingestion. (4) Phase-1a control
  test proving an agent cannot retrieve holdout data through its tools.
- **R7 — SEV-2 — Overnight CPCV compute budget + parallelism.** CPCV × folds × 2× stress × dozens
  of candidates on a Mac-local box with a serial Workflow-A `LOOP` likely won't finish overnight →
  queue backs up *or* someone quietly cuts fold counts (degrading the crown jewel under compute
  pressure). **Fix:** cap candidates/night + CV-path count to a **measured wall-clock target**;
  **parallelize** across candidates/folds (`request_backtest` already decouples submit from run →
  fan out to a process pool); add a **cheap pre-screen** (coarse walk-forward) culling losers before
  full CPCV; when the night's budget is spent, **carry the queue forward — never truncate folds.**
- **R8 — SEV-2 — Research-box liveness/heartbeat.** Worker liveness is first-class but the research
  box has none; a sleeping/offline Mac (the expected nightly state) blocks Workflow A indefinitely.
  **Fix:** mirror the worker — a **`research_status`** heartbeat table (last_seen, queue depth,
  current job, build version) + a stale-alert schedule; give `request_backtest` results a
  **timeout/lease** so no result within T fails the step loudly (alert + re-queue/skip) instead of
  hanging. Op-note: `caffeinate` the Mac during the nightly window.
- **R9 — SEV-3 — "Kite replay" is not forward-paper for Indian equities.** Replay feeds the same
  historical data to both sides → paper-vs-backtest divergence ≈ 0 **by construction** → reads as
  "validated" while testing a simulation against itself, for the most regulated/costliest market.
  **Fix:** for Kite, forward-paper = **live ticker WS feed driving the strategy in real time with
  simulated fills** (Kite WS needs no order placement), or Zerodha sandbox/paper order API if
  available; the evaluator's Kite leg measures **backtest fills vs live-feed-driven simulated fills**
  (surfaces real slippage/timing drift). Replay is a **regression/reproducibility check, not forward
  validation**; Phase-4 staged equity capital requires a **live-feed paper window**, not replay.
- **R10 — SEV-3 — Command-pickup latency.** All intent + governance rides the polled `commands`
  table at an unspecified interval; a pod-issued `flatten_all`/`clear_halt` at a 5–10s poll is a
  multi-second exposure window stacked on R1. **Fix:** define the poll interval (~1s is cheap and is
  not order placement, so within SEBI OPS); better, **push via Lemma `watchChanges`/realtime** on
  `commands` with poll as fallback. The worker-local autonomous kill-switch stays the fast path; the
  pod command is the human override (seconds, not tens of seconds).
- **R11 — SEV-3 — P&L/exposure must be DERIVED, not incrementally mutated.** Unique keys dedupe
  *inserts*, but at-least-once triggers double-fire; any "read current P&L, add delta, write"
  double-counts → can spuriously trip a loss kill-switch / mask a real loss / corrupt the
  capital-staging tracking-error gate. **Fix:** state the invariant — **all P&L/exposure is a pure
  fold over the deduped immutable `fills`/`orders` rows**, never mutated from trigger payloads;
  triggers only insert immutable facts.
- **R12 — SEV-3 — Daily Kite token-relay failure handling.** Live equity trading depends on the
  ~08:45 IST Mac-2FA relay with only a happy path; Mac asleep / 2FA missed / write fails / mid-session
  expiry → worker can't trade or reconcile, maybe while holding overnight positions. **Fix:** alert
  on reauth failure **before** market open with a retry window; worker treats **no fresh token as
  fail-closed** (don't arm, alert loudly) and relies on R1 deadman/exchange-side protections if
  holding positions (reconcile may be impaired); make `refresh_broker_token` idempotent and
  **on-demand triggerable from Telegram**, not only the schedule.
- **R13 — SEV-4 — Funding accrual into the risk gate, not just P&L.** v2 adds funding to
  backtest/live P&L, but the ported per-order risk gate + loss-based kill thresholds aren't stated to
  include accrued funding → a perp bleeding funding looks flat on mark-to-market while losing money
  each interval. **Fix:** route accrued funding into the **same realized/unrealized P&L the risk
  gate and kill thresholds consume**, not a separate reporting column.
- **R14 — SEV-4 — Calibrate the filter on a POPULATION, not 3 controls.** "overfit→reject,
  random→reject, decent→pass" on one each certifies nothing about false-promote/false-reject *rates*;
  the dangerous candidates are subtle near-overfits — exactly what the LLM produces. **Fix:** make
  Phase-1a calibration **statistical** — a synthetic population with known ground truth (many
  pure-noise → measures false-promote rate / PBO calibration; many overfit-by-construction;
  plant-a-known-signal → measures false-reject); set the **Phase-1b gate on measured error rates**,
  not 3/3. This same noise population validates DSR/PBO thresholds before they guard real money.

### v3 — where these slot into the roadmap
- **Phase 0:** keyed `research_ledger` table (R4); `research_status` heartbeat table (R8);
  `commands.emergency_flatten` signal (R1); holdout-partition store separation (R6); decide
  elastic-IP-as-SEBI-IP with broker (R2).
- **Phase 1a:** population calibration (R14); DSR search-cell taxonomy + embargo + thresholds (R4);
  holdout roll-forward + window-version (R5); CPCV budget + pre-screen + parallel pool +
  carry-forward (R7); data-access control test for holdout/ledger (R6).
- **Phase 1b:** `request_backtest` timeout/lease (R8); `caffeinate` op-note.
- **Phase 1c:** OS-level isolation gate (R3, D2).
- **Phase 3:** Kite live-feed paper + reframed evaluator (R9); derived-P&L invariant (R11);
  `commands` push/`watchChanges` + ~1s poll fallback (R10); token-relay failure handling + Telegram
  on-demand reauth (R12); funding into risk gate (R13); **deadman process** on the worker (R1).
- **Phase 4:** elastic-IP failover + RTO/RPO + runbook (R2); exchange-side cancel-on-disconnect +
  reduce-only stops (R1); staged equity capital gated on a **live-feed paper window** (R9).

### v3 — verification additions (on top of the bottom-up checks below)
- **R1/R2:** kill the worker mid-position (paper) → deadman flattens via broker REST within RTO;
  pull network → exchange cancel-on-disconnect fires; reattach elastic IP to standby → `restore()`
  recovers kill-switch + positions.
- **R3:** generated strategy attempting network/filesystem/holdout/ledger access is blocked at the
  OS boundary; a `while True` / huge-alloc strategy is cgroup-killed.
- **R4/R5/R6:** DSR penalty is invariant to unrelated trials in other cells; an agent cannot fetch
  holdout data through any tool/RAG path; `holdout_window_version` rolls forward.
- **R8:** stop the research box → Workflow A times out and alerts (does not hang).
- **R9:** Kite live-feed paper produces non-zero, explainable divergence vs backtest fills.
- **R11:** replay a duplicated trigger → P&L unchanged, no spurious kill-switch trip.
- **R14:** measured false-promote/false-reject rates on the synthetic population meet the Phase-1b
  gate thresholds.

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
- **Vega licensing — RESOLVED:** you own Vega (`Proprietary`, authored by you; no copyleft, no
  vendored third-party source, deps all permissive). Lift code wholesale, not just patterns
  (see `docs/DESIGN_v4.md` E1 / open item #1).
- Lemma money column type (decide in Phase 0 verification).
- CPCV embargo size + DSR significance threshold + control-strategy definitions (decide in Phase 1a).
- **DSR search-cell taxonomy** (R4) + **holdout roll-forward cadence** (R5) + **synthetic-population
  definitions** (R14) — decide in Phase 1a.
- **Elastic-IP-as-SEBI-registered-IP feasibility** with broker/SEBI (R2) — decide Phase 0/4.
- **Per-night CPCV compute target** on the actual research box (R7) — measure in Phase 1a.
- Capital-staging promotion tolerance + session count (decide before Phase 4).
- How this file feeds the canonical plan: either merge into
  `recursive-marinating-kurzweil.md` (post-approval, outside plan mode) or point the cloud Ultraplan
  session at this path.
