---
description: Close the session — update the TASKS.md pending tracker, then prompt /checkpoint
argument-hint: "[optional note on what landed]"
allowed-tools: Bash(git branch --show-current), Bash(git status*), Bash(git log*), Bash(gh pr list*), Read, Edit
---

Execute **session continuity step 4** from `CLAUDE.md`: leave the committed record that *every* future
session (including a remote/web clone with no local memory) can read. This is distinct from
`/checkpoint` — that writes local-only memory; this updates the in-repo source of truth.

## Gather (do not guess)

- Branch: !`git branch --show-current`
- Working tree: !`git status --short`
- Landed recently: !`git log --oneline -10`
- Open PRs: !`gh pr list --state open --limit 10 --json number,title,headRefName -q '.[] | "#\(.number) \(.headRefName) — \(.title)"' 2>/dev/null`

## Update the TASKS.md ⏳ Pending tracker

Edit the **⏳ Pending tracker at the end of `TASKS.md`** so it reflects reality:

- Move anything that landed this session out of the tracker and mark it ✅ in its phase table row.
- Re-evaluate **🟡 blocked → 🟢 unblocked** as prerequisites land.
- Refresh the **▶️ Next action** so a cold session knows exactly what to pick up.
- Keep the invariant: **every open item appears in the tracker exactly once**; no open work lives only
  in a phase table, a design doc, or a PR description.
- $1

## Then

Report a 3–5 line summary of what you changed in the tracker, and remind the user to run
**`/checkpoint`** (local memory) if on the CLI. Do **not** commit unless the user asks — the tracker
change rides the session's normal one-branch-one-PR flow.
