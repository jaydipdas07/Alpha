# CLAUDE.md — Alpha

Alpha is an **AI-driven multi-asset algorithmic auto-trading platform**: an autonomous
strategy-discovery + backtesting engine whose survivors are paper-traded, **human-approved per
strategy**, then deployed live. **Read this file, then `TASKS.md` (the ⏳ Pending tracker at the end
= the live worklist), then `docs/DESIGN_v4.md` (canonical design) and `BUILD_PLAN.md` (v3 backbone) at
the start of every session.** This file is the contract — obey it over any local convenience. The
doc-map is in `README.md`.

## What Alpha is
- A **pod ↔ worker split**. The **Vault pod** (Lemma — pod id `019ef606-7b77-76f1-853a-978ddf819415`)
  is mission control: the AI brain (strategist/quant-analyst/risk-officer/desk agents), the dashboard,
  and the human approval gate. The **worker** (a static-IP VPS) is the *sole* executor. A **research
  box** (the AWS machine) runs backtests/discovery with no live keys.
- A **thin `alpha-core` package** holds the portable strategy contract + the risk/cost/reconcile/rigor
  kernel, imported identically by the research-box backtester and the live worker — this is the
  parity guarantee.
- Markets: crypto derivatives (**Delta** live, **Binance** data/testnet, via ccxt) and Indian
  equities/F&O/currency derivatives (**Kite + Dhan + Upstox**, adapter-per-broker).
- Frequency: seconds-to-minutes (crypto) + minutes-to-daily (equities). Architecture is
  frequency-agnostic; sub-second is retired as a near-term goal (E5).

## Where we are
**Pre-build — this repo is the build.** The design is finalized (`docs/DESIGN_v4.md`); no application
code exists yet. The first job is **Phase 0**: scaffold `alpha-core`/`worker`/`pod`, verify Lemma +
money types, and run the **engine bake-off** (E1) — a co-equal A/B between a NautilusTrader-shell and
**lifting the operator's own proven Vega engine wholesale** (you own Vega outright; evidence favors
the lift). Much of the engine is *lifted, not written* — treat Vega (`/Users/jaydipdas/Code/Vega`) as
a trustworthy machine to copy from, not a greenfield to re-derive.

> **Live status is deliberately not kept here** (it would rot; there must be one source of truth). For
> where things stand *this* session use the **Session continuity** protocol below: the ⏳ tracker at
> the end of `TASKS.md`, `git log` / `gh pr list`, and the checkpoint memo.

## Session continuity & memory (read at the start of every session)
A new session starts cold with only what is **in the checkout**. Resume protocol:
1. Read `CLAUDE.md` → `TASKS.md` (the ⏳-tracker at the end = live worklist) → `git log --oneline -8`
   and `gh pr list` → `git branch --show-current`. These exist in every checkout and are the source of truth.
2. **Local accelerator (CLI only):** `~/.claude/projects/-Users-jaydipdas-Code-Alpha/memory/alpha-build-checkpoint.md`
   (gitignored, auto-loaded) holds the exact next action. Refresh it at session end with `/checkpoint`.
   A remote/web clone won't have it — fall back to step 1.
3. **One session per checkout.** For parallel work use a `git worktree`; check `git branch --show-current` before acting.
4. **Before you wrap up:** run `/session-wrap` (update the `TASKS.md` ⏳-tracker — the record *every*
   session can read), then on CLI `/checkpoint` (refresh the local accelerator).

## Engineering invariants (never violate)
- **Money is never a float.** `Decimal` for every price/quantity/P&L inside `alpha-core`; at the Lemma
  boundary store money as **native DECIMAL** (else string-encoded `Decimal`) — never a float column (B5).
- **Time is always tz-aware UTC** internally; convert only at the edges. Never read wall-clock inside
  the engine — `now` is **injected** (bar-time in backtest) so backtest ≡ live.
- **Orders are idempotent + audited.** Client-generated IDs + dedup-by-query; append-only,
  hash-chained audit log of every decision and order.
- **The broker is the source of truth.** Reconcile on startup and periodically; on **unexplained**
  drift, **halt + alert — never guess**; zero phantom positions.
- **Risk is hand-written and non-bypassable.** The halt gate is checked first on every order; a tripped
  kill-switch **latches** and survives restart (`restore()`); re-arm only on a clean reconcile.
- **Backtest and live share the same code.** The research box and the worker import the same
  `alpha-core`. If they diverge, the backtest is lying.

## Cardinal invariants (TEST-1..8 — the build proves each)
- **TEST-1 Parity** — same strategy code + injected `now` → backtest ≡ live on identical bars.
- **TEST-2 Derived P&L** — all P&L/exposure is a pure fold over deduped, immutable fills; never mutated from trigger payloads (R11).
- **TEST-3 Holdout isolation** — the strategist / RAG / table-read agents cannot read the holdout partition via any tool (R6).
- **TEST-4 Non-bypassable risk** — every order hits the halt gate first; the kill-switch latches + survives restart (`restore()`).
- **TEST-5 Deadman flatten** — a dead worker mid-position is flattened by the independent deadman + exchange-side cancel-on-disconnect/reduce-only within RTO (R1).
- **TEST-6 Idempotency** — one signal intent → one order (client-order-id + dedup-by-query); a duplicated fill trigger leaves P&L unchanged.
- **TEST-7 Reconcile-truth** — broker is truth; unexplained drift halts; explained drift adopts; phantom positions are zeroed.
- **TEST-8 Pod-never-trades** — the pod never places live orders; execution + broker-truth live only on the worker; **Lemma is never on the money path** and holds no logic that can't be re-hosted in ~a week.

