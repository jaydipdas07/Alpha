"""The shared MAKER-NATIVE post-only execution fold — the #184 fill rule in ONE tested
place (extracted verbatim from the #194-review-hardened G6 fold; G6/G7 both run it).

Semantics (PaperBroker parity, LTP-strict — every clause is part of each family's
registration):

- the order is COMPOSED at decision time ``t``: the limit ``L`` = the last 1s print
  at/before ``t`` (staleness ≤ ``ENTRY_STALE_S`` else no order composed) —
  ``t``-measurable, live-composable; nothing from the flight window prices the order
  (the #194-review look-ahead fix);
- the order arrives at ``t + LATENCY_S`` and takes the GTX arrival check
  (``PaperBroker._would_cross_on_arrival``, LTP-strict): an arrival print STRICTLY
  through ``L`` ⇒ the venue rejects the post-only order — missed entry, nothing rests
  (busy only through arrival); an at-limit arrival print RESTS;
- the resting order fills only on a **strict trade-through**: the first 1s bar strictly
  after arrival, within ``TTL_S`` (inclusive — the PaperBroker expires at the first
  tick PAST ``valid_until``), whose extreme prints STRICTLY through ``L`` (BUY:
  ``low < L``; SELL: ``high > L``) — a touch never fills. Fill AT ``L``. Unfilled by
  TTL ⇒ missed entry, never chased; the working-order window is busy time;
- the exit is **taker reduce-only** (the #179 contract): at the first 1s print at/after
  fill + hold, priced at that print; ``entry_cost``/``exit_cost`` are charged per side
  (maker fee entry — filled at ``L``, no spread leg — full taker cost exit);
- **non-overlap**: one position/working order at a time; an unfinished tail trade is
  DROPPED, never fabricated (and ends the fold — nothing later could exit either);
- returns land on **exit-minute marks** over the tape's calendar (the OOS slicer sees
  time, not cherry-picked trades).

The semantics are pinned end-to-end by ``tests/test_depth_backtester.py`` (fill rule,
GTX, TTL boundary, busy windows, costs) through the G6 fold, whose review verified the
discriminating tests fail against the pre-fix code.
"""

from __future__ import annotations

import numpy as np

_MINUTE = 60
# Pre-registered execution protocol constants, shared by every family that runs this
# fold (each family's registration incorporates them by reference):
LATENCY_S = 2  # decision -> order arrival (the F3/G2 pre-registered latency)
ENTRY_STALE_S = 3  # max staleness of the decision print that prices the limit L
TTL_S = 60  # post-only rest window; unfilled => missed, never chased


def run_post_only_fold(
    ts_p: np.ndarray,
    low: np.ndarray,
    high: np.ndarray,
    close: np.ndarray,
    dec_ts: np.ndarray,
    dec_side: np.ndarray,
    *,
    hold_s: int,
    entry_cost: float,
    exit_cost: float,
) -> np.ndarray:
    """Run the post-only execution fold over trigger instants ``dec_ts`` (ascending,
    epoch seconds) with sides ``dec_side`` (+1 BUY / -1 SELL); returns minute marks."""
    n = len(ts_p)
    minutes_lo = int(ts_p[0]) // _MINUTE
    marks = np.zeros(int(ts_p[-1]) // _MINUTE - minutes_lo + 1, dtype=np.float64)
    open_until = -np.inf
    for i in range(len(dec_ts)):
        t = float(dec_ts[i])
        if t < open_until:
            continue
        # the order is COMPOSED at decision time t: its limit is the last print
        # at/before t — everything in the flight window is unknowable when the
        # order is priced (the #194-review look-ahead fix)
        j0 = int(np.searchsorted(ts_p, t, side="right")) - 1
        if j0 < 0 or t - float(ts_p[j0]) > ENTRY_STALE_S:
            continue  # dead tape at decision: no order composed
        limit = close[j0]
        side = float(dec_side[i])
        arrival = t + LATENCY_S
        # GTX arrival check (#184 _would_cross_on_arrival, LTP-strict): if the
        # arrival-instant print has traded STRICTLY through the decision-priced
        # limit, the venue rejects the post-only order — missed entry, nothing
        # ever rests (busy only through arrival). An at-limit print RESTS.
        j = int(np.searchsorted(ts_p, arrival, side="right")) - 1
        arrival_ltp = close[j]  # j >= j0 >= 0: the decision print exists
        if (side > 0 and arrival_ltp < limit) or (side < 0 and arrival_ltp > limit):
            open_until = arrival
            continue
        # rest over prints strictly after arrival, within TTL (inclusive — the
        # PaperBroker expires at the first tick PAST valid_until, fills before)
        j_end = int(np.searchsorted(ts_p, arrival + TTL_S, side="right"))
        if j + 1 >= j_end:
            open_until = arrival + TTL_S  # no prints inside the window: missed
            continue
        window_ext = low[j + 1 : j_end] if side > 0 else high[j + 1 : j_end]
        through = (window_ext < limit) if side > 0 else (window_ext > limit)
        if not bool(through.any()):
            open_until = arrival + TTL_S  # TTL expiry: missed entry, never chased
            continue
        m = j + 1 + int(np.argmax(through))
        fill_t = float(ts_p[m])
        exit_idx = int(np.searchsorted(ts_p, fill_t + hold_s, side="left"))
        if exit_idx >= n:
            break  # unfinished tail trade: drop, never fabricate
        pnl = side * (close[exit_idx] / limit - 1.0) - entry_cost - exit_cost
        marks[int(ts_p[exit_idx]) // _MINUTE - minutes_lo] += pnl
        open_until = float(ts_p[exit_idx])
    return marks
