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

> **Session boundary:** this cloud session canNOT reach the Vega repo, `.env`, or the AWS box
> (per PHASE0_HANDOFF). Plain-Python deliverables (`alpha-core`, `worker`, rigor) can be built here
> and pushed; Lemma/Vega/AWS-coupled steps execute on the Mac `lemma` CLI session.

> **Update (post-Vega-review, 2026-06-24/25):** the deep Vega read-through that the prior session
> could not do is now **complete** (read-only). Every headline v3 claim about Vega was verified
> against the actual code — see **"Vega deep-review — COMPLETED"** below. Two outcomes fold back
> into this doc: (1) **Vega is the operator's own proprietary, wholesale-liftable code** (open item
> #1 RESOLVED), so the engine fallback is "lift Vega," not "hand-roll from scratch"; (2) **E1 is
> reframed to a co-equal A/B bake-off** (Nautilus-shell vs Vega-lift) — the "Vega = fallback"
> framing is dropped because the evidence makes lifting Vega the cheaper, lower-risk path to
> measure first. All other v4 decisions and the v3 backbone stand unchanged.

## Decisions locked (this session, by the user)

| # | Decision | Choice | Why |
|---|---|---|---|
| **E1** | Execution + backtest engine | **Decided by a time-boxed Phase-0 *co-equal A/B bake-off*** — **Track A** NautilusTrader-shell + Vega-pattern risk/cost/reconcile overlays; **Track B** lift Vega's own tested engine wholesale + thin event integration — chosen on backtest≡paper parity + integration friction + time-to-market. **No preordained primary/fallback; current evidence favors Track B (≈6–8 wk, ~80% lower risk, code you own).** | A tested OMS / order-FSM / fill engine / **backtest↔live parity** is the highest-risk thing to get wrong and *not* the differentiator. Nautilus offers one for free — **but the Vega review found Vega already *is* ~13k LOC of tested OMS/FSM/risk/cost/reconcile + a parity-proven backtester that you own and can lift** (Proprietary, authored by you). So the real choice is *which* tested engine to stand on; the spike **measures** both rather than presuming. "Hand-roll" was always more accurately "lift Vega." |
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

Keep a **thin `alpha-core` package** that you own; the heavy, tested execution work underneath comes
from **whichever track the Phase-0 bake-off picks** (Nautilus, or Vega's own lifted engine — both
verified to provide a complete OMS/FSM/fill/position model *and* backtest↔live parity). The seam is
path-independent: a **portable strategy contract**, a pure function `(bars, params) -> signals`. The
strategist agent (and the future sandboxed code-gen, v3 R3) targets *this* contract; one adapter
runs it inside the chosen engine's strategy shell. This survives an engine swap and is exactly what
R3's sandbox needs anyway. The review confirmed Vega's `Strategy` ABC (`on_bar`/`on_tick`→`Signal`,
no broker SDK, no loop/clock ownership) **is** this contract, and `StrategyEngine.process_bar` is a
push API a Nautilus `on_bar` callback can drive — so the seam is a thin shim either way.

| Layer | Owner |
|---|---|
| OMS, order FSM, fill sim, position/account model, backtest event loop, **backtest↔live parity** | **Spike-decided: NautilusTrader (Track A) or lifted Vega (Track B)** — review confirmed Vega provides *all* of it, incl. parity via `FakeClock` |
| Strategy contract `(bars, params) -> signals` (pure, portable, sandbox-ready) | **alpha-core** (thin, yours) — = Vega's `Strategy` ABC |
| Indicators/primitives | Nautilus indicators + a thin whitelist (Track A) / Vega + whitelist (Track B) |
| **Risk gate** (non-bypassable pre-trade caps, price collars, data-freshness, latching kill-switch w/ `restore()`, in-flight reservation) | **alpha-core** — **lift Vega verbatim** (you own it); wrap the engine's lighter risk, do not trust it alone |
| **Cost model** (Indian stack + TDS + **funding accrual into the risk gate**, v3 R13) | **alpha-core** — lift Vega `costs.py` + extend with funding |
| **Reconcile** (broker-truth adopt/halt, drift separation, phantom zeroing, re-arm-on-clean) | **alpha-core** — lift Vega `reconcile.py` verbatim |
| Rigor gate (lookahead/WF/CPCV/PBO/DSR/holdout/keyed trial ledger) | **alpha-core**, lift Vega's lookahead/WF/stress + **clean-sheet extend** (the differentiator) |
| Vectorized pre-screen | **vectorbt / VBT PRO** |
| Venue adapters (Delta/Binance via ccxt; Kite/Dhan/Upstox) | **worker**, isolated processes behind a registry |

