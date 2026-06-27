# config/ — the single source of truth for tunables

**No magic numbers in code** (CLAUDE.md). Every business parameter lives here, validated by
`pydantic-settings`, loaded per environment. Tweaking a metric must mean editing **one config
value**; any PR that hard-codes a tunable is rejected.

Files land with the task that first needs them:

| File | What | Lands in |
|---|---|---|
| `risk.yaml` | all risk limits (the most-tuned file) | Phase 1a+ |
| `costs.yaml` | fees, taxes (Indian stack + crypto TDS), slippage, perp funding | B1a.2 |
| `rigor.yaml` | CPCV embargo, DSR thresholds, holdout cadence, population-calibration defs | Phase 1a |
| `discovery.yaml` | the discovery universe — cells `(market, family, window)` + `n_candidates` | B1b.3 (#62) |
| `research.yaml` | research-box heartbeat + backtest lease/timeout (R8) | B0.6 |
| `portfolio.yaml` | pure allocator — scored signals → target weights (D6) | Phase 1a |
| `instruments.yaml` | tradable universe, lot/tick sizes | Phase 1+ |
| `venues.yaml` | adapter registry (Delta / Binance / Kite / Dhan / Upstox) | Phase 1+ |
| `strategies/<name>.yaml` | per-strategy parameters | B0.7+ |
| `<env>.yaml` (`dev` / `paper` / `live`) | infra: active adapters, DB, log level, the live gate | Phase 0+ |

Most files now exist (Phases 0–1b). Still to land: `venues.yaml` + the `<env>.yaml` infra files
(active adapters / DB / the live gate), which arrive with the worker + live layers (Phase 3+). Each
file appears as its consumer lands.
