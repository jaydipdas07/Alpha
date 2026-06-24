# .claude/hooks

Session hooks wired in `.claude/settings.json`.

- **`guard.sh`** — `PreToolUse(Bash)`. Defense-in-depth behind `permissions.deny`: blocks (exit 2)
  any command that would flip the live gate (`ALPHA_ALLOW_LIVE=`, `ALPHA_ENV=live`), start a
  real-venue runner (`alpha run --adapter <kite|delta|binance|bybit|dhan|upstox>`), or rewrite git
  history (force-push, `reset --hard`, `rebase`, `filter-branch`, `clean -f`). Execution-context
  aware — *mentioning* a token in a doc/PR body/grep is allowed. Fails open on parse error.
- **`ruff-touched.sh`** — `PostToolUse(Edit|Write|MultiEdit)`. Advisory `ruff check` on a touched
  `.py` file; report-only, never blocks, never `--fix`. pre-commit + CI remain the enforcing gate.

Both require `jq`. They mirror the Vega harness; keep them in sync with the `CLAUDE.md` never-do list.
