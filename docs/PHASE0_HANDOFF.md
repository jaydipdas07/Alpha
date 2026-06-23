# Alpha — Build Handoff to Mac CLI (Phase-0 readiness)

> Operational companion to [`BUILD_PLAN.md`](../BUILD_PLAN.md) (the canonical v3 plan). This file
> captures the build-environment decisions (B1–B5) and the sequenced readiness checklist so the
> Mac `lemma` CLI session — which owns the build — has a clean handoff. **No code until per-phase
> go-ahead** (paper-first). `Rn` refs point at the v3 refinements in `BUILD_PLAN.md`.

## Capability boundary

The build runs on the **Mac `lemma` CLI session**: it has the `lemma` CLI, the Vault pod default,
the Vega references, the `.env` keys, and SSH to the AWS box. A cloud/container session canNOT reach
the Vega repo, its `.env`, or the AWS machine — anything touching those is Mac-CLI-only.

## Decisions locked

| # | Item | Decision |
|---|---|---|
| B1 | Build environment | **Mac `lemma` CLI** for everything (pod + `alpha-core` + `worker`). |
| B2 | Research/compute host | **Repurpose the existing AWS machine** that runs Vega. **Wipe Vega + pre-existing stuff — but preserve the `.env` keys first** (B3). Mac-local is the fallback. |
| B3 | API keys | Reuse the keys in **Vega's `.env`**. Copy into Alpha's own **gitignored** secrets; never commit. |
| B4 | Crypto venue split | **Delta = live execution** (Indian, FIU-registered, crypto *derivatives* — matches the compliance posture). **Binance = free market data (WS/historical) + testnet paper**, not live. Both via `ccxt`. |
| B5 | Lemma money-column type | **Verify native `DECIMAL`/`NUMERIC` first; if present, use it. If absent, default to string-encoded `Decimal`** for money; keep ratio metrics (Sharpe/DSR/PBO/returns) as native numbers. Final pick is a Phase-0 verification on the CLI. |

### B5 detail — money-column type
`alpha-core` always computes in Python `Decimal`; the question is only how money is *stored in Lemma*
(floats banned). Options:
- **Native `DECIMAL`/`NUMERIC`** — exact *and* SQL can sort/aggregate. **Strictly best; use if available.**
- **String-encoded `Decimal`** (`"1234.56"`) — lossless, no scale bookkeeping, round-trips `Decimal`.
  Cost: SQL can't order/aggregate numerically (`"9" > "10"` lexically), so money sums happen
  client-side (cockpit JS) or in `alpha-core`. Fits Alpha because the pod is a store/display layer and
  P&L is a **derived fold computed upstream** (R11), pushed as snapshots — Lemma rarely does money math.
- **Integer minor-units** (paise) — SQL math works, exact, **but** one scale is fragile across assets
  (INR equity 2 dp vs crypto 6–8 dp price *and* qty). Only worth it for a column that must aggregate in SQL.

**Recommendation:** native DECIMAL if Lemma has it; else string-encoded Decimal as default, ratios as
native numbers; reserve integer-minor-units for any single column needing in-SQL money aggregation.

## AWS-machine repurpose — caveats (B2)
- **Before wiping:** copy Vega's `.env` (B3) + anything else worth keeping; confirm nothing else on the
  box is needed.
- Box serves as **research/backtest host (Phase 1)** and can host the **paper worker (Phase 3)**.
- **Live (Phase 4) caveat:** SEBI live execution needs a **static/registered IP** — confirm this
  instance's IP can be the SEBI-registered (ideally elastic) IP (R2) before relying on it for live;
  otherwise it stays research/paper and live runs on the registered-IP VPS.
- Op-note: keep it awake during the nightly discovery window; give the research role a heartbeat
  (`research_status`) + backtest timeout/lease so workflows don't hang (R8).

## Readiness tiers

**Tier 1 — gates Phase 0 + Phase 1a** (resolved by the decisions above; only verifications remain)
1. Build env = Mac CLI ✔ (B1). 2. Host = AWS box, wiped, keys preserved ✔ (B2/B3).
3. Data: Kite ₹500 historical sub + key; Binance data key; NSE bhavcopy + Dukascopy (free).
   Crypto live = Delta ✔ (B4). 4. Money-column type: verify on CLI, then apply B5.
5. Verify `lemma` resolves to Vault (`019ef606-7b77-76f1-853a-978ddf819415`).

**Tier 2 — gates Phase 1a calibration:** DSR search-cell taxonomy (R4); holdout roll-forward cadence
+ size (R5); synthetic-population defs + Phase-1b error-rate thresholds (R14); CPCV embargo + DSR
threshold + per-night compute target (R7).

**Tier 3 — gates Phase 3 (paper worker):** Telegram bot token; Delta + Binance testnet keys; Kite
live-feed paper (R9, not replay); daily Kite token-relay (fail-closed + on-demand Telegram reauth,
R12); `/knowledge` RAG seeds.

**Tier 4 — gates Phase 4 (live, staged) — start long-lead items early:** SEBI algo registration +
static/elastic registered IP (R2); live broker keys on the worker only; VPS + ClickHouse + deadman
process (R1/D1); capital-staging tolerance + session count; CA sign-off.

## First moves on the Mac CLI (when Tier 1 is green)
1. `cd` into the Alpha repo on the Mac; `git pull` (v3 `BUILD_PLAN.md` + this doc).
2. Run **`/ultraplan`** against `BUILD_PLAN.md` to generate the execution plan (lemma-builder skill there).
3. **Phase-0 verification:** inspect Lemma numeric column types → lock B5; confirm `lemma` → Vault;
   verify table/RLS/FK primitives.
4. **AWS prep:** SSH in, preserve `.env`, wipe Vega, set up the research/backtest host.
5. Scaffold `alpha-core/` + `worker/` + `pod/` (pod.json + table DDL + folders + seed) + `docs/`.
