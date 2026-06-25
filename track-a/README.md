# Track A — the NautilusTrader shell (engine bake-off)

This directory is **Track A** of the Phase-0 engine bake-off (`B0.8`): run the
**same portable B0.7 contract** (`MaCrossover`) through **NautilusTrader**, so it
can be compared head-to-head against **Track B** (the lifted Vega engine, already
in `alpha-core`, proven by `B0.9`). The engine is chosen at **`0.GATE`**
(`docs/adr/0001-engine-bakeoff.md`); the loser is discarded.

`track_a_backtest.py` wires three things together:
- the **portable contract** — `alpha_core.strategy.examples.ma_crossover.MaCrossover`,
  driven from Nautilus `on_bar` events (the integration seam: Nautilus `Bar` →
  `alpha_core.Bar` → contract → Nautilus order);
- the **shared cost kernel** — `alpha_core.execution.costs.CostModel` wired into a
  custom Nautilus `FeeModel.get_commission`, so *the same* cost logic feeds both tracks;
- the **Nautilus backtest** — `BacktestEngine` + a sandbox `BINANCE` venue.

## Why it's isolated (not in `alpha-core` / not in CI)

NautilusTrader is a large, Rust-backed dependency with its own deep tree. Keeping
it **out of the `alpha-core` kernel and CI** means the bake-off never weighs down
the (Track-B) kernel, and Track A can be deleted wholesale if Track B wins at
`0.GATE`. So this is a standalone dir with its own venv — not a uv-workspace member.

## Setup + run

```bash
cd track-a
uv venv
# the wheel is large — the 30s default UV_HTTP_TIMEOUT will time out; bump it:
UV_HTTP_TIMEOUT=600 uv pip install -e ../alpha-core nautilus_trader
.venv/bin/python track_a_backtest.py
```

## Captured result

```
=== Track A (NautilusTrader) — B0.7 MaCrossover backtest ===
fills:           2
total cost:      4.32080420 USDT (Vega CostModel via FeeModel)
start cash:      1000000 USDT
end balance:     999993.67919580 USDT
net P&L:         -6.32080420 USDT
```

The **same** contract emits the **same** 2 fills (BUY then SELL on the fast/slow
crossover) as Track B, and the **same** `CostModel` charges the cost — proving the
portable contract + cost kernel run unchanged on a second engine.

**Parity (the key result):** Track A's net **−6.32080420** matches Track B to **8
decimals**. Track-B's `final_pnl` is **−2.72240064** but that field is *net-of-
slippage, gross-of-fees*; Track B reports fees in a separate `total_fees` =
**3.59840355**, and −2.72240064 − 3.59840355 = **−6.32080419** ≡ Track A. So the two
engines **agree to the cent** on the same contract + cost kernel — **parity is NOT
the differentiator**; the engine choice rests on integration friction + time-to-market
(below).

## Integration-friction notes (input to `B0.10` / `0.GATE`)

What it took to get the contract running on Nautilus — the bake-off's real signal:

1. **Heavy install.** The `nautilus_trader` wheel times out on uv's 30s default
   (`UV_HTTP_TIMEOUT=600` fixed it). Big binary footprint; needs its own venv.
2. **A translation layer.** Nautilus's data/money types are Cython objects
   (`Bar`/`Price`/`Quantity`/`Money`) — every bar and order crosses a manual
   `Decimal`/`datetime` ↔ Nautilus boundary (~40 lines of glue here). Track B has
   none of this: the contract and OMS are already native `alpha_core` types.
3. **Cost-model mechanism mismatch (decomposition only — totals agree).** Vega's
   `CostModel` returns an all-in cost; Track B's `PaperBroker` splits it into a
   slippage-adjusted **fill price** + separate fees, whereas Nautilus separates a
   **fill model** (price impact) from the **fee model** (commission). Here the whole
   cost is mapped to commission, so the **net P&L totals match to 8 decimals** (see
   Parity above). The only residual is *where* the slippage/spread component (~0.7 =
   the 4.32 all-in cost minus the 3.60 fees) lands — Track B prices it into the fill,
   Track A books it as commission. A `B0.10` refinement
   (add a Nautilus fill model) would align the decomposition too; it does not change
   the total.
4. **Not yet done on Track A: live testnet.** Track B already trades on Binance
   testnet (`B0.9f`); the equivalent on Nautilus needs its live data/exec engine +
   Binance adapter — additional integration. That gap is itself a time-to-market
   data point for the decision.
