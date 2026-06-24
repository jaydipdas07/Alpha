# Alpha — Design & Build Plan (v4: research-reconciled)

## Context

Alpha is an AI-driven, multi-asset algorithmic **auto-trading platform** for an India-based
builder. The vision: an **autonomous backtesting/discovery engine** that develops and tests
strategies across frequencies and instruments simultaneously → strategies that survive a rigorous
statistical gate go to **paper trading** → paper-validated strategies are **human-approved and
deployed live** → they earn money. A **web app** tracks and controls everything; risk/capital
metrics are user-configurable. Markets: crypto derivatives (**Delta**, live; **Binance**,
data/testnet) and Indian equities/F&O/currency derivatives (**Kite + Dhan + Upstox**).

A strong prior plan exists at `BUILD_PLAN.md` (v3, adversarially hardened) plus
`docs/PHASE0_HANDOFF.md`. This v4 doc was produced by researching the domain **independently
first** (trading-bot frameworks, Indian SEBI 2025-26 retail-algo regulation, crypto venues + tax,
backtest rigor), then reconciling with `BUILD_PLAN.md`. The research **validates** the v3 backbone
and changes four things, all confirmed with the user. This file is the **authoritative amendment**;
where it differs from `BUILD_PLAN.md`, this wins. Where it is silent, `BUILD_PLAN.md` (and its
R1–R14 hardening) stands.

> **Session boundary:** the cloud session that authored this doc canNOT reach the Vega repo, `.env`,
> or the AWS box (per PHASE0_HANDOFF). Plain-Python deliverables (`alpha-core`, `worker`, rigor) can
> be built there and pushed; Lemma/Vega/AWS-coupled steps execute on the Mac `lemma` CLI session.
> **The Vega deep-review is still pending — see the HANDOFF section below.**

## Decisions locked (by the user)

| # | Decision | Choice | Why |
|---|---|---|---|
| **E1** | Execution + backtest engine | **NautilusTrader core + Vega patterns**, confirmed by a time-boxed Phase-0 spike (hand-roll fallback documented) | Nautilus gives a tested OMS / order-FSM / fill engine / **backtest↔live parity** for free — the highest-risk thing to get wrong and *not* the differentiator. Hand-rolling ~13k LOC of tested infra solo is the top schedule risk. |
| **E2** | Build sequencing | **Brain + rigor first** (keep `BUILD_PLAN.md` phase order) | The auto-discovery engine is the user's core vision; build and *calibrate* the rigor gate, then the AI discovery, then paper, then live. |
| **E3** | Indian brokers | **All three: Kite + Dhan + Upstox** (adapter-per-broker behind a registry) | Dhan/Upstox give 25 OPS + free API + **native paper sandbox** (real forward-paper, fixes v3 R9); Kite is most mature + user already has keys. Multi-broker = redundancy + best-of-each. |
| **E4** | First pipeline + first money | **Delta-first**, then Indian equities/index | Delta perps (5m–1h) exercise the whole live machine with the **fewest compliance long-poles** (no SEBI registration / static-IP / daily Kite 2FA). Equities/index (minutes–daily) is the better *edge* (sane tax, cheap data, latency-forgiving) and comes once SEBI prerequisites clear. |
| **E5** | Frequency posture | **Seconds-to-minutes (crypto) + minutes-to-daily (equities)**; sub-second/strict-1s retired as a near-term goal | Retail Indian APIs cannot do true sub-second (Kite ~1 tick/s, ≤10 OPS, no colocation); India's **1% TDS per crypto sell + 30% flat + no loss-offset** makes high-turnover crypto net-negative. Architecture stays frequency-agnostic and enables finer granularity *only where a venue permits*. |

**Inherited from v3 (unchanged, confirmed correct by research):** pod↔worker split (pod never
places live orders; issues `commands`); one engine in both modes = parity; the rigor gate as the
crown jewel; deadman + exchange-side cancel-on-disconnect + reduce-only stops; **derived (never
mutated) P&L** as a pure fold over immutable fills; Decimal money (native DECIMAL else
string-encoded) + tz-UTC + injected `now`; SEBI ≤10 OPS / static-IP / algo-registration / daily
re-auth; FIU crypto venues; no offshore forex; crypto via derivatives; CA tax sign-off.

