---
description: Audit docs vs code for drift (a doc/code disagreement is a bug) and report it
argument-hint: "[area to focus, e.g. risk, reconcile, rigor, pod]"
allowed-tools: Bash(git diff*), Bash(git log*), Bash(grep*), Bash(rg*), Read, Task
---

Find where the documentation and the code disagree. Per `CLAUDE.md`, **a doc/code disagreement is a
bug** — this command surfaces them so they can be fixed (or the doc corrected).

## Scope

Compare the design surface against the code it claims to describe:

- `docs/DESIGN_v4.md` (canonical) + `BUILD_PLAN.md` (backbone) — do the engine-approach table, the
  Vega-lift matrix, and the phase roadmap still match what's actually built in `alpha-core` / `worker` / `pod`?
- `TASKS.md` — are ✅-marked tasks actually done (their **Done-when** demonstrably met)? Is every open
  item in the ⏳ tracker exactly once?
- `docs/adr/*.md` (as they appear) — each ADR vs the module it governs (starting with the engine-choice ADR).
- `docs/PHASE0_HANDOFF.md` — do the referenced commands/keys/hosts/readiness tiers still hold?
- `README.md` doc-map + `CLAUDE.md` invariants — do the boldest claims hold (parity, holdout isolation,
  pod-never-trades, derived-P&L)?

Focus area (if given): **$1**

## How

For a thorough sweep, delegate to a **read-only Explore/general agent** (no edits). For a quick check,
grep the claim and read the cited code directly. Verify the boldest claims first — coverage numbers,
"parity", "holdout isolated", "non-bypassable", "phantom zeroed", "pod never trades", per-task status.

## Report

A `file:line`-keyed table: **doc claim → what the code actually does → verdict (matches / drifted /
stale) → fix (correct the doc, or the code).** Rank by blast radius: safety/correctness misdirection
first. Do not edit anything — this is a read-only audit; the fixes are separate, reviewed PRs.
