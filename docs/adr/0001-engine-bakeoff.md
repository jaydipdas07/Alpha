# ADR 0001 — Engine bake-off: Track A (NautilusTrader) vs Track B (lifted Vega)

- Status: **Proposed** — recommendation below; **0.GATE ratification is [You]'s call**
- Date: 2026-06-26
- Deciders: Jaydip Das ([You]) / Claude Code
- Depends on: `docs/DESIGN_v4.md` (E1), `BUILD_PLAN.md`

## Context

Phase 0 ran a **co-equal A/B bake-off** (E1) to fix the execution engine before any
strategy/risk feature builds on it: **Track A** wraps the portable B0.7 contract in a
**NautilusTrader** shell; **Track B** lifts the operator's own proven **Vega** engine
wholesale into `alpha_core`. Both ran the *same* contract (`MaCrossover`) and the
*same* cost kernel (`alpha_core.execution.costs.CostModel`). The engine is chosen here
on the three pre-agreed criteria: **parity, integration friction, time-to-market.**

Evidence:
- Track B — `B0.9`: the whole engine lifted (`#14`–`#18`), a deterministic backtest of
  the contract (`#19`), and a **real Binance-testnet order placed + filled** through
  the lifted `StrategyEngine → OMS(risk) → CcxtAdapter` (`#21`/`#22`). `docs/bakeoff_b0_9f.md`.
- Track A — `B0.8`: the same contract runs through Nautilus's `BacktestEngine` with the
  Vega `CostModel` wired into a custom `FeeModel`. `track-a/` + its README.

## Criteria

### 1. Parity — TIE (both pass)
The two engines produce the **same 2 fills** (the fast/slow crossover) and **agree on
net P&L to 8 decimals**:
- Track A net **−6.32080420** USDT.
- Track B `final_pnl` **−2.72240064** (net-of-slippage, *gross*-of-fees) + its separate
  `total_fees` **3.59840355** = **−6.32080419** ≡ Track A.

Track B additionally carries the **Vega differential oracle** (the lifted property +
replay suites, now in `alpha-core/tests`) and a deterministic-on-injected-`now` backtest
(TEST-1). Parity is **not a differentiator** — both engines are correct on the same inputs.

### 2. Integration friction — Track B decisively
- **Track B is native.** The contract, OMS, risk gate, cost model, and order FSM are all
  `alpha_core` types (`Decimal`/tz-UTC `datetime`). Zero translation glue. It is the
  workspace kernel, in CI, property-tested.
- **Track A needs a shell.** A heavy Rust-backed wheel (the install *timed out* on uv's
  30s default; needed `UV_HTTP_TIMEOUT=600`); a ~40-line Cython↔`Decimal`/`datetime`
  translation layer on every bar and order (`Bar`/`Price`/`Quantity`/`Money`); a custom
  `FeeModel` to reach cost parity; and a fill-model-vs-fee-model cost-mechanism mismatch.
  It must live as a **non-workspace, non-CI island** to keep that weight off the kernel.

### 3. Time-to-market — Track B decisively
- **Track B is done.** Backtest **and** live testnet execution work today; it is the
  operator's **own, already-proven** code (`Proprietary`, owned outright) — lifted, not
  re-derived, and verified byte-identical at each step.
- **Track A is backtest-only.** Live testnet (Nautilus's live data/exec engine + Binance
  adapter) is still ahead — additional integration the lift already cleared on Track B.

## Decision (recommended)

**Adopt Track B — the lifted Vega engine in `alpha_core` — as the Alpha execution engine.**
Parity is a tie; Track B wins integration friction and time-to-market decisively and
carries the lower risk (operator-owned, proven, already live on testnet). Track A is a
*viable* engine — the bake-off proved the contract is genuinely portable — but it buys a
heavyweight dependency + a permanent translation layer for no parity gain.

> **0.GATE is a `[You]+[CC]` gate.** This ADR records the recommendation; the engine choice
> is **[You]'s to ratify**. On ratification this flips to *Status: accepted*.

## Consequences (on ratification)

- `alpha_core` is the locked Phase-0 engine foundation; Phase 1a (rigor crown-jewel) builds on it.
- **Track A is archived** — `track-a/` removed and `nautilus_trader` dropped (the bake-off
  discards the losing track); the friction findings are preserved in this ADR + git history.
- The deferred **`vega`→`alpha` namespacing** pass (client-order-id prefix, metric names,
  pg trigger) proceeds as one mechanical PR.
- `B0.8`/`B0.9`/`B0.10`/`0.GATE` close; Phase 0 is complete.
