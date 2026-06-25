# B0.9f — Engine bake-off, Track B (lifted Vega): run the contract through the engine

**Goal (B0.9 Done-when):** the B0.7 portable contract (`MaCrossover`, a
`(bars, params) -> signals` strategy) runs *unchanged* through the **lifted Vega
engine** — first a **backtest**, then **Delta-testnet live-paper** — proving the
lift is wired end-to-end and is the parity substrate the bake-off (B0.10 / 0.GATE)
will compare against the NautilusTrader shell (Track A, B0.8).

This is Track **B** evidence only. The A/B comparison + parity oracle is **B0.10**;
the engine decision + ADR is **0.GATE**.

## 1. Backtest — DONE ✅

`MaCrossover` drives the full lifted path
`run_backtest -> StrategyEngine -> OMS (risk gate -> order FSM -> PaperBroker ->
StateStore)` over a synthetic BTCUSD bar series, with the committed crypto cost
stack (`config/costs.yaml`).

Reproduced forever by `alpha-core/tests/test_bakeoff_b0_9f.py` (in CI):
- **trades through the engine** — the contract's signals survive risk + the FSM
  and become real fills (`num_fills >= 2`, `traded_notional > 0`, P&L is `Decimal`);
- **deterministic on injected bar-time** — identical bars give an identical result
  (stats + equity curve), i.e. the engine never reads the wall clock — the TEST-1
  parity property the bake-off hangs on.

Captured run (`fast=2 / slow=4`, qty `0.01`, `starting_cash=100000`):

```
paper_fill  client_order_id=vega-24e6d26b77a01f47  price=30236.249664  qty=0.01  fees=0.30236249664
paper_fill  client_order_id=vega-1724c03636239b86  price=29964.0096    qty=0.01  fees=3.296041056
=== Vega backtest — stats ===
final P&L:       -2.72240064
total return:    -0.0027%
max drawdown:    0.0104%
Sharpe (bar):    -0.0906
fills:           2
total fees:      3.59840355264
win rate:        0.0000
traded notional: 602.00259264
turnover:        0.01x
halted:          False
```

The fills show the crypto cost model engaged (slippage on the fill price + taker
fees), P&L folded in `Decimal`, no kill-switch trip. (The `vega-` client-order-id
prefix + `=== Vega backtest ===` label are faithful-lift provenance artifacts,
deferred to a namespacing pass at 0.GATE — see the B0.9b/d PR notes.)

## 2. Delta-testnet live-paper — PENDING ⏳

Next sub-step: lift `adapters/crypto_ccxt.py` (+ `ccxt`), point it at **Delta
testnet** with the `DELTA_TESTNET_*` keys from `.env`, and run the same
`MaCrossover` on a live testnet ticker so a trivial signal places a **testnet**
(no real money) order. Mac-CLI-only (needs `.env` + network); captured here as a
transcript when run. **No live keys, no live gate** — testnet only.
