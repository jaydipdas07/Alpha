# pod/ — the Alpha Vault pod (Lemma bundle)

This directory is the **Lemma pod bundle** for Alpha's mission control: the AI brain
(strategist / quant-analyst / risk-officer / desk agents), the cockpit dashboard, and the
human approval gate. It is **not** a Python/pip package and is therefore not a `uv`
workspace member — a Lemma bundle is deployed with the `lemma` CLI, never installed into
the worker's venv. That separation is the point: **Lemma is never on the money path**, and
the pod holds no logic that can't be re-hosted in ~a week (TEST-8 / CLAUDE.md).

- **Pod ↔ worker split.** The pod issues `commands`; the **worker** (a static-IP VPS) is the
  *sole* executor and the only holder of broker truth. The pod **never** places live orders.
- **Target Vault pod:** `Lemma` — pod id `019ef606-7b77-76f1-853a-978ddf819415`.

## Layout

- `pod.json` — bundle manifest (`format_version` 2: `name` + `description`); the pod identity.
- `tables/<name>/<name>.json` — the 15 table schemas (B0.4). Money/price/qty columns are `TEXT` (B5).
- `seed/seed.sh` — coherent demo seed (records don't round-trip through import; run once after import).
- `apps/cockpit/` — the Phase-2 **cockpit** app: `cockpit.json` (manifest) + `source/` (a Vite +
  `lemma-sdk` project). `source/` is built (`npm ci && npm run build` → `dist/`) on import. See **Apps** below.

Further resources (functions, agents, workflows, schedules, surfaces) are authored with the Lemma
CLI — `lemma schema <resource>` / `lemma <resource> init` print the canonical shapes (see the
`lemma-builder` skill) — as their phases land (the cockpit app began in Phase 2 / M2.1).

## Tables (B0.4)

`strategies` · `backtests` · `discovery_runs` · `research_ledger` (keyed by `cell_key` =
`market|family|window`, R4) · `deployments` · `paper_runs` · `orders` (idempotent on
`client_order_id`) · `fills` (deduped on `dedup_key`) · `positions` · `pnl_snapshots` · `risk_events` ·
`commands` (incl. `emergency_flatten`) · `worker_status` · `research_status` · `broker_credentials`
(RLS). All shared (`enable_rls:false`) except `broker_credentials` (per-user RLS — the daily Kite token
relay). FKs hang off `deployments` / `strategies` / `orders`. Lemma has no composite-unique, so
composite keys are synthetic unique `TEXT` columns (`cell_key`, `dedup_key`, `position_key`,
`snapshot_key`).

## Import & seed runbook

```bash
lemma pods import pod/ --dry-run --pod 019ef606-7b77-76f1-853a-978ddf819415   # validate
lemma pods import pod/ --pod 019ef606-7b77-76f1-853a-978ddf819415             # upsert tables (+ build the app)
bash pod/seed/seed.sh                                                          # demo seed (run once)
```

> A full `lemma pods import pod/` also **builds the cockpit app** (`apps/cockpit/source/` → `dist/`).
> For a tables-only change, scope it: `lemma pods import pod/tables/<name>` (skips the app build).

## Apps — cockpit (Phase 2)

The **cockpit** (`apps/cockpit/`) is Alpha's mission-control dashboard — a Vite + `lemma-sdk` app that
reads the pod's tables live (`watchChanges`) and renders system health, discovery, backtests (incl.
**rejected** candidates), approvals, and risk. It is a **read + governance-command** surface only: it
never places a live order (the worker executes — **TEST-8**) and never surfaces the holdout
(**TEST-3**). Plan: `apps/cockpit/DESIGN.md`.

```bash
cd pod/apps/cockpit/source && npm install && npm run dev    # dev (auto-authed via the dev-token plugin)
#   agent/headless browser → same-origin proxy (no CORS in dev):
#   VITE_LEMMA_API_URL=/api LEMMA_DEV_PROXY_TARGET="https://api.lemma.work" npm run dev
npm run build                                               # tsc -b && vite build → dist/  (CI + import run this)
```

**Deploy is operator-authorized** (outward-facing, like the B1b.4 nightly deploy): either the whole
bundle (`lemma pods import pod/` rebuilds the app) or just the app
(`lemma apps deploy cockpit --source-dir pod/apps/cockpit/source`), then `lemma apps open cockpit` to
view it served. CC does **not** deploy the cockpit unattended.

## Status (Phase 0)

- **B0.1: ✅** bundle scaffolded (manifest + this README).
- **B0.3 `[You]+[CC]`: ✅ done.** `lemma` 0.5.0 reaches the active Vault; **B5 locked** (see money note
  below); a `Decimal` round-trips exactly through a `TEXT` column.
- **B0.4 `[CC]`: ✅ done.** All 15 tables authored in `tables/` + imported to the Vault; `seed/seed.sh`
  populates a demo chain. Verified: money round-trips exactly through a FK JOIN; a bogus FK + a
  duplicate `client_order_id` are rejected; `broker_credentials` RLS owns rows. **Every money/price/qty
  column is `TEXT`** (B5).

> **Money is never a float at the Lemma boundary (B5 ✅, locked in B0.3).** Lemma has **no native
> DECIMAL** type, `FLOAT` loses precision (proven: `12345678901234567.89` → `…568`), and `INTEGER` is
> **int32** (overflows on minor-units). So store money/price/quantity as a **`TEXT` string-encoded
> `Decimal`**, round-tripped via `Decimal(str)` in `alpha-core`. Never a `FLOAT` column.