**Vega is a valid copy target — you own the IP** (Proprietary, authored by you; no copyleft, no
vendored third-party source), so code may be lifted **verbatim**, not merely patterned. Independently,
the **differential-oracle** technique stays valuable on *either* track: run the same strategy through
the chosen engine and through Vega's logic on identical fills and **assert P&L/position equality**,
reusing Vega's property/replay test suite as the parity oracle. This supersedes the earlier
"port-patterns-not-code" caution in PHASE0_HANDOFF, which was self-imposed, not a legal limit.

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
the AI strategist until the filter is proven on the population.** (The review confirmed Vega's
`backtest/rigor.py` already supplies the look-ahead audit + walk-forward + 2× stress — and a bonus
cross-instrument portfolio-rigor suite — so this is a **lift-then-extend**, not a clean build.)

## Phased roadmap (brain+rigor-first per E2; Delta-first money per E4)

- **Phase 0 — Foundation + Engine Bake-off (decision gate).** Scaffold `alpha-core/` (thin contract +
  risk/cost/reconcile stubs), `worker/`, `pod/` (pod.json + tables + seed), `docs/`. Verify Lemma
  money-column type (B5) + `lemma`→Vault. **Time-boxed *two-track* bake-off (1–2 wk)** on
  **Delta-testnet via ccxt** (E4-aligned, no SEBI long-poles): **Track A** — run the portable
  `(bars,params)->signals` contract inside a Nautilus strategy shell; Nautilus backtest *and*
  Delta-testnet live-paper; Vega-pattern Indian cost model (+funding) wired into Nautilus's fee hook.
  **Track B** — lift Vega's own engine (OMS/FSM/risk/cost/reconcile/backtester) verbatim and run the
  same contract + Delta-testnet live-paper through it. Both tracks: **assert backtest≡paper parity on
  a replay**; measure integration friction + time-to-code. Stand up the **Vega differential-oracle
  harness** (its property/replay suite) to cross-check P&L/positions on the winning track. Add
  `research_status` heartbeat + `request_backtest` lease/timeout (R8) now. AWS prep (preserve `.env`,
  wipe Vega, set up research host). **Gate:** pick the track with **passing parity + lower
  cost-to-production + lower integration risk** — no preordained primary/fallback; **current evidence
  favors Track B (lift-Vega: ≈6–8 wk vs ≈14–16 for the Nautilus-shell, ~80% lower risk, code you
  own).**

- **Phase 1a — Trusted single backtester (the rigor crown jewel).** `alpha-core` + the chosen engine
  on the research box: free-data ingest (Binance WS/historical, Kite ₹500 historical, NSE bhavcopy,
  Dukascopy) → Parquet/DuckDB → full-rigor backtester (look-ahead, CPCV+PBO, DSR + keyed global
  trial ledger, roll-forward holdout, cost model incl. funding) + vectorbt pre-screen. **Statistical
  population calibration** (R14). **Gate:** measured false-promote/false-reject rates meet
  thresholds; an agent provably cannot read holdout/ledger via any tool/RAG path (R6); DSR penalty
  invariant to unrelated cells; a backtest is bit-for-bit reproducible from its recorded versions/seed.

- **Phase 1b — AI discovery (constrained track).** `strategist` (parameterize vetted templates +
  whitelisted primitives — seed from Vega's ~7 example strategies) + `quant-analyst`; Workflow A
  `discovery-cycle`; `nightly-discovery` schedule; `/knowledge` RAG. Strategist proposes from
  economic rationale + in-sample only; **never sees the holdout**; emits originality check +
  trial-ledger increment. **Gate:** an AI-proposed strategy passes the full gate end-to-end on the
  research box.

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
  currency derivatives), minutes-to-daily; multi-venue/multi-instrument concurrency (clean-sheet —
  Vega is single-adapter/process); elastic-IP failover + RTO/RPO runbook (R2); ClickHouse warm store
  **only if** volume now demands it. **Gate:** equity live-vs-paper match on a live-feed paper
  window; staged capital.

- **Phase 6 — Free-form code-gen + scale (additive, last).** Sandboxed free-form strategist (R3/D2)
  hard-gated on true OS-level isolation (gVisor/Firecracker + seccomp + cgroups + no-net +
  pure-function IPC). Portfolio optimization; RL/agentic discovery; reconsider engine only if
  scale/latency demands change.

## Key risks & mitigations (deltas over v3; v3 R1–R14 otherwise stand)

