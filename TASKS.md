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
| **0** | Foundation + **engine bake-off** (Nautilus-shell vs lift-Vega) → engine chosen | [CC]/[You] | ⏳ |
| **1a** | Rigor crown-jewel + data plane (+ population calibration) | [CC] | ⏳ |
| **1b** | AI discovery — constrained track (strategist + quant-analyst + Workflow A) | [CC] | ⏳ |
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
| B0.8 | **Track A (Nautilus-shell):** install NautilusTrader; wrap the contract in a Nautilus strategy; wire the Vega-pattern Indian cost model (+funding) into Nautilus's fee hook; Nautilus backtest + Delta-testnet live-paper via ccxt | [CC] | ⏳ trivial strategy trades on Delta testnet via Nautilus; backtest runs |
| B0.9 | **Track B (lift-Vega):** copy Vega `core`/`execution`/`risk`/`backtest` into `alpha-core` (rename `tradebot`→`alpha_core`); run the same contract + Delta-testnet live-paper + backtest through the lifted engine | [CC] | 🟡 **engine lifted** (core+risk+execution+strategy-engine+backtest/portfolio, #14–#18, 357 tests, ≥94% cov) **+ B0.7 contract proven through it in backtest** (#19, deterministic, fills+Decimal P&L); ⏳ **Delta-testnet live-paper (ccxt) remains** |
| B0.10 | Parity + oracle: assert backtest ≡ paper parity on a replay per track (**TEST-1**); stand up the Vega differential-oracle (its property/replay suite) cross-checking P&L/positions; measure integration friction + time-to-code | [CC] | ⏳ parity asserted both tracks; comparison memo written |
| **0.GATE** | **Engine decision:** pick Track A or B on parity + integration friction + TTM (evidence favors B); record as `docs/adr/0001-engine-bakeoff.md` | [You]+[CC] | ⏳ engine chosen + documented; `alpha-core` foundation locked |

---

## Phase 1a — Rigor crown-jewel + data plane · [CC]
Goal: a trusted single backtester with full statistical rigor + population calibration. No money/keys. Proves **TEST-3**.

| ID | Task | Owner | Done-when |
|---|---|---|---|
| B1a.1 | Free-data ingest (Binance WS/historical, Kite ₹500 historical, NSE bhavcopy, Dukascopy) → Parquet/DuckDB cold store | [CC] | ⏳ BTC-perp 5m + a NIFTY-constituent daily load reproducibly |
| B1a.2 | Lift Vega cost model (Indian stack + TDS) into the chosen engine; **add perp funding accrual → P&L + risk gate** (R13) | [CC] | ⏳ funding shows in backtest P&L + kill threshold (funding-bleed trips the loss gate) |
| B1a.3 | Lift Vega rigor (look-ahead audit + walk-forward + 2× stress + portfolio rigor) into `alpha-core` | [CC] | ⏳ Vega rigor tests green in `alpha-core` |
| B1a.4 | Extend rigor: CPCV + embargo + PBO | [CC] | ⏳ PBO computed; flags a known-overfit control |
| B1a.5 | Deflated Sharpe + **keyed** persistent trial ledger (R4): `research_ledger` keyed by (market, family, window); atomic increments | [CC] | ⏳ DSR penalty invariant to unrelated cells |
| B1a.6 | Roll-forward locked holdout (R5) in a **separate no-ACL DuckDB** store (R6); record `holdout_window_version` | [CC] | ⏳ agents/RAG cannot read the holdout via any tool (**TEST-3**) |
| B1a.7 | Reproducibility ledger: record strategy/dataset/engine/cost versions + RNG seed per backtest | [CC] | ⏳ a backtest is bit-for-bit reproducible from its record |
| B1a.8 | vectorbt pre-screen (coarse walk-forward) upstream of CPCV; CPCV compute budget + parallel pool + carry-forward (R7) | [CC] | ⏳ losers culled pre-CPCV; night budget respected, queue carried not truncated |
| B1a.9 | Population calibration harness (R14): synthetic noise / overfit / planted-signal populations; measure false-promote & false-reject rates | [You]+[CC] | ⏳ measured rates meet the Phase-1b gate thresholds (Tier-2 params) |
| **1a.GATE** | error-rate thresholds met; holdout/ledger unreachable by agents (TEST-3); DSR cell-invariance; reproducibility | [You]+[CC] | ⏳ all four hold |

---

## Phase 1b — AI discovery (constrained track) · [CC]
Goal: autonomous discovery + rigorous backtesting. No money/keys. Delivers the hero loop on the research box.

| ID | Task | Owner | Done-when |
|---|---|---|---|
| B1b.1 | `strategist` agent (constrained): parameterize vetted templates (seed from Vega's ~7 example strategies) + whitelisted primitives; economic-rationale prompt; **in-sample only, never sees the holdout**; originality check + trial-ledger increment | [CC] | ⏳ proposes a valid strategy config; holdout untouched (TEST-3) |
| B1b.2 | `quant-analyst` agent: CPCV/PBO, DSR vs trial count, look-ahead/survivorship, cost sensitivity, regime stability → promote/reject/revise | [CC] | ⏳ rejects a planted overfit; promotes a planted edge |
| B1b.3 | Workflow A `discovery-cycle`: data prep → strategist → LOOP{ full-rigor backtest (holdout untouched) → quant-analyst → promote? } → survivors | [CC] | ⏳ a run produces promote/reject decisions end-to-end |
| B1b.4 | `nightly-discovery` schedule + `/knowledge` RAG seeds | [CC] | ⏳ schedule fires; RAG answers a strategy-doc query |
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

### 🟢 Unblocked — ready to start now (full A/B bake-off authorized by [You])
- **B0.9f (Delta-testnet live-paper)** — the Track-B engine lift (B0.9a–e, #14–#18) + the backtest proof (#19) are ✅ on `main`. **Remaining:** lift `adapters/crypto_ccxt.py` (561L) + install `ccxt` (+`ccxt.*` mypy override), point it at **testnet** (`DELTA_TESTNET_*` in `.env`), run `MaCrossover` live-paper so a trivial signal places a **testnet** order; capture the transcript into `docs/bakeoff_b0_9f.md`. **External** (network + a tiny testnet order). ⚠ confirm target: the lifted adapter was validated on **Binance** testnet (its docstring) — [You] said **Delta** (and `DELTA_TESTNET_*` keys are present). **No live keys / live gate** — testnet only.
- **B0.8** — Track A: install NautilusTrader, wrap the contract, Vega-pattern cost model, Delta-testnet live-paper. *(Full A/B bake-off authorized; **stop-and-ask [You] before starting** — needs NautilusTrader installed.)*

### 🟡 Blocked — needs a prerequisite
- **B0.10 → 0.GATE** — parity bake-off + engine decision (→ `docs/adr/0001`) — needs B0.8 **and** B0.9.

### ▶️ Next action for a cold session
**Done: B0.1–B0.7 ✅; PR-merge norm (#6); B5 = `TEXT` string-Decimal; 15 pod tables + seed; research host up; and the whole Track-B engine lift — B0.9a core (#14), B0.9c risk (#15), B0.9b execution+substrate (#16), B0.9e strategy-engine (#17), B0.9d backtest+portfolio (#18), B0.9f-backtest proof (#19). `alpha-core` now runs the B0.7 contract through the lifted engine in a deterministic backtest; 357 tests, ≥94% cov; `main` clean, 0 open PRs.**

**Next:** finish **B0.9f** = **Delta-testnet live-paper** (the one remaining bake-off half — see 🟢 above; external, places a tiny testnet order; confirm Binance-vs-Delta target first). Then **B0.8** (Track A Nautilus — **stop-and-ask [You]**, needs NautilusTrader installed) → **B0.10 → 0.GATE** (parity oracle + engine choice → `docs/adr/0001`). Drive each under the **PR norm** (build → CI green → sub-agent review → **CC merges** squash).

**Deferred during the lift (each rejoins when its layer/phase lands, tracked in the coverage `omit` + checkpoint):** the **fuller `helpers/config.py`** (Env/live-gate `load_env_config` + universe config + **restore `_scan_for_secrets`**) — **[You]-gated** (touches the live gate); `strategy/registry` + the ~8 example strategies + the **options** layer; the **data** ingest layer (`data.universe` etc.); `portfolio/setup`. **Faithful-lift provenance** (`vega-` client-order-id prefix, `vega_*` metric names, `vega_append_only` trigger, "Vega backtest" label) — left as-is, **namespaced `vega`→`alpha` at 0.GATE** (separate mechanical PR). **Safety-gate edits** (never-do / cardinal invariants / money-live-secrets / live-gate) → **[You]** merges.
