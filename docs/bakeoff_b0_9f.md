# B0.9f — Engine bake-off, Track B (lifted Vega): run the contract through the engine

**Goal (B0.9 Done-when):** the B0.7 portable contract (`MaCrossover`, a
`(bars, params) -> signals` strategy) runs *unchanged* through the **lifted Vega
engine** — first a **backtest**, then **crypto-testnet live-paper** — proving the
lift is wired end-to-end and is the parity substrate the bake-off (B0.10 / 0.GATE)
will compare against the NautilusTrader shell (Track A, B0.8).

> **Venue note:** the live-paper half targets **Binance testnet**, not Delta. Delta
> Exchange does not let one user hold both a live and a testnet account, so [You]
> provided **Binance** testnet creds (`BINANCE_TESTNET_*` in `.env`) — which is also
> the `CcxtAdapter`'s validated path (its docstring). Same `CcxtAdapter`, different
> ccxt exchange; switching to Delta later is a creds + symbol change, no code change.

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

## 2. Binance-testnet live-paper — DONE ✅

`MaCrossover`'s signal is driven through the **whole lifted execution path onto a
real testnet venue** — `StrategyEngine -> OMS (risk gate) -> CcxtAdapter -> ccxt ->
Binance testnet` — placing a live testnet order that fills, then flattening. Driver:
`scripts/bakeoff_binance_testnet.py` (Mac-CLI; needs `BINANCE_TESTNET_*` + network;
not a CI test). `set_sandbox_mode(True)` forces the sandbox — **fake money, never live**.

Captured run:

```
=== B0.9f live-paper — Binance TESTNET (sandbox) via the lifted engine ===
[feed]  BTC/USDT last=59317.44
[strat] MaCrossover emitted BUY 0.001 BTC/USDT (MA cross up)
[oms]   risk-approved -> placed: client_order_id=vega-27e2d5144b8e4948 venue_order_id=9148888 state=OPEN
[fill]  testnet order closed: filled=0.001 avg=59317.45 fee=None None
[flat]  closed 0.00100 BTC via OMS -> closed
=== done — a trivial strategy traded on Binance testnet via the lifted engine ===
```

What this proves end-to-end on a live venue: the lifted **strategy** emits a signal →
the lifted **risk gate** approves it → the lifted **OMS** derives an idempotent
client-order-id (`vega-…`, the flagged provenance prefix) and places via the lifted
**`CcxtAdapter`** → a real Binance-**testnet** order is accepted, **fills** (0.001 BTC
@ 59317.45), and is **flattened** back through the OMS. The position is left flat.

## B0.9 Done-when — met ✅

Both halves hold: **backtest runs** (§1, in CI) **and a trivial strategy trades on
testnet via the lifted engine** (§2). Track B is the ready parity substrate for the
A/B bake-off — **B0.10** (parity oracle + integration-friction memo across Track A
**B0.8** and Track B) → **0.GATE** (engine choice → `docs/adr/0001`).