| Risk | Mitigation |
|---|---|
| Engine work eats the schedule | **Not a from-scratch rebuild** — Vega is your own tested ~13k-LOC engine, liftable wholesale (you own the IP). Co-equal A/B **Phase-0 bake-off** decides Nautilus-shell vs lift-Vega on evidence (favors lift, ≈6–8 wk); the strategy-contract seam survives an engine swap. |
| Dual-OMS divergence (if Nautilus Cache *and* Vega OMS both hold position truth) | Pick **one** OMS owner per the bake-off; on Track A, Vega's reconcile becomes a validation layer over Nautilus; never run two sources of position truth (ADR 0001 one-way). |
| Multi-venue gap (Vega = one `active_adapter`/process) | Multi-venue routing + cross-venue position/risk aggregation is **clean-sheet** for E3 (Kite+Dhan+Upstox+Delta); build per-venue adapter processes behind a registry. |
| Multi-process state (Vega = single-instance SQLite) | Move the (lifted) event-sourced + audit-chain state store to a shared backend (Postgres/distributed) when the worker pool fans out; keep the design. |
| Greeks not enforced; no market calendar / instrument master | Wire Vega's `options/risk.py` Greeks calc **into the pre-trade gate** (net-new); build NSE/BSE calendar + live instrument master before F&O/equity live. |
| Cost-model edge case (possible index-option premium-cost config) | Validate `costs.py` index-option premium handling against broker contract notes during the lift. |
| Overfitting under AI-rate generation | Rigor gate built + **calibrated before** AI generates (Phase 1a→1b); keyed trial ledger; roll-forward holdout; population error-rate gate; vectorbt pre-screen. |
| Holdout leakage | Physically separate no-ACL DuckDB store; holdout never re-enters Workflow A; redact from RAG/agent views; access-control test in the Phase-1a gate. |
| Dead-worker, stranded live positions | Deadman process independent of worker loop, in **Phase 3** with first paper positions; exchange-side COD + reduce-only stops; RTO + manual runbook. |
| SEBI / tax non-compliance | Delta-first defers SEBI long-poles; ≤10 OPS + static/elastic-IP + algo registration + daily re-auth before equity live; FIU venues; no offshore forex; CA sign-off; frequency design respects OPS + TDS economics. |
| Lemma lock-in / cost / disappearance | **Portability invariant**: Lemma = dashboard + approval + agent host only; all logic in plain-Python; narrow SDK + `commands` contract; re-hostable in ~a week. |
| Running 2 DB engines solo | DuckDB/Parquet everywhere first; ClickHouse only if Phase-5 volume forces it. |
| P&L double-count (at-least-once triggers) | Derived fold over immutable fills, never mutate (R11); replay-duplicate test. |

## Vega deep-review — COMPLETED

**Status: done this session (read-only).** Most headline claims verified first-hand by reading the
source; breadth (adapters, ADRs, tests, state store, licensing) cross-checked by a 14-agent
read-only workflow. Package `src/tradebot` (project "vega"); `license = "Proprietary"`, author
**Jaydip Das = the operator**; src 12,981 LOC vs tests 13,506 LOC (>1:1), `fail_under=94`,
`mypy strict`.

### Claim-verification table (v3 claims vs actual code)

| v3 claim | Verdict | Evidence |
|---|---|---|
| 48-cell order FSM, fill dedup, over-fill detect, caller-`now` | **Confirmed, exact** | `core/order_fsm.py` — 8 states × 6 events = 48, every cell named; `seen_fills` dedup; over-fill→`OrderStateConflict`; `now` injected (bar-time in backtest). *Stronger:* each `step` returns a re-validated `Order`; conflicts route to reconcile. |
| Non-bypassable 10-step gate, latching kill-switch + `restore()`, in-flight reservation | **Confirmed, exact** | `risk/manager.py` — 10 numbered steps, halt always first ("no flag disables risk"); `trip`/`rearm` fail-closed; `restore(halted,trigger)`; `WorkingExposure`/`_effective_book` (ADR 0014). `update_pnl(realized,unrealized)` is the kill hook → R13 funding is an additive feed, not a structural change. |
| Reconcile adopt-vs-halt, explained/unexplained drift, phantom zeroing | **Confirmed, exact** | `execution/reconcile.py` — broker-as-truth; `unexplained → trip(RECONCILIATION_MISMATCH)`; phantom `quantity=0` synthesis (M2); `startup_gate` blocks trade until clean. |
| Indian cost stack + crypto TDS, price-taker, 2× stress | **Confirmed** | `execution/costs.py` — itemized brokerage/STT/exchange/GST/SEBI/stamp + `tds`; ask/bid touch ("never mid"); `stress_multiplier`. Classes EQUITY/CRYPTO/INDEX_OPTION. **No perp funding** (Alpha adds). |
| Rigor: look-ahead + walk-forward + 2× stress | **Confirmed, stronger** | `backtest/rigor.py` — interior-bar pivot sweep + walk-forward + stress-gate, **plus a full cross-instrument portfolio-rigor suite** v3 never mentioned. |
| Backtest↔live parity via one shared kernel | **Confirmed** | `backtest/runner.py` — reuses `StrategyEngine`→`OMS`(risk→FSM→`PaperBroker`→`StateStore`)→`CostModel`, `FakeClock` from bar-close. **Parity achieved without Nautilus.** |
| State store, event-sourced + tamper-evident | **Confirmed (gem)** | `execution/state.py` — append-only `audit_log`/`fills`/`pnl_ledger` (DB triggers), SHA-256 hash-chained audit (`prev_hash`→`hash`, `verify_audit_chain`), crash-recovery by replaying fills. |
| >1:1 tests, `fail_under=94`, `mypy --strict`, property/replay/chaos | **Confirmed** | 13,506 vs 12,981 LOC; `pyproject.toml`; hypothesis property tests + replay (`test_paper_loop`) + chaos. |
| "18 ADRs" | **Weaker** | **17 numbered ADRs** (0001–0015, 0017, 0018; **0016 missing**); 0000 is a template → 18 *files*. |

