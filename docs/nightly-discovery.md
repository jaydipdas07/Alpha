# B1b.4 — nightly discovery + `/knowledge` RAG (deploy guide)

The portable nightly discovery run (B1b.4a) is built and tested in `alpha-core`. This guide is the
**operator deploy layer** for the two outward-facing halves — both touch the research box / the live
Vault pod, so they are deployed by hand, not by CI.

- **B1b.4b — the `nightly-discovery` schedule:** a research-box **systemd timer** fires the run.
- **B1b.4c — the `/knowledge` RAG:** a strategy-knowledge doc uploaded to the pod as a File.

> **Why a research-box cron, not a pod-triggered function.** The cold store is local Parquet on the
> research box (`data_cold/`), not in the pod — a Lemma function can't read it. So the box runs
> discovery on its own schedule and the pod only *receives the results* (synced from the per-run JSON
> records). The pod→box command path (a schedule that signals the box) lands later with the worker
> command-poll (Phase 3, M3.6); until then the box's own timer is the schedule.

## B1b.4b — the schedule (research box, AWS)

Prereqs on the box (`ubuntu@<elastic-ip>`, repo at `~/alpha` — see `docs/research-host.md`):

1. `uv` installed and the repo synced: `cd ~/alpha && uv sync --all-packages --all-groups`.
2. The cold store ingested on the box: `uv run python scripts/ingest_cold_store.py` (writes
   `~/alpha/data_cold/`). The nightly run only promotes on a real edge, which needs a richer window
   than the B1a.1b seed (289 BTC 5m + 21 RELIANCE daily bars) — extend the ingest before expecting
   survivors.

Deploy the timer (units are in `deploy/systemd/`; adjust `User` / `WorkingDirectory` / the `uv` path
if the box differs from the defaults):

```bash
sudo cp ~/alpha/deploy/systemd/nightly-discovery.service /etc/systemd/system/
sudo cp ~/alpha/deploy/systemd/nightly-discovery.timer   /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nightly-discovery.timer
```

Verify (this is the **Done-when**: *the schedule fires a discovery run*):

```bash
systemctl list-timers nightly-discovery.timer      # shows the next fire time
sudo systemctl start nightly-discovery.service     # fire one run now
journalctl -u nightly-discovery.service -n 40      # "cycles run: N | survivors: ... | quarantined: ..."
ls -t ~/alpha/discovery_runs/                       # a <timestamp>.json record per run
```

Each run writes `discovery_runs/<UTC-timestamp>.json` — a per-cycle record shaped to the pod
`discovery_runs` table (`market` / `family` / `window` / `trial_count` / `survivors` + a `detail`
blob, plus the quarantined cells). The records are gitignored (research-plane artifacts).

### Sync runs to the pod `discovery_runs` table (optional, for the cockpit)

So the Phase-2 cockpit can show discovery runs (incl. **rejected** candidates), sync each JSON
record's `reports[]` to the pod (one row per cycle). This touches the **live pod**, so it's an
operator step:

```bash
# illustrative — one row per cycle from the latest record:
lemma records create discovery_runs \
  --field status=complete --field market=<crypto|equity> --field family=<template> \
  --field window=<window> --field trial_count=<n> --field survivors=<n> \
  --field detail='<the cycle JSON>'
```

A small sync helper (read the newest `discovery_runs/*.json` → `lemma records create` per cycle) is
a clean follow-up once the cockpit needs live rows. Each `reports[]` entry maps straight onto the
table columns — `market` (already lowercased to the ENUM) / `family` / `window` / `trial_count` /
`survivors`, with `promoted` → the `detail` JSON and `status` = `complete`; each `quarantined[]`
entry is a `status=failed` row.

## B1b.4c — the `/knowledge` RAG (live pod)

The strategy-knowledge corpus is `docs/knowledge/strategies.md` (the R7 seed templates, their
economic rationale, and the rigor gate). Upload it as a pod **File** so agents/the cockpit can
answer strategy questions via `files search` — **without** any holdout/ledger access (TEST-3/R6:
`/knowledge` is a no-ACL-read corpus, never the holdout).

```bash
lemma files upload docs/knowledge/strategies.md --label "/knowledge: strategy templates"
# wait for conversion to COMPLETED, then verify (the Done-when: RAG answers a strategy-doc query):
lemma files search "what is the VWAP reversion strategy and when does it enter?"
lemma files search "how does the rigor gate decide promote vs reject?"
```

Expect the search to return passages from `strategies.md`. Add more corpus files (e.g. cost-model
notes) the same way; keep design/build docs (DESIGN_v4, ADRs) out of `/knowledge` unless you want the
RAG to answer "how Alpha is built" as well as "what the strategies are".
