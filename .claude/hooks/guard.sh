#!/usr/bin/env bash
# PreToolUse(Bash) guard enforcing the CLAUDE.md "never-do" list.
#
# Exit 2  -> block the call and feed the reason back to Claude.
# Exit 0  -> allow.
#
# Defense-in-depth behind permissions.deny in settings.json. Matching is
# EXECUTION-CONTEXT aware (not a bare substring), so merely *mentioning* a token
# in a PR body, file content, grep pattern, or doc string is NOT blocked — only a
# command that would actually flip the live gate, start a real-venue runner, or
# rewrite git history. This matters because Alpha's own docs reference these tokens.
#
# Philosophy: fail-OPEN on a parse error (a broken guard must not brick the
# session); permissions.deny remains the declarative hard stop.
set -uo pipefail

input="$(cat 2>/dev/null)" || exit 0
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty' 2>/dev/null)" || cmd=""
[ -z "$cmd" ] && exit 0

block() {
  echo "BLOCKED by .claude/hooks/guard.sh: $1." >&2
  echo "This belongs to the human operator (CLAUDE.md never-do list). Claude must not run it." >&2
  exit 2
}
re() { printf '%s' "$cmd" | grep -Eq "$1"; }

# 1. Live-mode gate flip / live env — match the ASSIGNMENT form (with `=`), the
#    only way to actually flip the gate. A bare token mention (no `=`, e.g.
#    "`ALPHA_ALLOW_LIVE`" in a doc string) is allowed; token-bearing *text* belongs
#    in a file via the Write tool / `--body-file`, not in an inline Bash arg.
re 'ALPHA_ALLOW_LIVE=' && block "flips the live-mode gate (ALPHA_ALLOW_LIVE=)"
re 'ALPHA_ENV=live(\b|$)' && block "starts a live environment (ALPHA_ENV=live)"

# 2. Real-venue live runner: `alpha run ... --adapter(s) <real venue>` in one
#    segment. Requires the venue to sit in the --adapter argument, so prose like
#    "alpha run (kite/delta)" without --adapter is allowed.
re 'alpha[[:space:]]+run[^;&|]*--adapters?[[:space:]=]+[^;&|]*\b(kite|delta|binance|bybit|dhan|upstox)\b' \
  && block "starts a live broker runner (alpha run against a real venue)"

# 3. Irreversible git history / working-tree destruction.
re '\bgit[[:space:]]+push\b[^;&|]*(--force|--force-with-lease|[[:space:]]-f(\b|$))' \
  && block "force-push / history rewrite"
re '\bgit[[:space:]]+reset[[:space:]]+--hard\b' && block "git reset --hard (discards work irreversibly)"
re '\bgit[[:space:]]+filter-branch\b'           && block "git filter-branch (history rewrite)"
re '\bgit[[:space:]]+rebase\b'                   && block "git rebase (history rewrite)"
re '\bgit[[:space:]]+clean[[:space:]]+-[a-z]*f'  && block "git clean -f (permanently deletes untracked files)"

exit 0
