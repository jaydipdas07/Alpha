---
description: Dump current session state into the durable build-checkpoint memory
argument-hint: "[optional note to emphasize]"
allowed-tools: Bash(git branch --show-current), Bash(git status*), Bash(git log*), Bash(gh pr list*), Read, Edit, Write
---

Capture the current state of this session into durable memory so the next session can resume cold.

## Gather

Run these and use the results (do not guess):

- Current branch: !`git branch --show-current`
- Working tree: !`git status --short`
- Recent commits: !`git log --oneline -8`
- Open PRs: !`gh pr list --state open --limit 10 --json number,title,headRefName,state -q '.[] | "#\(.number) [\(.state)] \(.headRefName) — \(.title)"' 2>/dev/null`

## Write the checkpoint

Update `/Users/jaydipdas/.claude/projects/-Users-jaydipdas-Code-Alpha/memory/alpha-build-checkpoint.md`
(create it if absent; keep any frontmatter; this file is a running log — prepend a new dated entry at
the top of the body, above the previous `LATEST` note, and demote the old one).

The new entry MUST be concrete and verifiable, covering:
- **Date** (today's date) and a one-line headline of where the build now stands (which `B`/`M` task).
- **Branch + open PRs** (from the gathered data) and whether anything is uncommitted.
- **What changed this session** — merged PRs, decisions made, files/areas touched.
- **Exact next action** — the single next `TASKS.md` task a fresh session should pick up.
- **Blockers / human-gated items** still outstanding (any `[You]` / `[You]+[CC]` step).
$1

Then update the `MEMORY.md` index line for the checkpoint if its hook/summary changed.

## Rules
- One fact per claim; cite branch/PR numbers and task IDs, not vibes. Convert relative dates to absolute.
- Do **not** record what the repo already captures (git history, code structure, CLAUDE.md, TASKS.md tables).
- Do **not** commit — this is local memory. Report a 3–5 line summary of what you wrote.