### The 6 HANDOFF review questions — answered

1. **Strategy contract Nautilus-shell-compatible / how thin a seam?** Yes — thin shim. `Strategy` is
   pure synchronous `on_bar`/`on_tick`→`Sequence[Signal]`, no broker SDK, no loop/clock ownership;
   `StrategyEngine.process_bar` is a push API a Nautilus `on_bar` callback can drive. Caveat: the
   `Signal`→`PortfolioConstructor`→risk→OMS→FSM spine is Vega's own (exactly what Nautilus would
   otherwise own), and data models differ (translate `Bar/Tick/Signal/Order` at the boundary).
2. **Stronger-in-Vega (port) vs redundant (Nautilus-owns)?** Port (Vega stronger / India-specific):
   risk gate, reconcile, cost model, order FSM, rigor gates, event-sourced state + audit-chain —
   confirming **risk/cost/reconcile as the port-heavy three**. Let Nautilus own (generic infra):
   event loop, message bus, callback dispatch, adapter framework, position/account cache, bar
   streaming.
3. **Does Vega's backtester reduce the Nautilus dependency?** **Yes, substantially.** It is a true
   event-driven backtester sharing the exact live kernel with `FakeClock` parity — Nautilus's
   headline feature is already met by code you own. (This is the main reason E1 became a bake-off.)
4. **Licensing — lift code, not just patterns?** **Yes, wholesale.** Proprietary, authored by you;
   no copyleft, no vendored third-party source, deps all permissive (MIT/Apache/BSD). **Resolves
   open item #1** — see below.
5. **Effort to port risk+cost+reconcile into a Nautilus-wrapped alpha-core?** Low — the three are
   self-contained, Decimal-pure, well-tested; lift + thin adapter ≈ a few days each. Whole-engine:
   Nautilus-shell ≈ 14–16 wks vs lift-Vega ≈ 6–8 wks (~80% lower risk).
6. **Weaker/different than v3 assumes, or under-used gems?** Weaker: ADR count (17 not 18); cost
   model lacks perp funding; Greeks calc exists (`options/risk.py`) but is **not wired into the
   pre-trade gate**; single `active_adapter` per process (`factory.py`) so multi-venue is net-new;
   single-instance SQLite state; subagents flag a possible index-option premium-cost config issue
   (**validate during lift**). Under-used gems: **~7 tradeable example strategies** (template library
   for the constrained strategist), options/Greeks, F&O roll scheduler, paper adapter, event-sourced
   state + SHA-256 audit chain, vendor-agnostic error-by-class-name taxonomy, sha1-client-order-id +
   dedup-by-query idempotency, and the **property/replay test suite reusable as a differential
   oracle**.

### Verified Vega-lift matrix

