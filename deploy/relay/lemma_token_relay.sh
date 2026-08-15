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
# The token VALUE is NEVER printed or logged (only its length). Config via env — the box address
# and key are deliberately not defaulted here (public-repo hygiene; they live in the gitignored
# .env, see docs/research-host.md):
#   LEMMA_BIN          the Mac lemma CLI          (default: ~/.local/bin/lemma)
#   ALPHA_WORKER_HOST  ssh target                 (required, e.g. ubuntu@<elastic-ip>)
#   ALPHA_WORKER_KEY   ssh key                    (required, the .pem path)
# The box .env path is fixed at ~/alpha/.env (the worker's WorkingDirectory).
set -euo pipefail

ts() { date -u +%FT%TZ; }
LEMMA_BIN="${LEMMA_BIN:-$HOME/.local/bin/lemma}"
BOX="${ALPHA_WORKER_HOST:?set ALPHA_WORKER_HOST (ubuntu@<elastic-ip>) — see docs/research-host.md}"
KEY="${ALPHA_WORKER_KEY:?set ALPHA_WORKER_KEY (the ssh .pem path) — see docs/research-host.md}"

token="$("$LEMMA_BIN" auth print-token 2>/dev/null | tr -d '\r\n' || true)"
if [ "${#token}" -lt 100 ]; then
  echo "$(ts) ERROR: print-token returned ${#token} chars (not a token) — not pushing"
  exit 1
fi

# Pipe the token over ssh stdin (never an argv/ps exposure); rewrite ONLY the LEMMA_TOKEN line.
# The box .env holds the broker secret on the sole executor, so the rewrite is guarded: it refuses
# to create a missing .env (would mask a deploy problem) and refuses to promote a .env.new that is
# empty or has FEWER lines than the original (a partial/truncated write — e.g. disk-full) — which
# would otherwise silently drop the broker keys and break venue auth on the next worker restart. The
# mv is atomic, so an ssh interruption leaves .env fully old or fully new, never half-written.
printf '%s' "$token" | ssh -i "$KEY" -o ConnectTimeout=15 -o BatchMode=yes "$BOX" '
  IFS= read -r TOK
  cd ~/alpha || exit 1
  if [ ! -f .env ]; then echo "ERROR: ~/alpha/.env missing — refusing to create a token-only file"; exit 1; fi
  before=$(grep -c "" .env)
  grep -v "^LEMMA_TOKEN=" .env > .env.new
  printf "LEMMA_TOKEN=%s\n" "$TOK" >> .env.new
  after=$(grep -c "" .env.new)
  if [ -s .env.new ] && [ "$after" -ge "$before" ]; then
    chmod 600 .env.new && mv .env.new .env
  else
    rm -f .env.new
    echo "ERROR: .env.new ($after lines) < .env ($before) — refusing to promote a truncated file"
    exit 1
  fi
  unset TOK
'
echo "$(ts) ok: pushed a fresh token (${#token} chars) to $BOX:~/alpha/.env"
