# TASKS.md — Alpha work queue

> **The work queue for the whole build.** `docs/DESIGN_v4.md` (canonical design) and `BUILD_PLAN.md`
> (v3 backbone + R1–R14 hardening) say *how* and *why*; `CLAUDE.md` is the operating contract; **this
> file says what's done and what's next.** The single live worklist is the **⏳ Pending tracker at the
> very end of this file** — everything above it is the planned record. Phases 0–1b are decomposed to
> executable tasks; Phases 2–6 are milestone work-packages, expanded to `B`-tasks on arrival.

**Status:** ✅ done · 🔶 in progress · ⏳ open (pending) · ⛔ out of scope · ➡️ superseded.
**Owners:** `[CC]` Claude Code builds · `[You]` keys / money / regulator / live-gate · `[You]+[CC]` you open the door, CC drives.
**ID scheme:** `B<phase>.<seq>` build task · `M<phase>.<seq>` milestone · `<phase>.GATE` gate · `TEST-<n>` cardinal invariant · `R1–R14` v3 hardening · `E1–E5 / B1–B5 / D1–D2` locked decisions.

## Phase index (status at a glance)

| Phase | What | Owner | Status |
|---|---|---|---|
| **0** | Foundation + **engine bake-off** (Nautilus-shell vs lift-Vega) → engine chosen | [CC]/[You] | ✅ (0.GATE, ADR 0001) |
| **1a** | Rigor crown-jewel + data plane (+ population calibration) | [CC] | ✅ (1a.GATE, ADR 0002) |
| **1b** | AI discovery — constrained track (strategist + quant-analyst + Workflow A) | [CC] | 🔶 next |
| **2** | Cockpit + copilot + Telegram surface | [CC] | ⏳ |
| **3** | Paper + approval gate + worker (paper) + **deadman** | [CC]/[You] | ⏳ |
| **4** | First real money — Delta live (staged) + start SEBI long-poles | [You]+[CC] | ⏳ |
| **5** | Indian equities/index live + multi-venue breadth | [You]+[CC] | ⏳ |
| **6** | Free-form code-gen (sandboxed) + scale | [CC]/[You] | ⏳ |

---

## Phase 0 — Foundation + Engine Bake-off · [CC] builds, [You] keys/AWS/Lemma
Goal: a correct `alpha-core`/`worker`/`pod` skeleton that lints/types/tests green; Lemma + money verified; the two-track engine bake-off run and the engine chosen. Proves **TEST-1**.