| Subsystem | Action | Effort | Vega files |
|---|---|---|---|
| Core models/enums/errors/interfaces (ABCs; Decimal/tz/immutable) | lift-wholesale | trivial | `core/{interfaces,models,enums,errors}.py` |
| Order FSM (48-cell, dedup, over-fill, `now`) | lift-wholesale | trivial | `core/order_fsm.py` (+ tests) |
| Risk gate (10-step, latching kill+`restore`, working-exposure) | lift-wholesale | low | `risk/manager.py`, `risk/limits.py` |
| Reconcile (adopt/halt, drift sep, phantom zero, rearm-on-clean) | lift-wholesale | low | `execution/reconcile.py` |
| Cost model (Indian stack + TDS, price-taker, 2× stress) **+ add funding** | lift + extend | low (+med funding) | `execution/costs.py`, `config/costs.yaml` |
| OMS + positions + state-store (event-sourced, SHA-256 audit, restore) | lift-wholesale | low | `execution/{oms,positions,state}.py` |
| Backtester + rigor (lookahead/WF/stress + portfolio rigor) **+ extend** CPCV/PBO/DSR/holdout/keyed-ledger | lift + extend | low lift / **high extend (the differentiator)** | `backtest/{runner,rigor}.py` |
| Strategy engine + registry + ~7 example templates | lift / port-pattern | low | `strategy/engine.py`, `registry.py`, `strategy/examples/*` |
| Options Greeks (calc exists; **wire into pre-trade gate = net-new**) | port + extend | medium | `options/{risk,greeks,chain}.py` |
| Adapters: Delta/Binance (ccxt) + Kite (token flow) | port-pattern | medium | `adapters/{crypto_ccxt,kite,factory,paper}.py`, `helpers/kite_auth.py` |
| **Dhan + Upstox adapters** | clean-sheet | medium | none (build to `BrokerAdapter` ABC) |
| **Multi-venue + multi-strategy orchestration** (Vega = single-adapter/process) | clean-sheet | high | `portfolio/loop.py` as pattern only |
| **Indian market calendar + live instrument master** | clean-sheet | low–med | none |
| **Perp funding accrual → P&L + risk gate** (R13) | clean-sheet | medium | `costs.py` ext + `risk.update_pnl` feed |
| **AI-rigor layer** (CPCV/PBO, Deflated Sharpe, keyed global trial ledger, roll-forward holdout, population calibration) | clean-sheet | high | n/a — the crown jewel |
| **Lemma pod** (tables/agents/workflows/app), pod↔worker SDK, **deadman** | clean-sheet | high | n/a |
| Live event loop | spike-decided (Nautilus-owns *or* lift `live.py`) | depends | `live.py`, `app.py` |

**Framing:** the engine-seam delta (6–8 vs 14–16 wks) is only the *foundation*. The clean-sheet
Alpha differentiators (AI-rigor, pod, multi-venue orchestration, Dhan/Upstox adapters) are common to
**both** engine tracks and dominate the overall timeline — so the engine choice mainly affects
foundation speed/risk, not the whole project.

## Critical files
- `BUILD_PLAN.md` — canonical v3 spec these changes amend (engine decision E1 supersedes its Q1).
- `docs/PHASE0_HANDOFF.md` — build-env decisions B1–B5; Phase-0 bake-off + portability invariant slot here.
- `README.md` — update framing to engine-by-Phase-0-bake-off + multi-broker + the v4 roadmap.
- `alpha-core/` (new) — thin portable strategy contract + lifted Vega risk/cost/reconcile/state + rigor gate; the seam between the chosen engine and the AI brain.
- `worker/` (new) — sole executor + deadman + isolated venue adapters (Delta/Binance/Kite/Dhan/Upstox).
- `pod/` (new) — pod.json + tables (incl. keyed `research_ledger`, `worker_status`, `research_status`, `commands.emergency_flatten`) + seed.

## Open items to confirm (do not block plan approval)
- **Vega licensing/ownership — RESOLVED.** Vega is `Proprietary`, authored by you; no copyleft, no
  vendored third-party source, all deps permissive (MIT/Apache/BSD). You may **lift Vega code
  wholesale**, not just patterns — this is now **Track B** of the Phase-0 bake-off and the
  evidence-favored path.
- **Elastic-IP-as-SEBI-registered-IP** feasibility — confirm with broker before Phase 4/5 equity live.
- **Lemma cost** at nightly-discovery agent/workflow/table volume — confirm the portability invariant is acceptable.
- **Tier-2 rigor params** (decide entering Phase 1a): DSR search-cell taxonomy, CPCV embargo size,
  holdout roll-forward cadence/size, synthetic-population defs + Phase-1b error-rate thresholds.
- **Capital-staging tolerances** (before any live phase): tracking-error threshold + session count.
- **Per-night CPCV compute target** — measure on the actual research box in Phase 1a.
- **Index-option premium-cost config** in Vega `costs.py` — validate against broker contract notes during the lift.

## Verification (per-layer, bottom-up)
1. **Engine bake-off (Phase 0):** the portable contract runs in **both** tracks (Nautilus shell and
   lifted-Vega) on Delta-testnet; backtest≡paper parity on a replay in the chosen track; Vega-oracle
   differential P&L/position equality holds.
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
