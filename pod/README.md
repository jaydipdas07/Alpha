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

Resources (tables, functions, agents, workflows, schedules, surfaces, app) are authored with
the Lemma CLI — `lemma schema <resource>` / `lemma <resource> init` print the canonical shapes
(see the `lemma-builder` skill) — and land in **B0.4**.

## Status (Phase 0)

- **B0.1 (this PR):** bundle scaffolded (manifest + this README). No tables/agents yet; nothing
  imported to the Vault.
- **B0.3 `[You]+[CC]`: ✅ done.** `lemma` 0.5.0 reaches the active Vault; **B5 locked** (see money note
  below); a `Decimal` round-trips exactly through a `TEXT` column.
- **B0.4 `[CC]`:** author all tables (deployments, strategies, backtests, discovery_runs, paper_runs,
  orders, fills, positions, pnl_snapshots, risk_events, `commands` (+ `emergency_flatten`),
  `worker_status`, `research_status`, keyed `research_ledger`, `broker_credentials` RLS) + seed, then
  import. **Every money/price/qty column is `TEXT`** (B5).

> **Money is never a float at the Lemma boundary (B5 ✅, locked in B0.3).** Lemma has **no native
> DECIMAL** type, `FLOAT` loses precision (proven: `12345678901234567.89` → `…568`), and `INTEGER` is
> **int32** (overflows on minor-units). So store money/price/quantity as a **`TEXT` string-encoded
> `Decimal`**, round-tripped via `Decimal(str)` in `alpha-core`. Never a `FLOAT` column.
