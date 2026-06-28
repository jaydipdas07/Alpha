#!/bin/bash
# Mac → box Lemma token relay (token-persistence, deploy/relay).
#
# The pod-sync access token is ~60-min and the worker reads it only at startup, so a long-lived
# worker would lose pod-sync. This relay (driven by launchd every few minutes) mints a fresh token
# on the Mac — where the `lemma` CLI session auto-refreshes — and writes it to the worker box's
# gitignored ~/alpha/.env as LEMMA_TOKEN. The box worker re-reads that file on a timer
# (PodSyncConfig.token_refresh_seconds) and swaps in a freshly-tokened pod client WITHOUT a restart,
# so the cockpit keeps seeing the worker live. Pod-sync is best-effort (TEST-8): this never touches
# trading or safety — at worst the cockpit heartbeat is briefly stale.
#
# The token VALUE is NEVER printed or logged (only its length). Tunables via env (with defaults):
#   LEMMA_BIN          the Mac lemma CLI          (default: ~/.local/bin/lemma)
#   ALPHA_WORKER_HOST  ssh target                 (default: ubuntu@3.6.176.133)
#   ALPHA_WORKER_KEY   ssh key                    (default: ~/.ssh/vega-key.pem)
# The box .env path is fixed at ~/alpha/.env (the worker's WorkingDirectory).
set -euo pipefail

ts() { date -u +%FT%TZ; }
LEMMA_BIN="${LEMMA_BIN:-$HOME/.local/bin/lemma}"
BOX="${ALPHA_WORKER_HOST:-ubuntu@3.6.176.133}"
KEY="${ALPHA_WORKER_KEY:-$HOME/.ssh/vega-key.pem}"

token="$("$LEMMA_BIN" auth print-token 2>/dev/null | tr -d '\r\n' || true)"
if [ "${#token}" -lt 100 ]; then
  echo "$(ts) ERROR: print-token returned ${#token} chars (not a token) — not pushing"
  exit 1
fi

# Pipe the token over ssh stdin (never an argv/ps exposure); rewrite only the LEMMA_TOKEN line.
printf '%s' "$token" | ssh -i "$KEY" -o ConnectTimeout=15 -o BatchMode=yes "$BOX" '
  IFS= read -r TOK
  cd ~/alpha || exit 1
  grep -v "^LEMMA_TOKEN=" .env > .env.new 2>/dev/null || true
  printf "LEMMA_TOKEN=%s\n" "$TOK" >> .env.new
  chmod 600 .env.new && mv .env.new .env
  unset TOK
'
echo "$(ts) ok: pushed a fresh token (${#token} chars) to $BOX:~/alpha/.env"
