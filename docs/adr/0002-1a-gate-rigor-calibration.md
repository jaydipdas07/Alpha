# ADR 0002 — 1a.GATE: the rigor crown-jewel is trusted (the four conditions + the calibrated gate)

- Status: **Accepted** — ratified by [You] (Jaydip Das) at 1a.GATE on 2026-06-26
- Date: 2026-06-26
- Deciders: Jaydip Das ([You]) / Claude Code
- Depends on: ADR 0001 (engine), `docs/DESIGN_v4.md` (R4/R5/R6/R14), `BUILD_PLAN.md`

## Context

Phase 1a built the **rigor crown-jewel** — the honest-results gate that decides which
strategies survive before any money or AI-generation is trusted (R14: *trust the filter
before automating it*). The whole `[CC]` build landed under the PR norm (B1a.1–B1a.9,
500 tests, ≥94% cov, every new rigor/research module 100%): the Parquet/DuckDB cold
store + ingest, perp funding (R13), the lifted look-ahead/walk-forward/stress rigor +
portfolio funding, **CPCV + embargo + PBO**, the **Deflated Sharpe Ratio + keyed trial
ledger** (R4), the **no-ACL roll-forward holdout** (R5/R6 → TEST-3), the **reproducibility
ledger** (R5/R7), the **vectorbt pre-screen + budgeted/carried CPCV queue** (R7), and the
**population-calibration harness** (R14). 1a.GATE ratifies that this gate can be trusted.

## Decision

**1a.GATE is ratified.** The four pre-agreed conditions are built and verified by their
own suites:

1. **Error-rate thresholds met (R14).** Against synthetic control populations, the gate's
   measured false-promote is **0.0 ≤ 5%** and false-reject **0.056 ≤ 25%** (mean over a
   20-seed sweep) — at the calibrated operating point below. (`research/calibration.py`, #45.)
2. **TEST-3 — holdout/ledger unreadable by agents via any tool.** Holdout bars are absent
   from the cold store and live only in a physically separate no-ACL store at a disjoint
   root; proven holdout-free via every cold-store read path. (`data/holdout.py`, #38.)
3. **DSR cell-invariance (R4).** The Deflated-Sharpe multiple-testing penalty is bit-for-bit
   invariant to trials run in any unrelated `(market, family, window)` cell. (#36.)
4. **Reproducibility (R5/R7).** A backtest is bit-for-bit reproducible from its provenance
   record; re-recording the same inputs with a different result is a detectable violation. (#40.)

**The calibrated operating point — a deliberate, [You]-approved trade.** The gate (DSR
deflated by the cumulative trial count, R4) is **conservative by design**. A review caught
that the first calibration was *rigged* — it certified false-reject against an implausibly
strong edge (Sharpe ~8), giving false confidence. The honest power curve showed the gate
at its original strictness (`dsr.threshold = 0.95`) **could not** meet the ≤25% false-reject
target for a realistic edge. [You] chose to **loosen the gate**:

- `dsr.threshold` **0.95 → 0.92** — the *high/safe* end of the feasible `[0.90, 0.92]`
  window (at 0.90 the worst noise candidate, DSR 0.909, slips through; at 0.92 false-promote
  stays **0**). This is now the **production** significance bar the live DSR gate will consume.
- Calibrated at a **realistic strong edge** — annualized Sharpe ~2.4 over a ~5-year daily
  out-of-sample horizon.

**Accepted caveat:** the gate **certifies STRONG edges only** (Sharpe ~2.4+). Weaker-but-real
edges (Sharpe ~1–2) are rejected 48–92% of the time. This is correct multiple-testing
statistics and matches the agreed asymmetry — **promoting noise is the dangerous error (held
to 0); missing a weak edge is the cheaper one** — and the downstream holdout + paper-trading +
human-approval gates remain the real safety, so the discovery filter is allowed to be strict.

## Consequences

- **Phase 1b is open** — AI discovery (the constrained `strategist` + `quant-analyst` agents
  + Workflow A `discovery-cycle`), pure `[CC]`, on the research box. The deferred
  `strategy/registry` + the ~7 Vega example strategies (the strategist's seed templates)
  rejoin here.
- `dsr.threshold = 0.92` is the live significance bar; revisit it (or the strong-edge floor /
  OOS horizon) if real candidates show the conservative gate discards too much genuine edge.
- The calibration is a **single-component (DSR-on-OOS)** calibration; its false-reject is a
  *lower bound* on the composed gate's (adding pre-screen + CPCV/PBO + holdout only rejects
  more). Re-calibrate the composed pipeline when the discovery loop wires the stages together.
