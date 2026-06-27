# Alpha strategy knowledge — the seed templates & the rigor gate

This is the `/knowledge` corpus (B1b.4c): a strategy reference the discovery agents and the cockpit
can answer questions from via `files search`. It describes the **vetted seed templates** the
constrained strategist parameterizes, the **economic rationale** of each, and the **rigor gate** that
decides which proposals survive. It contains no market data, no holdout, no ledger — it is a
read-only knowledge file (TEST-3 / R6: `/knowledge` never exposes the holdout).

## How discovery works (the constrained track)

The **strategist** does not write code. It proposes a strategy by choosing parameter values for one
of the vetted templates below, each value inside a whitelisted range. Three properties hold by
construction:

- **In-sample only — never the holdout.** The strategist takes no market data at all; it emits
  configs. It therefore has no path to the locked holdout (TEST-3 is structural, not a runtime
  check). A proposal is evaluated on in-sample bars by the backtester; the holdout is the one-shot
  final gate only.
- **It never tunes position size.** Only *edge* parameters are whitelisted (periods, thresholds,
  bands). `quantity` is the risk overlay's job, never an edge knob.
- **Originality + honest trial accounting.** Every accepted proposal is fingerprinted and recorded
  in the idempotent proposal ledger, so re-proposing a config is a no-op and the per-cell trial
  count the Deflated Sharpe Ratio penalizes by can never be inflated.

A **discovery cell** is a `(market, family, window)` — e.g. `(crypto, ma_crossover, btcusdt-5m)`.
The nightly run sweeps every template over every configured cell.

## The seed templates (R7)

Each template lists its **economic idea**, **entry/exit**, and the **tunable edge parameters** (with
the whitelisted ranges the strategist may explore).

### `ma_crossover` — moving-average crossover (trend-following)
Ride a trend: when a fast moving average crosses **above** a slow one, momentum has turned up — go
long; when it crosses **below**, go short. Exits on the opposite crossover (always in the market,
flipping side). Works where trends persist; whipsaws in chop.
- `fast_period` — bars in the fast MA. Range **3–30**.
- `slow_period` — bars in the slow MA (must exceed `fast_period`). Range **10–100**.

### `rsi_bollinger` — RSI + Bollinger mean-reversion (fade extremes)
Fade over-extension: go **long** when RSI is oversold **and** the close is below the lower Bollinger
band; go **short** when RSI is overbought **and** the close is above the upper band. Exit when price
reverts to the band mid (the SMA). Conviction scales with how far past the RSI threshold the reading
is. Works in range-bound regimes; dangerous in strong trends (extremes get more extreme).
- `rsi_period` — RSI lookback. Range **5–30**.
- `oversold` / `overbought` — RSI entry thresholds. Ranges **10–40** / **60–90** (step 5).
- `bollinger_period` — SMA/band lookback. Range **10–40**.
- `num_std` — band width in standard deviations. Range **1–3** (step 0.5).

### `momentum_roc` — rate-of-change momentum (trend-following)
Trade persistence of returns: go **long** when the `period`-bar rate of change pushes above
`+threshold_pct`, **short** when it falls below `-threshold_pct`; flip on the opposite signal.
Conviction scales with the ROC magnitude. Captures sustained moves; gives back at turns.
- `period` — ROC lookback in bars. Range **3–40**.
- `threshold_pct` — the % move that triggers entry. Range **0.5–5** (step 0.5).

### `vwap_reversion` — VWAP reversion (intraday mean-reversion)
Mean-revert to the session VWAP (typical-price × volume / cumulative volume): when the close
stretches more than `band_bps` **above** VWAP it shorts (expecting a pullback); more than that
**below**, it buys. The position closes when price crosses back through VWAP. An intraday,
liquidity-anchored edge; needs volume.
- `band_bps` — stretch from VWAP (in basis points) that triggers entry. Range **10–100** (step 5).

### `opening_range_breakout` (ORB) — opening-range breakout
Trade the day's first decisive move: build the **opening range** (high/low of the first
`opening_range_minutes`), then **enter once** on the first bar that *closes* beyond the range, with a
`breakout_buffer_bps` cushion — a close above the high → long, below the low → short. **Exit at the
opposite extreme** (the classic ORB stop): a long exits if a later bar closes below the range low; a
short exits if a bar closes above the range high. A breakout edge; whipsaws on range-bound days.
- `opening_range_minutes` — minutes used to build the opening range. Range **5–60**.
- `breakout_buffer_bps` — cushion beyond the range before entry. Range **2–30** (step 2).

## The rigor gate (how a proposal is judged → promote / reject / revise)

The **quant-analyst** composes the statistical-rigor kernel into one verdict per candidate. In order:

1. **Out-of-sample edge screen.** Compute the candidate's Sharpe on the out-of-sample slice; if it
   has no OOS edge, **reject**. This is also how a candidate that looked great in-sample but fails
   out-of-sample (the classic overfit) dies.
2. **Deflated Sharpe Ratio vs the cell's trial count** *(the promote-work)*. The DSR (Bailey–López de
   Prado) discounts the Sharpe by **how many configs have already been tried in the cell** — the more
   you search, the higher the bar, because the best of many random tries looks good by luck. The
   candidate must clear the configured DSR threshold (currently **0.92** — a deliberately
   *conservative* bar: it certifies strong edges, ~Sharpe 2.4+, and rejects weaker ones, holding the
   false-promote rate near zero). The trial count is the **cumulative** per-cell count from the
   durable ledger, so re-runs across nights don't reset the penalty.
3. **Fold consistency.** A regime check across cross-validation folds: a candidate that is
   statistically significant but only works in one regime is fragile → **revise**.
4. **Cell-PBO (optional).** The Probability of Backtest Overfitting over the cell's trial×fold matrix;
   evidence of overfit selection → **revise**.

A candidate that clears the OOS screen and the DSR, and is fold-consistent, is **promoted** — a
*survivor*. Survivors still face the **one-shot holdout gate** and **human approval** before any paper
or live run; promotion in discovery is necessary, not sufficient.

> The thresholds (`dsr.threshold`, the OOS-Sharpe floor, fold-consistency, PBO, CPCV embargo) live in
> `config/rigor.yaml` — one config value per knob, no magic numbers in code.
