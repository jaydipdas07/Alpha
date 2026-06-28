# Lemma token relay (pod-sync token persistence)

The worker↔pod telemetry token (`LEMMA_TOKEN`) is a ~60-min access token, and the worker reads it
only at startup. Without help, a long-lived worker loses pod-sync after the token expires (the
cockpit stops seeing it; commands stop being polled) — best-effort, so trading/safety are never
affected (TEST-8), but the cockpit goes blind. This relay keeps it fresh, in two halves:

- **Mac side (here):** a launchd LaunchAgent runs `lemma_token_relay.sh` every 3 min →
  `lemma auth print-token` (the Mac CLI session auto-refreshes) → writes the token to the box's
  `~/alpha/.env` `LEMMA_TOKEN` over ssh. The value is never printed/committed.
- **Worker side (code):** the worker re-reads `LEMMA_TOKEN` from `.env` on a timer
  (`config/<env>.yaml` → `pod_sync.token_refresh_seconds`, default 60s) and swaps in a
  freshly-tokened pod client **without a restart** (`Worker._pod_token_refresh_loop`).

> Why the relay can't just push a 60-min token and rest: `print-token` returns the *current* session
> token at its current age (it only re-mints when expired), so a push may carry anywhere from ~0 to
> 60 min of life. Pushing every 3 min keeps the box's token recent; the worker's 60s re-read picks
> up each change. Brief, self-healing staleness is possible right when the session token rolls over.

## Install (on the Mac — this is the operator's machine)

```bash
mkdir -p ~/Library/LaunchAgents
cp deploy/relay/com.alpha.lemma-token-relay.plist ~/Library/LaunchAgents/
launchctl load   ~/Library/LaunchAgents/com.alpha.lemma-token-relay.plist   # RunAtLoad fires once now
```

The plist hard-codes this Mac's paths (repo at `/Users/jaydipdas/Code/Alpha`, lemma at
`~/.local/bin/lemma`, ssh key `~/.ssh/vega-key.pem`, box `ubuntu@3.6.176.133`). Edit the script's
env-var defaults or the plist if any move. The box `.env` path is fixed at `~/alpha/.env`.

## Verify

```bash
tail -f ~/Library/Logs/alpha-lemma-token-relay.log        # "ok: pushed a fresh token (NNNN chars)"
bash deploy/relay/lemma_token_relay.sh                     # run once by hand; same line, no token value
# on the box: the worker should log a swap when the token changes —
ssh -i ~/.ssh/vega-key.pem ubuntu@3.6.176.133 'journalctl -u alpha-worker -n50 | grep pod_token_refreshed'
```

A healthy steady state: the box `worker_status` heartbeat on the pod stays fresh (< ~1 min old) and
`alpha-worker` logs no sustained `worker_status_beat_failed [401]`.

## Remove / pause

```bash
launchctl unload ~/Library/LaunchAgents/com.alpha.lemma-token-relay.plist
rm ~/Library/LaunchAgents/com.alpha.lemma-token-relay.plist                 # to remove entirely
```

Pausing the relay is safe anytime — the worker keeps trading; only cockpit telemetry goes stale
until a token is re-staged (manually or by re-loading the relay).