## Configuration & tunables — single source of truth
**No magic numbers in code.** Every business parameter lives in `config/`, validated by
`pydantic-settings`, loaded per environment:
- `config/risk.yaml` — all risk limits (the most-tuned file).
- `config/costs.yaml` — fees, taxes (Indian stack + crypto TDS), slippage, perp funding.
- `config/rigor.yaml` — CPCV embargo, DSR thresholds, holdout cadence, population-calibration defs.
- `config/strategies/<name>.yaml` — per-strategy parameters.
- `config/instruments.yaml` — tradable universe, lot/tick sizes.
- `config/venues.yaml` — adapter registry (Delta/Binance/Kite/Dhan/Upstox).
- `config/<env>.yaml` (`dev` / `paper` / `live`) — infra: active adapters, DB, log level, the live gate.

Tweaking a metric must mean editing **one config value**. Any PR that hard-codes a tunable is rejected.

## ID taxonomy & owners (see `TASKS.md`)
- **`B<phase>.<seq>`** executable build task (`B0.1`, `B1a.3`) · **`M<phase>.<seq>`** milestone
  work-package for phases 2–6 · **`<phase>.GATE`** phase decision/acceptance gate · **`TEST-<n>`**
  cardinal invariant · **`G<n>`** gap/issue as discovered.
- Reference inline: **R1–R14** (v3 hardening, `BUILD_PLAN.md`), **E1–E5 / B1–B5 / D1–D2** (locked decisions).
- Owners: **`[CC]`** Claude Code builds · **`[You]`** keys / money / regulator / the live-gate flip ·
  **`[You]+[CC]`** you open the door, CC drives.

## Stack
Python 3.12+ · `uv` · `ruff` · `mypy --strict` · `pytest` + `hypothesis` · `pydantic` /
`pydantic-settings` · `structlog` · Parquet/DuckDB (cold + warm; ClickHouse only if Phase-5 volume
forces) · `ccxt` / `ccxt.pro` · `kiteconnect` (+ Dhan/Upstox SDKs) · NautilusTrader **or** lifted Vega
(engine decided by the Phase-0 bake-off) · Lemma SDK (pod) · Docker.

## Repo conventions
- **One change = one branch + one PR**, clear message (`feat(core): order FSM`, `adr: 0001 engine bake-off`).
  Branch protection on `main`.
- ADRs in `docs/adr/` (created as decisions land, starting with the engine-choice ADR at `0.GATE`).
- **Check work before "done":** `ruff` clean, `mypy --strict` clean, tests written and passing for any
  modified logic. A task is done only when its **Done-when** (in `TASKS.md`) is demonstrably met.
- Secrets only in gitignored `.env` (reused from Vega's `.env`, B3); never commit or log them.

## What Claude Code must NEVER do (these belong to the human, by design)
- Create accounts, log in, or enter/commit credentials, API keys, or secrets.
- Place real-money / live orders, move funds, or flip the live gate (worker → live).
- **Make the pod place live orders** — the pod issues `commands`; only the worker executes (TEST-8).
- **Surface the holdout to any agent / RAG / cockpit view** — it lives in a no-ACL store; redact it (TEST-3).
- Copy live broker secrets into the pod — the pod relays only the daily Kite token; the API *secret* stays on the worker.
- Provision paid infrastructure (VPS, cloud, paid data) or incur cost without explicit approval.
- Perform irreversible actions (force-push, history rewrite, `reset --hard`, data deletion) without approval.
- **Fabricate a `▲ DECIDE HERE` value.** If something required is missing or unclear, **STOP and ask (max 3 questions)**, then proceed.

The human reviews and approves **every PR**, and **every go-live is a human FORM approval**. Build
incrementally — never "build it all at once."

## Environment boundary (from `docs/PHASE0_HANDOFF.md`)
The build runs on the **Mac `lemma` CLI session** — it alone has the `lemma` CLI + Vault pod, the Vega
repo, the `.env` keys, and SSH to the AWS box. A cloud/container session **cannot** reach Vega, `.env`,
or AWS — anything touching those is Mac-CLI-only. Plain-Python deliverables (`alpha-core`, `worker`,
rigor) can be built and pushed from anywhere.

## How to proceed
Orient with the **Session continuity** protocol (source-of-truth files in every checkout). The live
worklist is the **⏳ Pending tracker at the end of `TASKS.md`** — work whatever is unblocked, **one
task at a time = one branch + one PR**, until its **Done-when** holds. **Stop at human-gated steps**
(the never-do list). Close every non-trivial session with `/session-wrap` (+ `/checkpoint` on CLI).
