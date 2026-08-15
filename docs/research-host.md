# Research host (AWS box) — operations (B0.5)

The Phase-1 research/backtest host: an AWS EC2 instance repurposed from Vega (B2/B0.5).
Mac-CLI-only (it needs the operator's ssh key); a cloud/web session can't reach it.

**The box's concrete address and key are deliberately NOT in the repo** (public-repo hygiene): they
live in the gitignored `.env` as `ALPHA_WORKER_HOST` (e.g. `ubuntu@<elastic-ip>`) and
`ALPHA_WORKER_KEY` (the `.pem` path). Export them before running the commands below —
`set -a; source .env; set +a` — or inline them. Never commit either value.

## Box
- **Instance:** `t4g.small` (arm64/Graviton) · `ap-south-1`.
- **Address:** a stable Elastic IP (does not rotate) → `ALPHA_WORKER_HOST`.
- **Access:** `ssh -i "$ALPHA_WORKER_KEY" "$ALPHA_WORKER_HOST"`. Security-group inbound SSH:22 is
  open to the operator's Mac IP only (residential — re-add if it changes).
- **Toolchain (pre-existing):** `uv 0.11.21` + Python `3.12.3` at `~/.local/bin/uv`. Disk 24G free.

## Alpha research host — `~/alpha`
A clean alpha-core tree, rsync'd from the Mac with **no `.env`/secrets — nothing live**. Verified:
`uv sync --all-packages --all-groups` + `uv run pytest` green on linux-aarch64 (40 tests).

Re-deploy (Mac → box, from the repo root):
```bash
rsync -az --delete -e "ssh -i $ALPHA_WORKER_KEY" \
  --exclude=.git --exclude=.venv --exclude=.env --exclude='.env.*' --exclude=secrets \
  --exclude='*.pem' --exclude='*.key' --exclude=.ruff_cache --exclude=.mypy_cache \
  --exclude=.pytest_cache --exclude=.hypothesis --exclude=__pycache__ --exclude=.coverage \
  ./ "$ALPHA_WORKER_HOST":~/alpha/
ssh -i "$ALPHA_WORKER_KEY" "$ALPHA_WORKER_HOST" 'cd ~/alpha && ~/.local/bin/uv sync --all-packages --all-groups'
```
(Git-based deploy + a research_status heartbeat unit are later refinements; the lease/timeout logic is
`alpha_core/research/lease.py`, B0.6.)

## Vega — dormant, NOT deleted (operator chose "just stop"; fully reversible)
- **Service:** stopped + disabled (`sudo systemctl stop/disable vega`). Revert: `sudo systemctl enable --now vega`.
- **Docker stack:** containers stopped but kept — `vega-postgres`, `alloy`, `promtail`.
  Revert: `docker start vega-postgres alloy promtail`.
- `~/vega/` + its data/volumes are intact. The Mac `~/Code/Vega` remains the Track-B lift source.
- **Secrets preserved (B3):** the box's `.env` / `.env.enc` / `live.env` were copied to the Mac's
  gitignored `secrets/aws-box/` (never committed/logged).

## Caveat (Phase 4)
SEBI live execution needs a static *registered* IP — the box's Elastic IP is a candidate; confirm
with the broker (R2) before relying on it for live. Until then this box stays research/paper.