## Engine approach (E1) — the seam that makes Nautilus + Vega + AI coexist

Keep a **thin `alpha-core` package** that you own; let Nautilus do the heavy, tested execution work
underneath. The seam is a **portable strategy contract**: a pure function `(bars, params) ->
signals`. The strategist agent (and the future sandboxed code-gen, v3 R3) targets *this* contract;
one adapter runs it inside a Nautilus strategy shell. This survives an engine swap and is exactly
what R3's sandbox needs anyway.

| Layer | Owner |
|---|---|
| OMS, order FSM, fill sim, position/account model, backtest event loop, **backtest↔live parity** | **NautilusTrader** |
| Strategy contract `(bars, params) -> signals` (pure, portable, sandbox-ready) | **alpha-core** (thin, yours) |
| Indicators/primitives | Nautilus indicators + a thin whitelist |
| **Risk gate** (non-bypassable pre-trade caps, price collars, data-freshness, latching kill-switch w/ `restore()`, in-flight reservation) | **alpha-core**, Vega-pattern port — wrap Nautilus; do not trust its lighter risk alone |
| **Cost model** (Indian stack + TDS + **funding accrual into the risk gate**, v3 R13) | **alpha-core**, Vega-pattern port |
| **Reconcile** (broker-truth adopt/halt, drift separation, phantom zeroing, re-arm-on-clean) | **alpha-core**, Vega-pattern port |
| Rigor gate (lookahead/WF/CPCV/PBO/DSR/holdout/keyed trial ledger) | **alpha-core**, clean-sheet (the differentiator) |
| Vectorized pre-screen | **vectorbt / VBT PRO** |
| Venue adapters (Delta/Binance via ccxt; Kite/Dhan/Upstox) | **worker**, isolated processes behind a registry |

**Vega's role = pattern source + correctness oracle**, not a copy target: run the same strategy
through Nautilus and through Vega's logic on identical fills and **assert P&L/position equality** (a
differential test). This is a better use of Vega than porting code, and respects the
port-patterns-not-code constraint in PHASE0_HANDOFF. *(If the Vega deep-review confirms outright code
ownership, the Phase-0 hand-roll fallback strengthens — see HANDOFF open item.)*

## Target architecture (refined v3 backbone)

```
        ┌──────────  VAULT POD (Lemma — Mission Control · AI Brain · Approval Gate · Dashboard) ──────────┐
 you ──▶ │ Tables(book/governance) · Files(/knowledge RAG) · Functions(mkt-data READ, orchestrate)          │
(browser │ Agents: strategist · quant-analyst · risk-officer · desk   Workflows: A discovery · B deploy      │
 /TG)    │ App: cockpit (watchChanges + charts)   Surface: telegram                                          │
         └──▲────────────────────────────────────────────────────────────────────────┬─────────────────────┘
   reads:   │ approved deployments + risk_limits + commands (watchChanges, ~1s fallback)│ SDK writes (batched, DERIVED):
   writes:  │ FORM approvals · emergency_flatten trip                                   ▼ orders/fills/positions/pnl_snapshots
        ┌───┴─ RESEARCH BOX (AWS, NO live keys) ──────┐    ┌── EXECUTION WORKER (VPS, static/elastic IP) ──────┐
        │ Nautilus backtest + alpha-core risk/cost/   │    │ Nautilus live + alpha-core risk/cost/reconcile     │
        │ reconcile · RIGOR GATE (CPCV/PBO/DSR/       │    │ venue adapters (isolated procs): Delta(ccxt) ·      │
        │ keyed trial ledger / roll-forward holdout)  │    │ Binance(ccxt,paper) · Kite · Dhan · Upstox          │
        │ vectorbt PRE-SCREEN · research_status hb     │    │ WS→bars→strategy contract→RISK GATE→OMS→broker      │
        │ HOLDOUT store (separate DuckDB, no agent ACL)│   │ heartbeat→worker_status · kill-switch + restore     │
        │ Parquet/DuckDB (cold)                        │   └──────────┬──────────────────────────────────────────┘
        └──────────────────────────────────────────────┘   ┌─────────┴── DEADMAN (separate systemd unit, independent) ──┐
        the SAME engine runs in both ───────────────────────│ polls worker_status + commands.emergency_flatten →         │
                                                            │ broker REST cancel-all/flatten. Exchange-side COD +         │
                                                            │ reduce-only stops survive worker AND deadman death.         │
                                                            └─────────────────────────────────────────────────────────────┘
```

