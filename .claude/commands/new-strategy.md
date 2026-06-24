---
description: Scaffold a new Strategy (config + class + registry + backtest) honoring the invariants
argument-hint: "<strategy_name> (snake_case)"
allowed-tools: Bash(grep*), Bash(ls*), Read, Edit, Write, Bash(uv run alpha backtest*), Bash(uv run ruff*), Bash(uv run mypy*), Bash(uv run pytest*)
---

Scaffold a new strategy named **`$1`** end-to-end, following the existing patterns exactly. A strategy
is an edge candidate wired into the machine — it must clear the rigor gate before it means anything.

> **Availability:** the strategy layer lands in **Phase 1** (the portable contract in `B0.7`; the
> lifted engine + example templates in `B0.9` / `B1b.1`, seeded from Vega's ~7 example strategies).
> Until `alpha-core/strategy/` exists, scaffold against the contract in `B0.7` and skip the backtest step.

## Study the pattern first (do not invent)

- Existing example strategies (templates): !`ls alpha-core/src/alpha_core/strategy/examples/ 2>/dev/null || echo "(not built yet — see B0.9 / B1b.1)"`
- The registry it must join: `alpha-core/src/alpha_core/strategy/registry.py`
- A config template: !`ls config/strategies/ 2>/dev/null || echo "(none yet)"`

## Build `$1`

1. **Config** — `config/strategies/$1.yaml` with every tunable. **No magic numbers in code**: every
   parameter the strategy reads lives here.
2. **Strategy class** — `alpha-core/src/alpha_core/strategy/examples/$1.py` implementing the
   `(bars, params) -> signals` contract (= Vega `Strategy` ABC) with a `from_config` classmethod. It must:
   - emit only **abstract signals** — never import a broker SDK (venue-agnostic);
   - never read **future** bars (no look-ahead — the rigor gate will catch it);
   - use `Decimal` for every price/quantity; tz-aware UTC; source time from the bar, never wall-clock.
3. **Register** it in `strategy/registry.py` (the one config-driven selector).
4. **Tests** — a unit test mirroring an existing example's test.

## Verify before declaring done

Run `uv run ruff check .`, `uv run mypy`, `uv run pytest -q -k $1`, then `uv run alpha backtest --rigor`
with `$1` configured. Report the rigor verdict — a fresh strategy that fails `--require-edge` is
expected; what matters is that it runs clean (no look-ahead, no errors) and **never touches the
holdout** (TEST-3). Do **not** flip any live gate or touch adapters.
