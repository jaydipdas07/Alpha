# Research host (AWS box) — operations (B0.5)

The Phase-1 research/backtest host: an AWS EC2 instance repurposed from Vega (B2/B0.5).
Mac-CLI-only (it needs `~/.ssh/vega-key.pem`); a cloud/web session can't reach it.

## Box
- **Instance:** `vega-bot` · `i-0d577eb3e62f7bdb6` · `t4g.small` (arm64/Graviton) · `ap-south-1`.
- **Address:** Elastic IP **`3.6.176.133`** (stable — `vega-eip`, does not rotate); public DNS
  `ec2-3-6-176-133.ap-south-1.compute.amazonaws.com`. (Private `172.31.29.232` is VPC-internal only.)
- **Access:** `ssh -i ~/.ssh/vega-key.pem ubuntu@3.6.176.133`. Security-group inbound SSH:22 is open to
  the operator's Mac IP (residential — re-add if it changes).
- **Toolchain (pre-existing):** `uv 0.11.21` + Python `3.12.3` at `~/.local/bin/uv`. Disk 24G free.

## Alpha research host — `~/alpha`
A clean alpha-core tree, rsync'd from the Mac with **no `.env`/secrets — nothing live**. Verified:
`uv sync --all-packages --all-groups` + `uv run pytest` green on linux-aarch64 (40 tests).

Re-deploy (Mac → box):
```bash
rsync -az --delete -e "ssh -i ~/.ssh/vega-key.pem" \
  --exclude=.git --exclude=.venv --exclude=.env --exclude='.env.*' --exclude=secrets \
  --exclude='*.pem' --exclude='*.key' --exclude=.ruff_cache --exclude=.mypy_cache \
  --exclude=.pytest_cache --exclude=.hypothesis --exclude=__pycache__ --exclude=.coverage \
  /Users/jaydipdas/Code/Alpha/ ubuntu@3.6.176.133:~/alpha/
ssh -i ~/.ssh/vega-key.pem ubuntu@3.6.176.133 'cd ~/alpha && ~/.local/bin/uv sync --all-packages --all-groups'
```
(Git-based deploy + a research_status heartbeat unit are later refinements; the lease/timeout logic is
`alpha_core/research/lease.py`, B0.6.)

## Vega — dormant, NOT deleted (operator chose "just stop"; fully reversible)
- **Service:** stopped + disabled (`sudo systemctl stop/disable vega`). Revert: `sudo systemctl enable --now vega`.
- **Docker stack:** containers stopped but kept — `vega-postgres`, `alloy`, `promtail`.
  Revert: `docker start vega-postgres alloy promtail`.
- `~/vega/` + its data/volumes are intact. The Mac `/Users/jaydipdas/Code/Vega` remains the Track-B lift source.
- **Secrets preserved (B3):** the box's `.env` / `.env.enc` / `live.env` were copied to the Mac's
  gitignored `secrets/aws-box/` (never committed/logged).

## Caveat (Phase 4)
SEBI live execution needs a static *registered* IP — the Elastic IP `3.6.176.133` is a candidate;
confirm with the broker (R2) before relying on it for live. Until then this box stays research/paper.