**Storage (simplified for a solo operator):** Parquet + **DuckDB everywhere first** (cold research
*and* worker warm). Defer **ClickHouse to Phase 5**, introduced only if tick-volume/query-latency
demands it — don't run two DB engines solo before forced to. Holdout lives in a **separate DuckDB
file with no agent/RAG read ACL** (v3 R6). Money in Lemma Tables as native DECIMAL else
string-encoded Decimal (B5).

**Lemma portability invariant (new, explicit):** Lemma is **only** the dashboard, the human
approval gate, the agent host, and the orchestration/Telegram surface. It is **never on the money
path** and **never holds logic that can't be re-hosted in ~a week**. All money-critical code
(engine, risk, cost, reconcile, rigor, discovery) lives in plain-Python packages on boxes you
control. The contract between Lemma and the rest is the narrow SDK write boundary + the `commands`
table. This caps blast radius if Lemma gets costly or unavailable. (Public identity of "Lemma" is
uncertain — closest match Thread AI's workflow platform — so this invariant is a deliberate hedge.)

## Frequency & instrument stance (E4/E5)

- **Pipeline proving ground:** **Delta crypto perps, 5m–1h.** Delta testnet + Binance paper exercise
  the entire live machine (WS → bars → strategy → risk → OMS → fills → reconcile → kill-switch →
  deadman) with no SEBI/static-IP/Kite-2FA dependency.
- **Profit edge target:** **liquid Indian equities + index** (NIFTY/BANKNIFTY constituents, index
  futures, **NIFTY/BANKNIFTY weekly options** — deepest algo liquidity), **minutes-to-daily**.
- **Avoid:** sub-second anything; high-turnover crypto (TDS-dead); offshore/spot forex (FEMA-illegal —
  only NSE INR currency derivatives are legal).
- **Extensibility:** strategy contract is frequency-parameterized at the bar-construction layer;
  venues are isolated-process adapters behind a registry; the rigor gate is **keyed by
  (market, strategy-family, data-window)** (v3 R4) so a new venue/frequency adds a *search cell*,
  not new code.

## The rigor gate — built and **calibrated before** the AI generates (crown jewel)

Keep all of v3's gate: look-ahead audit + walk-forward + **CPCV+embargo+PBO**, **Deflated Sharpe**
fed by a **keyed, persistent trial ledger** (R4), **roll-forward locked holdout** in the no-ACL
store (R5/R6), full reproducibility (strategy/dataset/engine/cost versions + RNG seed). Add a
**vectorbt pre-screen** upstream of the expensive CPCV (cheap coarse walk-forward culls losers),
plus CPCV compute budget + parallel pool + carry-forward (R7). **Statistical population calibration**
(R14): synthetic noise/overfit/planted-signal populations that measure *false-promote / false-reject
rates* — the Phase-1b gate is set on measured error rates, not 3 control strategies. **Do not trust
the AI strategist until the filter is proven on the population.**

## Phased roadmap (brain+rigor-first per E2; Delta-first money per E4)

- **Phase 0 — Foundation + Engine Spike (decision gate).** Scaffold `alpha-core/` (thin contract +
  risk/cost/reconcile stubs), `worker/`, `pod/` (pod.json + tables + seed), `docs/`. Verify Lemma
  money-column type (B5) + `lemma`→Vault. **Time-boxed Nautilus spike (1–2 wk):** trivial strategy
  through Nautilus backtest *and* Delta-testnet live-paper via ccxt; portable `(bars,params)->signals`
  contract through a Nautilus shell; Vega-pattern Indian cost model + funding wired into Nautilus's
  fee hook; assert backtest≡paper parity on a replay; stand up Vega-as-oracle differential test
  harness. Add `research_status` heartbeat + `request_backtest` lease/timeout (R8) now. AWS prep
  (preserve `.env`, wipe Vega, set up research host). **Gate:** Nautilus runs the contract on Delta
  testnet with the Indian cost model attached at acceptable parity → adopt; else fall back to
  hand-roll with eyes open.

- **Phase 1a — Trusted single backtester (the rigor crown jewel).** `alpha-core` + Nautilus on the
  research box: free-data ingest (Binance WS/historical, Kite ₹500 historical, NSE bhavcopy,
  Dukascopy) → Parquet/DuckDB → full-rigor backtester (look-ahead, CPCV+PBO, DSR + keyed global
  trial ledger, roll-forward holdout, cost model incl. funding) + vectorbt pre-screen. **Statistical
  population calibration** (R14). **Gate:** measured false-promote/false-reject rates meet
  thresholds; an agent provably cannot read holdout/ledger via any tool/RAG path (R6); DSR penalty
  invariant to unrelated cells; a backtest is bit-for-bit reproducible from its recorded versions/seed.

- **Phase 1b — AI discovery (constrained track).** `strategist` (parameterize vetted templates +
  whitelisted primitives) + `quant-analyst`; Workflow A `discovery-cycle`; `nightly-discovery`
  schedule; `/knowledge` RAG. Strategist proposes from economic rationale + in-sample only; **never
  sees the holdout**; emits originality check + trial-ledger increment. **Gate:** an AI-proposed
  strategy passes the full gate end-to-end on the research box.

- **Phase 2 — Cockpit + copilot + surface.** Vite + lemma-sdk dashboard (`watchChanges` + charts):
  Overview (+ system-health strip: heartbeats, last reconcile, data freshness, open risk_events),
  Strategies, Discovery, Backtests (Sharpe/DSR/PBO/maxDD/cost-sensitivity **incl. rejected
  candidates**), Approvals (FORM inbox), Exchanges & Instruments, Risk (arm kill-switch / flatten /
  clear-halt), Config. `desk` copilot + Telegram. **Gate:** walk one scenario; message `desk` on TG.

- **Phase 3 — Paper + approval gate + worker (paper mode).** Worker on AWS box in PAPER: **Delta
  testnet + Binance paper via ccxt** (and Dhan/Upstox native sandbox for equities forward-paper —
  not Kite replay, R9). `alpha-core` risk gate + latching kill-switch + restore + reconcile;
  **deadman process** (R1); `worker_status`/`research_status` heartbeats; derived-P&L fold (R11);
  `commands` via `watchChanges` + ~1s poll fallback (R10); `start_paper_run`/`evaluate_paper_run` +
  evaluator + Workflow B `deployment-approval` (risk-officer → one-shot holdout gate → human FORM →
  issue start command). **Gate:** kill-the-worker-mid-position → deadman flattens within RTO; paper
  P&L matches backtest within tolerance; flatten from Telegram; full pipeline incl. approval works.

- **Phase 4 — First real money (Delta live, staged).** Worker → LIVE on **Delta** at tiny size;
  live secrets on worker only; exchange-side cancel-on-disconnect + reduce-only stops (R1); staged
  capital 1%→5%→25%→full gated on live-vs-paper tracking error + zero risk_events; CA sign-off on
  crypto-derivative income. **In parallel, start the SEBI equity long-poles:** algo registration +
  static/elastic-IP feasibility with broker (R2); daily Kite token relay (fail-closed + on-demand
  Telegram reauth, R12); Kite **live-feed** paper (R9). **Gate:** Delta live runs at small size with
  clean reconcile across N sessions; equity prerequisites confirmed.

- **Phase 5 — Indian equities/index live + breadth.** Kite/Dhan/Upstox live (equities/index +
  currency derivatives), minutes-to-daily; multi-venue/multi-instrument concurrency; elastic-IP
  failover + RTO/RPO runbook (R2); ClickHouse warm store **only if** volume now demands it. **Gate:**
  equity live-vs-paper match on a live-feed paper window; staged capital.

- **Phase 6 — Free-form code-gen + scale (additive, last).** Sandboxed free-form strategist (R3/D2)
  hard-gated on true OS-level isolation (gVisor/Firecracker + seccomp + cgroups + no-net +
  pure-function IPC). Portfolio optimization; RL/agentic discovery; reconsider engine only if
  scale/latency demands change.

## Key risks & mitigations (deltas over v3; v3 R1–R14 otherwise stand)

| Risk | Mitigation |
|---|---|
| Engine rebuild eats the schedule | Adopt Nautilus core; port only Vega risk/cost/reconcile; **Phase-0 spike gate** decides on evidence, hand-roll fallback. |
| Overfitting under AI-rate generation | Rigor gate built + **calibrated before** AI generates (Phase 1a→1b); keyed trial ledger; roll-forward holdout; population error-rate gate; vectorbt pre-screen. |
| Holdout leakage | Physically separate no-ACL DuckDB store; holdout never re-enters Workflow A; redact from RAG/agent views; access-control test in the Phase-1a gate. |
| Dead-worker, stranded live positions | Deadman process independent of worker loop, in **Phase 3** with first paper positions; exchange-side COD + reduce-only stops; RTO + manual runbook. |
| SEBI / tax non-compliance | Delta-first defers SEBI long-poles; ≤10 OPS + static/elastic-IP + algo registration + daily re-auth before equity live; FIU venues; no offshore forex; CA sign-off; frequency design respects OPS + TDS economics. |
| Lemma lock-in / cost / disappearance | **Portability invariant**: Lemma = dashboard + approval + agent host only; all logic in plain-Python; narrow SDK + `commands` contract; re-hostable in ~a week. |
| Running 2 DB engines solo | DuckDB/Parquet everywhere first; ClickHouse only if Phase-5 volume forces it. |
| P&L double-count (at-least-once triggers) | Derived fold over immutable fills, never mutate (R11); replay-duplicate test. |

## Vega deep-review — HANDOFF (pending; do in a Vega-scoped session)

The authoring cloud session was hard-scoped to `jaydipdas07/Alpha` and **could not reach Vega** (not
on disk; not on GitHub under the account; the `add_repo` tool was not connected). The thorough Vega
read-through must run in a session that has Vega in scope (the user's "Vega UltraPlan" session, or a
new `/ultraplan` session scoped to **both Alpha and Vega**). The goal: **verify the v3 claims about
Vega against the actual code, confirm the E1 seam is feasible, and fold concrete findings back into
this doc + `BUILD_PLAN.md`.**

**Read these modules (the v3 porting basis) and verify the specific claims:**
- `core/interfaces.py` — `Strategy` / `BrokerAdapter` ABCs; idempotent `place_order` dedup-by-query.
  **Verify:** the strategy contract's true shape and whether it can be expressed as / wrapped to the
  portable `(bars, params) -> signals` seam that runs inside a Nautilus strategy shell (E1).
- `core/order_fsm.py` — claimed **48-cell** FSM, fill dedup, over-fill detection, caller-supplied
  `now`. **Verify** completeness and decide what Nautilus owns vs what stays in `alpha-core`.
- `strategy/engine.py` — `now`-injection parity discipline; how strategies are driven (bar loop).
- `risk/manager.py` + `risk/limits.py` — claimed **non-bypassable 10-step gate**, **latching
  kill-switch with `restore()`**, **in-flight working-exposure reservation**. **Port-heavy** — verify
  it is stronger than Nautilus's built-in risk (the reason to wrap, not replace).
- `execution/reconcile.py` — broker-truth **adopt-vs-halt**, explained/unexplained drift separation,
  **phantom-position zeroing**, re-arm-on-clean (`rearm`). **Port-heavy** — verify and compare to
  Nautilus reconciliation.
- `execution/costs.py` — **Indian cost stack + crypto TDS**, price-taker, 2× stress. **Port-heavy** —
  verify; confirm it must be extended with **perp funding accrual into the risk gate** (R13).
- `execution/oms.py` + `execution/state.py` — OMS idempotency + audit trail.
- `backtest/rigor.py` — look-ahead + walk-forward + 2× stress. **Port then extend** with
  CPCV/PBO/DSR/holdout — verify what's already there so we don't rebuild it.
- `adapters/kite.py` + `adapters/crypto_ccxt.py` — token flow + normalization patterns to lift for
  the Kite/Dhan/Upstox + Delta/Binance worker adapters.
- The **18 ADRs** (design rationale) and a **sampling of tests** — confirm the claimed `>1:1`
  test:src ratio, `fail_under=94`, `mypy --strict`, property/replay/chaos tests.

**Questions the review must answer (feed back into E1 and the porting matrix):**
1. Is Vega's strategy contract Nautilus-shell-compatible, or does the seam need an adapter? How thin?
2. Which subsystems are genuinely stronger in Vega than Nautilus (→ port) vs redundant (→ let
   Nautilus own)? Confirm risk / cost / reconcile as the port-heavy three.
3. Does Vega already have a usable backtester/event loop that reduces the Nautilus dependency, or is
   Nautilus clearly the better core? (Informs whether the Phase-0 spike leans adopt vs hand-roll.)
4. **Licensing/ownership** — is Vega the user's to *lift code* from (not just patterns)? If yes, the
   Phase-0 hand-roll fallback strengthens (copy tested code instead of re-deriving).
5. Rough **effort estimate** to port risk+cost+reconcile into the Nautilus-wrapped `alpha-core`.
6. Anything in Vega **weaker/different than v3 assumes**, or any gem v3 under-uses (e.g. data plane,
   property-test harness reusable as the differential oracle).

**Deliverable of that session:** update this doc's Engine-approach table + Vega-lift matrix with
verified facts, adjust the Phase-0 spike gate criteria, and note the licensing answer (open item #1).

## Critical files
- `BUILD_PLAN.md` — canonical v3 spec these changes amend (engine decision E1 supersedes its Q1).
- `docs/PHASE0_HANDOFF.md` — build-env decisions B1–B5; Phase-0 spike + portability invariant slot here.
- `README.md` — update framing to Nautilus-core + multi-broker + the v4 roadmap.
- `alpha-core/` (new) — thin portable strategy contract + Vega-pattern risk/cost/reconcile + rigor gate; the seam between Nautilus and the AI brain.
- `worker/` (new) — sole executor + deadman + isolated venue adapters (Delta/Binance/Kite/Dhan/Upstox).
- `pod/` (new) — pod.json + tables (incl. keyed `research_ledger`, `worker_status`, `research_status`, `commands.emergency_flatten`) + seed.

## Open items to confirm (do not block plan approval)
- **Vega licensing/ownership** — if you own Vega code outright, the Phase-0 gate may *lift code*, not
  just patterns (strengthens the hand-roll fallback).
- **Elastic-IP-as-SEBI-registered-IP** feasibility — confirm with broker before Phase 4/5 equity live.
- **Lemma cost** at nightly-discovery agent/workflow/table volume — confirm the portability invariant is acceptable.
- **Tier-2 rigor params** (decide entering Phase 1a): DSR search-cell taxonomy, CPCV embargo size,
  holdout roll-forward cadence/size, synthetic-population defs + Phase-1b error-rate thresholds.
- **Capital-staging tolerances** (before any live phase): tracking-error threshold + session count.
- **Per-night CPCV compute target** — measure on the actual research box in Phase 1a.

## Verification (per-layer, bottom-up)
1. **Engine spike (Phase 0):** portable contract runs in Nautilus backtest *and* Delta-testnet
   live-paper; backtest≡paper parity on a replay; Vega-oracle differential P&L equality holds.
2. **alpha-core / rigor:** population calibration meets false-promote/false-reject thresholds;
   backtest bit-for-bit reproducible from recorded versions/seed; agent cannot reach holdout/ledger.
3. **Pod primitives:** `lemma records create` + `query run` (columns/FKs/RLS + money type); upload a
   `/knowledge` doc → COMPLETED + `files search`; run `fetch_historical`/`request_backtest`/`clear_halt`.
4. **Agents/workflows:** `agents chat strategist "propose a mean-reversion strategy for BTC-perp 5-min"`;
   run `discovery-cycle`; trip Workflow B; submit the approval FORM.
5. **Safety (Phase 3):** kill worker mid-position → deadman flattens via broker REST within RTO; pull
   network → exchange cancel-on-disconnect fires; replay a duplicated fill trigger → P&L unchanged.
6. **Whole loop:** discovery → strategist proposes → rigor (CPCV/DSR/look-ahead-clean, holdout
   untouched) → quant-analyst promotes → paper run → divergence in tolerance → risk-officer limits →
   one-shot holdout gate → **you approve via FORM** → command → worker (paper→Delta live, tiny size)
   → orders/fills/positions/heartbeat flow back → **cockpit live P&L** → arm kill-switch flattens &
   latches; `clear_halt` re-arms only on clean reconcile.
