#!/usr/bin/env bash
# PostToolUse(Edit|Write|MultiEdit): advisory ruff lint on the touched Python file.
#
# Report-only and non-blocking by design:
#   - it never edits the file (no --fix), so it can't race the next Edit;
#   - it always exits 0, so it never interrupts the turn.
# pre-commit (--fix) and CI remain the enforcing gate; this is just fast feedback
# so a lint slip is visible at edit time instead of minutes later in CI.
#
# No-op until alpha-core has a uv project (Phase 0 / B0.1); `uv run ruff` simply
# returns nothing useful before then, and the hook stays silent.
set -uo pipefail

f="$(cat 2>/dev/null | jq -r '.tool_input.file_path // empty' 2>/dev/null)" || exit 0
[ -z "$f" ] && exit 0
case "$f" in
  *.py) ;;
  *) exit 0 ;;
esac
[ -f "$f" ] || exit 0

out="$( (cd "${CLAUDE_PROJECT_DIR:-.}" && uv run ruff check --quiet "$f") 2>&1 )" || true
if [ -n "$out" ]; then
  echo "ruff (advisory, not blocking) — $f:" >&2
  printf '%s\n' "$out" >&2
fi
exit 0
