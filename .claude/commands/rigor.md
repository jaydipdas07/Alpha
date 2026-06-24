---
description: Run the configured strategy through the rigor gate and report pass/fail
argument-hint: "[extra args, e.g. --require-edge or a data path]"
allowed-tools: Bash(uv run alpha backtest*), Bash(uv run python scripts/backtest_rigor.py*), Bash(grep*), Read
---

Run the backtest **rigor gate** and report whether the edge clears it. The gate is the crown jewel
(`docs/DESIGN_v4.md`): only a survivor is worth a paper run, and only a paper survivor is worth a
human go-live FORM.

> **Availability:** the rigor gate lands in **Phase 1a** (lift Vega's look-ahead/walk-forward/stress in
> `B1a.3`; CPCV/PBO/DSR/holdout in `B1a.4–B1a.6`). Until `alpha` exposes `backtest --rigor`, report
> that the gate isn't built yet and point at the next `B1a.x` task instead of guessing a verdict.

## Context (do not guess)

- Active env / engine / strategy: !`uv run alpha status 2>/dev/null | grep -iE "env|engine|adapter|strateg" || echo "(alpha CLI not built yet — see Phase 1a in TASKS.md)"`

## Run

Run `uv run alpha backtest --rigor --require-edge $ARGUMENTS`.

`--require-edge` makes the command exit non-zero unless the edge survives 2× slippage **and** is
stable across out-of-sample windows. A FAIL on a fresh candidate is expected, not a bug.

## Report

A tight verdict, citing the actual numbers (not vibes):

- **Look-ahead audit** — clean or violated (where)?
- **2× slippage stress** — `final_pnl` still > 0?
- **Walk-forward** — stable across windows, or fitted to one?
- **CPCV / PBO** — probability of backtest overfitting; **Deflated Sharpe** vs the keyed trial-ledger count.
- **Holdout** — untouched (it must never be tuned against; one-shot only, Workflow B).
- **One-line verdict:** CLEARS / FAILS the gate, and the single reason why.

Do **not** edit any config or strategy, and **never** read or print the holdout partition (TEST-3). If
the user wants a different strategy tested, tell them to set it in `config/<env>.yaml` first.