| ID | Task | Owner | Done-when |
|---|---|---|---|
| B0.1 | Scaffold `alpha-core/` + `worker/` + `pod/` + `docs/`; `pyproject.toml` (uv, py≥3.12; pydantic/pydantic-settings/structlog + dev ruff/mypy/pytest/hypothesis/pre-commit); ruff + `mypy --strict` + pytest config; CI (uv→ruff→mypy→pytest) | [CC] | ✅ green in CI; ruff + mypy clean; merged (#5) |
| B0.2 | Mirror Vega quality gates into `alpha-core`: `fail_under=94`, `mypy strict`, pre-commit hooks | [CC] | ✅ gates enforced locally + in CI (#7) |
| B0.3 | Lemma Phase-0 verification (B5): inspect money-column types → lock native DECIMAL vs string-Decimal; confirm `lemma` → Vault; verify table/RLS/FK primitives | [You]+[CC] | ✅ B5 = **TEXT string-Decimal** (Lemma has no native DECIMAL; FLOAT lossy + INTEGER is int32 — both proven); `Decimal` round-trips; `lemma`→Vault ok |
| B0.4 | Pod skeleton: `pod.json` + table DDL (deployments, strategies, backtests, discovery_runs, paper_runs, orders, fills, positions, pnl_snapshots, risk_events, **commands**(+`emergency_flatten`), **worker_status**, **research_status**, **research_ledger** keyed by (market,family,window), broker_credentials RLS) + seed | [CC] | ✅ 15 tables imported + seeded; FK JOIN + money round-trip + idempotency + RLS verified (#11) |
| B0.5 | AWS prep: SSH; preserve `.env` keys → Alpha gitignored secrets; wipe the AWS box's Vega **deployment** (keep the Mac Vega repo as the lift source); set up the research host | [You]+[CC] | ✅ research host `3.6.176.133` reachable; alpha-core runs (40 tests, aarch64); Vega stopped (kept, reversible); keys preserved; nothing live (#13) |
| B0.6 | `research_status` heartbeat table + `request_backtest` lease/timeout scaffolding (R8) | [CC] | ✅ lease + heartbeat-staleness logic (`alpha_core/research`); a dead box's work is reclaimable, never hangs; 100% cov (#12) |
| B0.7 | Portable strategy contract in `alpha-core`: `(bars, params) -> signals` (= Vega `Strategy` ABC) + a trivial MA-crossover reference impl | [CC] | ✅ contract + MA-crossover ref impl; 29 tests, 100% cov (#8) |
| B0.8 | **Track A (Nautilus-shell):** install NautilusTrader; wrap the contract in a Nautilus strategy; wire the Vega-pattern Indian cost model (+funding) into Nautilus's fee hook; Nautilus backtest + Delta-testnet live-paper via ccxt | [CC] | ✅ the B0.7 contract runs through a Nautilus backtest (#24, `track-a/`) with Vega's `CostModel` wired into a custom `FeeModel`; same 2 fills as Track B, net P&L matches to 8 decimals. (Live-testnet on Track A not pursued — a TTM data point; Track B is the recommendation, see 0.GATE.) |
| B0.9 | **Track B (lift-Vega):** copy Vega `core`/`execution`/`risk`/`backtest` into `alpha-core` (rename `tradebot`→`alpha_core`); run the same contract + Delta-testnet live-paper + backtest through the lifted engine | [CC] | ✅ **engine lifted** (core+risk+execution+strategy-engine+backtest/portfolio, #14–#18) **+ B0.7 contract proven through it**: deterministic backtest (#19) **and crypto-testnet live-paper** (#21 CcxtAdapter, #22 run — MaCrossover placed+filled a real order via StrategyEngine→OMS→CcxtAdapter on **Binance** testnet; Delta swapped to Binance per [You], no live/test dual accounts). 401 tests, ≥94% cov; `docs/bakeoff_b0_9f.md` |
| B0.10 | Parity + oracle: assert backtest ≡ paper parity on a replay per track (**TEST-1**); stand up the Vega differential-oracle (its property/replay suite) cross-checking P&L/positions; measure integration friction + time-to-code | [CC] | ✅ parity asserted — both tracks net the **same P&L to 8 decimals** on the same contract + cost kernel; the Vega differential oracle (property/replay suites) is in `alpha-core/tests` (Track B); comparison written as ADR 0001 |
| **0.GATE** | **Engine decision:** pick Track A or B on parity + integration friction + TTM (evidence favors B); record as `docs/adr/0001-engine-bakeoff.md` | [You]+[CC] | ✅ **[You] ratified Track B** (lifted Vega) on 2026-06-26 — ADR 0001 *accepted*; `track-a/` archived; `alpha-core` locked as the Phase-0 engine. Follow-up: `vega`→`alpha` namespacing PR. |

---

## Phase 1a — Rigor crown-jewel + data plane · [CC]
Goal: a trusted single backtester with full statistical rigor + population calibration. No money/keys. Proves **TEST-3**.

| ID | Task | Owner | Done-when |
|---|---|---|---|
| B1a.1 | Free-data ingest (Binance WS/historical, Kite ₹500 historical, NSE bhavcopy, Dukascopy) → Parquet/DuckDB cold store | [CC] | ✅ **B1a.1a** (#28) Parquet/DuckDB `BarStore` (Decimal-exact, idempotent/reproducible, DuckDB analytics) + lifted `data/{normalize,historical}` readers; **B1a.1b** (#30) Binance BTC-perp 5m + NIFTY-constituent (RELIANCE) daily **fetchers** (`data/ingest/` pure transforms + `scripts/ingest_cold_store.py`) — **289 + 21 bars loaded reproducibly** into the store. |
| B1a.2 | Lift Vega cost model (Indian stack + TDS) into the chosen engine; **add perp funding accrual → P&L + risk gate** (R13) | [CC] | ✅ (#32) cost model already lifted (`execution/costs.py`); **perp funding added** — `execution/funding.py` + `OMS.accrue_funding` (into P&L **and** `_day_realized`) + `run_backtest` funding loop. Proven: funding shows in backtest P&L **and** a funding-bleed trips the daily-loss kill. |
| B1a.3 | Lift Vega rigor (look-ahead audit + walk-forward + 2× stress + portfolio rigor) into `alpha-core` | [CC] | ✅ (#34) rigor core already lifted (B0.9d, byte-identical); B1a.3 **finalized** it — threaded perp funding through `run_portfolio_backtest` (portfolio rigor now correct on perps; closed the TODO) + `rigor.py` to 100% + equity-skip test. Vega rigor suites green in `alpha_core`. |
| B1a.4 | Extend rigor: CPCV + embargo + PBO | [CC] | ✅ (#35) `backtest/overfitting.py` (pure-stdlib): CPCV+embargo splits + PBO via CSCV; `config/rigor.yaml`. PBO flags a reversing-overfit control (~1.0) vs a genuine edge (0.00); noise is no-skill (~0.5). |
| B1a.5 | Deflated Sharpe + **keyed** persistent trial ledger (R4): `research_ledger` keyed by (market, family, window); atomic increments | [CC] | ✅ (#36) `backtest/dsr.py` (PSR/DSR, Bailey–LdP, stdlib `NormalDist`) + the keyed SQLite ledger (`market\|family\|window`). DSR penalty bit-for-bit invariant to an unrelated cell. *(The bare-counter `trial_ledger.py` was **superseded by `research/proposal_ledger.py`** at B1b.3b #56 — one idempotent store; the cell-invariance demonstration migrated with it.)* |
| B1a.6 | Roll-forward locked holdout (R5) in a **separate no-ACL DuckDB** store (R6); record `holdout_window_version` | [CC] | ✅ (#38) `data/holdout.py`: holdout bars routed to a physically separate gate-only store at a **disjoint root** (the cold store's Parquet glob can't reach them); proven holdout-free via *every* cold-store read path (Bar reader + DuckDB view). Re-seal **rebuilds** the rolled-forward window (no stale contamination); `holdout_window_version` recorded. |
| B1a.7 | Reproducibility ledger: record strategy/dataset/engine/cost versions + RNG seed per backtest | [CC] | ✅ (#40) `research/reproducibility.py`: `ReproRecord` captures **all** of `run_backtest`'s inputs (engine/dataset/instruments/risk/cost/strategy+params/seed/venue/cash/stress/funding) → a `fingerprint`; `ReproLedger` (SQLite) persists record+result hash and **enforces** reproducibility (same inputs, different result → raises). `run_and_record` binds the record to the run. Proven: a seeded stochastic strategy re-runs bit-for-bit; any input change flips the fingerprint. |
| B1a.8 | vectorbt pre-screen (coarse walk-forward) upstream of CPCV; CPCV compute budget + parallel pool + carry-forward (R7) | [CC] | ✅ **B1a.8a** (#42) `research/prescreen.py` — vectorbt coarse walk-forward (faithful event-driven signals → vbt sim, `direction='both'` for short alpha) culls losers below a Sharpe floor before CPCV; **B1a.8b** (#43) `research/cpcv_budget.py` — persistent `CpcvQueue` + `drain_within_budget` (budget-aware over any Executor; poison-candidate quarantine) carries the unfinished queue, never truncates. vectorbt in the dev group only. |
| B1a.9 | Population calibration harness (R14): synthetic noise / overfit / planted-signal populations; measure false-promote & false-reject rates | [You]+[CC] | ✅ (#45) `research/calibration.py` — noise/overfit/edge controls through the DSR-on-OOS gate. [You] ratified the Tier-2 bounds (false-promote ≤5%, false-reject ≤25%); measured (mean over 20 seeds) **false-promote 0.0, false-reject 0.056** at a realistic strong edge (Sharpe ~2.4, ~5y OOS). Review caught + fixed a rigged false-reject → gate loosened to `dsr.threshold` 0.92 (safe end). The gate is **conservative by design** — it certifies STRONG edges (Sharpe ~2.4+); weaker edges rejected (documented floor). |
| **1a.GATE** | error-rate thresholds met; holdout/ledger unreachable by agents (TEST-3); DSR cell-invariance; reproducibility | [You]+[CC] | ✅ **[You] ratified 2026-06-26** (`docs/adr/0002`) — all four hold: error rates (B1a.9 — false-promote 0 / false-reject 0.056 at a realistic strong edge) + TEST-3 (B1a.6) + DSR cell-invariance (B1a.5) + reproducibility (B1a.7). **Conservative gate accepted** (`dsr.threshold` 0.92; certifies Sharpe ~2.4+ edges). **Phase 1a COMPLETE → Phase 1b open.** |

---

## Phase 1b — AI discovery (constrained track) · [CC]
Goal: autonomous discovery + rigorous backtesting. No money/keys. Delivers the hero loop on the research box.

| ID | Task | Owner | Done-when |
|---|---|---|---|
| B1b.1 | `strategist` agent (constrained): parameterize vetted templates (seed from Vega's ~7 example strategies) + whitelisted primitives; economic-rationale prompt; **in-sample only, never sees the holdout**; originality check + trial-ledger increment | [CC] | ✅ **B1b.1a** (#48) seed-template substrate (registry + R7 examples) **+ B1b.1b** (#50) the strategist `research/strategist.py` — parameterizes a vetted template within a bounded space, validates by constructing, fingerprint-originality + keyed-ledger increment (B1a.5), **holdout-isolated structurally** (takes no data → TEST-3). The **economic-rationale LLM proposer** is the injectable pod-agent wrapper (Mac-CLI + Lemma), deferred. |
| B1b.2 | `quant-analyst` agent: CPCV/PBO, DSR vs trial count, look-ahead/survivorship, cost sensitivity, regime stability → promote/reject/revise | [CC] | ✅ (#52) `research/quant_analyst.py` — composes the OOS-edge screen + **DSR vs the cell trial count** (the promote-work) + fold-consistency + optional cell-PBO into promote/reject/revise. Done-when MET on the B1a.9 populations (overfit→reject, edge→promote ≥85%, noise never promoted), as a rate over seeds at the calibration point. Validates the deflation inputs (the safety footgun); `deflation_inputs()` is the calibration-faithful helper. *(Carried: cost-sensitivity + look-ahead/survivorship checks wire in with real backtests at B1b.3.)* |
| B1b.3 | Workflow A `discovery-cycle`: data prep → strategist → LOOP{ full-rigor backtest (holdout untouched) → quant-analyst → promote? } → survivors | [CC] | ✅ **B1b.3a** (#54) idempotent `proposal_ledger.py` + **B1b.3b(1)** (#56) it's the sole per-cell count source (`TrialLedger` retired, 1a.GATE cell-invariance migrated) + **B1b.3b(2)** (#58) `research/discovery.py` — `run_discovery_cycle` wires propose → backtest → `deflation_inputs` (cumulative count via `trial_index`) → `assess` → survivors. Done-when MET: promotes planted edges, rejects planted overfits; linkage tested. **B1b.3c** (#60) `research/engine_backtester.py` — the **real** `Backtester`: builds the strategy → runs the lifted engine (`run_backtest`, `now` injected from bar-time) on in-sample bars → per-bar returns; the loop now runs **end-to-end on the actual engine** (a real edge promotes). **B1b.3d** (#62) `research/cold_store_bars.py` — the cold-store `bars_for` adapter: resolves a cell `(market, window)` → its `config/discovery.yaml` series and reads **in-sample-only** bars from the no-ACL cold store; **TEST-3 holdout-denial proven** (structural at the store boundary — the adapter is a pure pass-through). The loop now runs on **real cold-store data, no injected seam left**. *(Carried: cross-run cumulative deflation **variance** — the trial *count* is cumulative via `trial_index`, the variance is per-run today.)* |
| B1b.4 | `nightly-discovery` schedule + `/knowledge` RAG seeds | [CC] | ✅ **B1b.4a** (#64) `research/nightly.py` — the **portable nightly run**: `run_nightly_discovery` sweeps every (cell, template) over `config/discovery.yaml` → strategist → real engine (`ColdStoreBarsFor`/`EngineBacktester`) → quant-analyst → survivors; per-cell capital alignment + poison-quarantine + a data-readiness gate (no trial-count burn on empty cells); `scripts/nightly_discovery.py` entry point. Proven on the real cold store (10 cycles fire end-to-end; 0 promote — correct on thin windows). **B1b.4b** (#66) the research-box **systemd cron** (`deploy/systemd/`) that fires it nightly + a JSON run-record (`NightlyReport.summary`, shaped to `discovery_runs`); **B1b.4c** (#66) the `/knowledge` corpus `docs/knowledge/strategies.md` + the upload/`files search` steps. **Build + DEPLOY complete (CC, operator-authorized 2026-06-27):** timer enabled on the research box (next 02:00 UTC; a manual run fired **30 cycles / 0 quarantine / 0 promote** on the sealed richer data — clean + correct), `/knowledge/strategies.md` uploaded + indexed on the pod and `files search` verified. Done-when MET → only **1b.GATE** remains. |
| **1b.GATE** | an AI-proposed strategy passes the full gate end-to-end on the research box | [You]+[CC] | ⏳ demonstrated |

---

## Phase 2 — Cockpit + copilot + surface · [CC]
Goal: the visible hero — dashboard + copilot + Telegram. Expand to `B2.x` on arrival.

- **M2.1** Vite + lemma-sdk cockpit shell (`watchChanges` + charts).
- **M2.2** System-health strip: worker/research heartbeats, last reconcile, data freshness, open `risk_events`.
- **M2.3** Backtests view incl. **rejected** candidates (Sharpe / DSR / PBO / maxDD / cost-sensitivity) — the filter is visibly working.
- **M2.4** Approvals FORM inbox + Strategies/Discovery/Exchanges/Risk/Config views.
- **M2.5** `desk` copilot (other agents as sub-tools) + Telegram surface.
- **2.GATE** walk one cockpit scenario; message `desk` on Telegram.

## Phase 3 — Paper + approval gate + worker (paper) + deadman · [CC]/[You]
Goal: the full paper pipeline with safety. Proves **TEST-2, TEST-4, TEST-5, TEST-6, TEST-7, TEST-8**.

- **M3.1** Worker on the AWS box in PAPER: Delta testnet + Binance paper (ccxt) + **Dhan/Upstox native sandbox** for equities forward-paper (not Kite replay, R9).
- **M3.2** `alpha-core` risk gate + latching kill-switch + `restore()` + reconcile on the worker.
- **M3.3** **Deadman process** (independent systemd unit, R1) + exchange-side cancel-on-disconnect + reduce-only stops.
- **M3.4** `worker_status` / `research_status` heartbeats + stale-alert schedules.
- **M3.5** Derived-P&L fold over immutable fills (R11); never mutate from trigger payloads.
- **M3.6** `commands` via `watchChanges` + ~1s poll fallback (R10); `emergency_flatten` trip path.
- **M3.7** `start_paper_run` / `evaluate_paper_run` + evaluator + **Workflow B** (risk-officer → one-shot holdout gate → **human FORM approval** → issue start command).
- **3.GATE** kill the worker mid-position → deadman flattens via broker REST within RTO (**TEST-5**); paper P&L matches backtest within tolerance; flatten from Telegram; full pipeline incl. approval works.

## Phase 4 — First real money: Delta live (staged) + SEBI long-poles · [You]+[CC]
Goal: earn the first real money on the lowest-compliance venue, while starting the slow SEBI work.

- **M4.1** Worker → LIVE on **Delta** at tiny size; staged capital 1%→5%→25%→full gated on live-vs-paper tracking error + zero `risk_events`.
- **M4.2** Exchange-side cancel-on-disconnect + reduce-only stops live (R1); CA sign-off on crypto-derivative income.
- **M4.3** *(parallel, long-lead)* SEBI algo registration + static/elastic-IP feasibility with the broker (R2).
- **M4.4** Daily Kite token relay (Mac 2FA → RLS row), fail-closed + on-demand Telegram reauth (R12).
- **M4.5** Kite **live-feed** paper (live ticker WS → strategy → simulated fills), not replay (R9).
- **4.GATE** Delta live runs at small size with clean reconcile across N sessions; equity prerequisites confirmed.

## Phase 5 — Indian equities/index live + breadth · [You]+[CC]
Goal: the real edge — liquid equities/index/options, minutes-to-daily; multi-venue concurrency.

- **M5.1** Kite/Dhan/Upstox live (equities/index + currency derivatives), staged capital.
- **M5.2** Multi-venue + multi-strategy orchestration (clean-sheet — Vega is single-adapter/process): per-venue adapter processes behind a registry + cross-venue position/risk aggregation.
- **M5.3** Elastic-IP failover + RTO/RPO runbook (R2); standby `restore()`s kill-switch + positions.
- **M5.4** ClickHouse warm store **only if** tick volume/query latency now demands it.
- **5.GATE** equity live ≈ paper on a live-feed paper window; staged capital advances.

## Phase 6 — Free-form code-gen + scale · [CC]/[You]
Goal: additive, last — the riskiest discovery track, hard-gated on isolation.

- **M6.1** Sandboxed free-form strategist (R3/D2): true OS-level isolation — gVisor/Firecracker + seccomp + cgroups + no network namespace + pure-function IPC; strategy can't reach holdout/ledger/network.
- **M6.2** Portfolio optimization across approved strategies.
- **M6.3** RL / agentic discovery; reconsider the engine only if scale/latency demands change.

---

## ⏳ Pending tracker — the single live worklist (pending items only)

> Every open item appears here exactly once. When it lands, mark ✅ in its phase table above and remove
> it here. Update via `/session-wrap` at the end of every session.

### 🟢 Unblocked — ready to start now
- **The discovery pipeline is COMPLETE + holdout-safe** — richer data ingested (#73, ~18.8k bars / 6 cells) and the multi-series cold store **SEALED (#75)**: `data.holdout.seal_cold_store` + `scripts/seal_cold_store.py` reserve **each series' own** rolled-forward holdout into `data_research` (the nightly reads this) + `data_holdout` (gate-only); `ColdStoreBarsFor` reads the sealed research store, **holdout-free for every cell** (TEST-3 proven on the canonical store). `BarStore.series()` added. Pipeline: strategist → real engine → cold store → **seal** → nightly.
- **B1b.4 DEPLOYED + verified (CC, operator-authorized 2026-06-27):** the timer is live on the research box — a manual run fired **30 cycles / 0 quarantine / 0 promote** on the sealed richer data (clean + correct: the conservative gate rightly finds no Sharpe-~2.4 edge in 3y-daily / 7wk-5m BTC+NIFTY) — and `/knowledge/strategies.md` is uploaded + indexed on the pod with `files search` verified. **Only 1b.GATE remains for Phase 1** (needs an actual edge to *exist* + [You] ratification).
- **No pure-`[CC]` discovery-pipeline task remains.** Other directions if not chasing 1b.GATE data: start **Phase 2** (cockpit), or a **[You]+[CC]** gate item (below).
- *(Prior pure-`[CC]` carried items all done: `_sharpe` #68, ORB short-stop #70; the cell-PBO-into-`discovery` item #58 was attempted + reverted → it's [You]+[CC] below.)*
- **Carried follow-ups — NOT pure-`[CC]` ([You]+[CC], both touch the calibrated gate):**
  - **cell-PBO into `discovery` (#58) — attempted, reverted, needs redesign.** Wiring the *existing* hard cell-PBO downgrade into the loop **over-rejects robust, edge-rich cells**: PBO is high both for an overfit *selection* **and** for a cell of uniformly-strong edges (selecting "the best" among equals is luck either way). It downgraded all 10 planted edges in the B1b.3 Done-when test (`test_a_discovery_run_promotes_planted_edges`: PBO 0.68 > 0.5 → 0 survivors). So it needs a **gate-design** fix — PBO as a *confirmatory* signal, not a hard primary downgrade (matching `quant_analyst`'s own docstring intent), and/or re-calibration — not a faithful wire-up. No code shipped.
  - **cumulative DSR deflation variance.** Make the **variance** cumulative across runs (persist a per-trial score; the *count* already is via `trial_index`). Changes a **calibrated** DSR input (1a.GATE calibrated the per-run point) → needs re-validation.
- *(Environment: the agents/pod are **Mac-CLI-only** — pod + `lemma` CLI + research box; the portable agent logic + tests build anywhere.)*

### 🟡 Blocked — needs [You]
- **B1b.4 live deploy ([You], outward-facing):** the build (4a/4b/4c, #64/#66) is done; deploying it touches the AWS box + the shared Vault pod — enable the `nightly-discovery` systemd timer on the research box and upload `docs/knowledge/strategies.md` as a `/knowledge` File, then verify (`docs/nightly-discovery.md`). This satisfies B1b.4's Done-when (the schedule fires; RAG answers a query). Otherwise Phase 1b is pure-`[CC]` until **1b.GATE** + the Phase-3/4 money/live gates.

### ▶️ Next action for a cold session
**Phase 0 ✅ + Phase 1a ✅ (B1a.1–B1a.9 + 1a.GATE ratified, `docs/adr/0002`).** The full rigor crown-jewel is built, calibrated, and **trusted**: cold store + ingest (#28/#30), perp funding (#32), lifted rigor + portfolio funding (#34), CPCV+embargo+PBO (#35), DSR + keyed trial ledger (#36), no-ACL holdout → TEST-3 (#38), reproducibility ledger (#40), vectorbt pre-screen + CPCV budget (#42/#43), population-calibration (#45). **500 tests, ≥94% cov; `main` clean, 0 open PRs.**

**Phase 1b in progress — B1b.1 ✅ (the `strategist`, #48 substrate + #50 logic):** it parameterizes a vetted R7 template within a bounded space, validates by constructing, fingerprint-originality + keyed-ledger increment, **holdout-isolated structurally** (takes no data → TEST-3). The economic-rationale **LLM proposer** is the injectable pod-agent wrapper (Mac-CLI + Lemma), deferred. **545 tests, ≥94% cov.**

**Phase 1b — both discovery agents done: B1b.1 the `strategist` (#48/#50) + B1b.2 the `quant-analyst` (#52).** The quant-analyst composes the rigor gate (OOS-edge screen + DSR-vs-trial-count + fold-consistency + cell-PBO) into promote/reject/revise; Done-when met on the B1a.9 populations.

**Phase 1b constrained track COMPLETE + real-engine-wired + cold-store-fed: B1b.1 strategist (#48/#50) + B1b.2 quant-analyst (#52) + B1b.3 discovery-cycle (#54/#56/#58) + B1b.3c real backtester (#60) + B1b.3d the cold-store `bars_for` adapter (#62).** `research/discovery.run_discovery_cycle` turns a search cell into promote/reject/revise decisions + survivors, runs **end-to-end on the actual engine** (`EngineBacktester` → `run_backtest` → real returns; a real edge promotes), and now reads **real in-sample bars from the no-ACL cold store** (`ColdStoreBarsFor`, keyed by `config/discovery.yaml`; TEST-3 holdout-denial proven at the store boundary). **No injected seam left.**

**B1b.4 BUILD COMPLETE — B1b.4a/4b/4c (#64/#66).** The portable nightly run (`research/nightly.py` + `scripts/nightly_discovery.py`) sweeps the universe → real engine → survivors and writes a JSON run-record (`summary()`, shaped to the pod `discovery_runs` table); a research-box **systemd cron** (`deploy/systemd/`) fires it nightly; the `/knowledge` corpus is `docs/knowledge/strategies.md`. Proven on the real cold store (10 cycles fire; 0 promote — correct on thin windows). **B1b.4 DEPLOYED + verified (CC, operator-authorized 2026-06-27):** timer live on the research box (next 02:00 UTC; a manual run = 30 cycles / 0 quarantine / 0 promote on the sealed richer data — clean + correct), `/knowledge` uploaded + indexed + `files search` verified → **B1b.4 Done-when MET**. **Only 1b.GATE remains for Phase 1** — an AI-proposed strategy passes the full gate end-to-end; needs an actual edge to *exist* (the 0-promote run shows the conservative gate rightly finds none in this data yet) + [You] ratification. **The discovery pipeline is COMPLETE + holdout-safe:** `seal_cold_store` (#75) reserves each series' own rolled-forward holdout, and the nightly reads the holdout-free `data_research` store (TEST-3 proven on the canonical store). **No clean pure-`[CC]` discovery-pipeline task remains** (done: `_sharpe` #68, ORB short-stop #70, richer ingest #73, multi-series seal #75). **cell-PBO into `discovery` (#58) was attempted + reverted** → it over-rejects robust edge-rich cells (failed the B1b.3 planted-edge Done-when: PBO 0.68 → 0 survivors), so it's **[You]+[CC]** (gate-design + re-calibration), as is the cumulative-variance item. **Phase-1 remaining = just 1b.GATE:** (1) ✅ B1b.4 deployed (CC, 2026-06-27 — timer + `/knowledge` live); (2) 1b.GATE — needs an actual edge in the data + [You] ratification. **Nightly wiring note:** point `ColdStoreBarsFor` at the **sealed research** cold store, never a raw ingestion store. **Conservative-gate note:** the live `dsr.threshold` is **0.92**.

**Deferred during the lift (each rejoins when its layer/phase lands, tracked in the coverage `omit` + checkpoint):** the **fuller `helpers/config.py`** (Env/live-gate `load_env_config` + universe config + **restore `_scan_for_secrets`**) — **[You]-gated** (touches the live gate); `strategy/registry` + the ~8 example strategies + the **options** layer; `data.universe` (needs the universe config); `portfolio/setup`. *(The data **readers** — `normalize`/`historical`/`feed` + the new `store` — landed in B1a.1a.)* **Faithful-lift provenance** `vega`→`alpha` **namespacing is done (#27)**. **Safety-gate edits** (never-do / cardinal invariants / money-live-secrets / live-gate) → **[You]** merges.
