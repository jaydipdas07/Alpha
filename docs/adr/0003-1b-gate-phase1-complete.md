# ADR 0003 — 1b.GATE / Phase 1 complete: the discovery machine is built; the real-edge demonstration is deferred to vendor data (M3.0)

- Status: **Accepted** — operator-directed Phase-1 closure by [You] (Jaydip Das) on 2026-06-27
- Date: 2026-06-27
- Deciders: Jaydip Das ([You]) / Claude Code
- Depends on: ADR 0001 (engine → Vega lift), ADR 0002 (1a.GATE calibration), `docs/DESIGN_v4.md`, `TASKS.md`

## Context

Phase 1b built the **AI discovery machine** (pure `[CC]`, under the PR norm): the `strategist`
(parameterizes vetted R7 templates, holdout-isolated → TEST-3), the `quant-analyst` (composes the
rigor gate → promote/reject/revise), the `discovery-cycle` (`run_discovery_cycle`: propose →
real-engine backtest → DSR-vs-trial-count → assess → survivors), the **real backtester**
(`EngineBacktester` on the lifted engine, `now` injected from bar-time), the **cold-store `bars_for`
adapter** (in-sample-only; TEST-3 holdout-denial structural at the store boundary), the **multi-series
seal** (each series' own rolled-forward holdout; the nightly reads the holdout-free research store),
and the **portable nightly run + research-box cron + `/knowledge` RAG** — deployed and verified on the
research box (timer live; a manual sweep ran 30 cycles / 0 quarantine / 0 promote on real sealed free
data; `/knowledge` searchable on the pod).

1b.GATE asked for *"an AI-proposed strategy passes the full gate end-to-end on the research box."* On
the **free** data the build used (Binance BTC + Yahoo NIFTY — large, liquid, efficient markets), the
machine correctly **promotes nothing**: there is no Sharpe-~2.4 edge for the seed templates to find,
and the 1a-calibrated gate is conservative by design (ADR 0002). Demonstrating a real, *deployable*
promotion needs market data with a genuine edge — which the operator will provide from a data
**vendor** (Tardis/Velo for crypto; a broker/vendor source for the Indian leg) when active backtesting
begins. That is naturally the on-ramp to paper-trading, not part of building the machine.

## Decision

**Phase 1 is complete, on machine-validation.** 1b.GATE is reframed and satisfied: *the discovery
machine runs end-to-end on real cold-store data (propose → backtest → full rigor gate →
promote/reject), validated on free data.* The evidence:

1. **It promotes genuine edges and rejects overfits/noise** — the B1b.2 / B1b.3 contract tests
   promote planted edges and reject planted overfits, and the B1a.9 population calibration measures
   false-promote 0 / false-reject 0.056 at the operating point (ADR 0002).
2. **It runs clean end-to-end on real data** — a 30-cycle sweep over the sealed 6-series cold store
   completed with 0 quarantine and 0 (correct) promotions.
3. **It is deployed** — the `nightly-discovery` timer fires the run on the research box; the
   `/knowledge` corpus is searchable on the pod (B1b.4).

**The real-edge demonstration is deferred to M3.0 (Phase 3).** Showing that the machine finds a real,
deployable edge on operator-provided **vendor** data — the original spirit of "passes the gate
end-to-end" — moves to **M3.0**, the real-data on-ramp at the start of paper-trading. [You] provides
the data source/keys; [CC] builds the vendor ingest adapters (the `data/ingest` seam — the existing
free Binance/Yahoo adapters are the pattern) and runs discovery; a genuine survivor then feeds
Workflow B (M3.7).

This is an **explicit, transparent deferral, not a faked result** — the machine is proven; the thing
not yet shown (a real promotion) is honestly gated on data that does not exist in the build yet, and
is recorded as a concrete future task (M3.0).

## Consequences

- **Phase 2 (the cockpit) is next** — the visible hero (dashboard + copilot + Telegram).
- **M3.0** is added to Phase 3: data-vendor ingest adapters (Tardis/Velo + the Indian-leg source) +
  the deferred real-edge demonstration. **Data sources are distinct from execution brokers** — they
  plug into `data/ingest`, not the venue/execution layer.
- The discovery nightly **self-fires at 02:00 UTC** on the research box on free data until vendor data
  arrives; each per-run JSON record shapes to the pod `discovery_runs` table.
- `dsr.threshold = 0.92` (ADR 0002) remains the live significance bar; revisit it with the real
  candidates M3.0 surfaces if the conservative gate discards too much genuine edge.
- The carried `[You]+[CC]` gate refinements (cell-PBO-as-confirmatory redesign; cumulative DSR
  deflation variance) remain open and touch the calibrated gate — neither blocks Phase 2.
