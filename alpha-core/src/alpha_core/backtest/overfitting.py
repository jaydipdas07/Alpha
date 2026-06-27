"""Advanced backtest-overfitting rigor (B1a.4) — CPCV + embargo + PBO.

Two honest-results tools, both pure-stdlib (no numpy) so they stay importable in the
lean worker kernel and on the research box alike:

- ``cpcv_splits`` — Combinatorial Purged Cross-Validation (López de Prado, AFML
  ch. 12): split the in-sample series into ``n_groups`` contiguous groups and hold out
  every ``n_test_groups``-sized combination as a test fold, purging/embargoing the
  observations around each test block so a label adjacent to the boundary cannot leak
  into training. Yields many train/test paths instead of one fragile split.

- ``probability_of_backtest_overfitting`` — PBO via CSCV (Bailey, Borwein, López de
  Prado & Zhu 2017, "The Probability of Backtest Overfitting"): from a (time x trial)
  performance matrix, the probability that the trial selected as best in-sample lands
  below the median out-of-sample — the share of in-sample/out-of-sample partitions
  where selection failed to generalize. PBO near 0.5 means selection is no better than
  chance (overfit); near 0 means it generalizes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations
from math import ceil

from alpha_core.backtest.metrics import sharpe


def _contiguous_groups(n: int, k: int) -> list[range]:
    """Partition ``range(n)`` into ``k`` contiguous, near-equal groups (the first
    ``n % k`` groups get one extra element)."""
    if not 0 < k <= n:
        raise ValueError(f"need 0 < k <= n; got k={k}, n={n}")
    base, extra = divmod(n, k)
    out: list[range] = []
    start = 0
    for i in range(k):
        size = base + (1 if i < extra else 0)
        out.append(range(start, start + size))
        start += size
    return out


def _contiguous_blocks(indices: Sequence[int]) -> list[tuple[int, int]]:
    """Maximal runs of consecutive integers in ``indices`` as ``(first, last)`` pairs."""
    blocks: list[tuple[int, int]] = []
    for i in sorted(indices):
        if blocks and i == blocks[-1][1] + 1:
            blocks[-1] = (blocks[-1][0], i)
        else:
            blocks.append((i, i))
    return blocks


@dataclass(frozen=True, slots=True)
class CPCVSplit:
    """One combinatorial purged train/test split, as observation indices."""

    train: tuple[int, ...]
    test: tuple[int, ...]


def cpcv_splits(
    n_obs: int, *, n_groups: int, n_test_groups: int, embargo_frac: float = 0.0
) -> list[CPCVSplit]:
    """All Combinatorial Purged CV splits of ``range(n_obs)``.

    Partition the observations into ``n_groups`` contiguous groups; for every choice of
    ``n_test_groups`` groups the test set is their union and the train set is the
    complement minus an *embargo*: the ``ceil(embargo_frac * n_obs)`` observations on
    either side of each contiguous test block (purge before + embargo after), so a train
    label adjacent to the test cannot leak across the boundary. Returns
    ``C(n_groups, n_test_groups)`` splits.
    """
    if not 1 <= n_test_groups < n_groups <= n_obs:
        raise ValueError(
            "need 1 <= n_test_groups < n_groups <= n_obs; got "
            f"n_test_groups={n_test_groups}, n_groups={n_groups}, n_obs={n_obs}"
        )
    if not 0.0 <= embargo_frac <= 1.0:
        raise ValueError(f"embargo_frac must be in [0, 1]; got {embargo_frac}")
    groups = _contiguous_groups(n_obs, n_groups)
    horizon = ceil(embargo_frac * n_obs)
    splits: list[CPCVSplit] = []
    for combo in combinations(range(n_groups), n_test_groups):
        test = sorted(i for g in combo for i in groups[g])
        test_set = set(test)
        purged: set[int] = set()
        for first, last in _contiguous_blocks(test):
            purged.update(range(max(0, first - horizon), first))  # purge before the block
            purged.update(range(last + 1, min(n_obs, last + 1 + horizon)))  # embargo after
        train = tuple(i for i in range(n_obs) if i not in test_set and i not in purged)
        splits.append(CPCVSplit(train=train, test=tuple(test)))
    return splits


@dataclass(frozen=True, slots=True)
class PBOResult:
    """The CSCV verdict — Probability of Backtest Overfitting."""

    pbo: float  # P(in-sample-best trial is below the out-of-sample median)
    logits: tuple[float, ...]  # logit of the OOS relative rank, one per partition
    n_trials: int
    n_paths: int  # number of in-sample/out-of-sample partitions evaluated

    def is_overfit(self, threshold: float) -> bool:
        """Selection is flagged overfit when PBO exceeds ``threshold`` (``rigor.yaml``)."""
        return self.pbo > threshold


def probability_of_backtest_overfitting(
    performance: Sequence[Sequence[float]], *, n_splits: int
) -> PBOResult:
    """PBO via CSCV from a ``performance`` matrix of shape ``(T time-buckets, N trials)``.

    Split the T rows into ``n_splits`` contiguous subsets; for every way of assigning
    half the subsets to in-sample (the rest out-of-sample), pick the trial with the best
    in-sample Sharpe, find its *relative rank* out-of-sample, and take the logit of that
    rank. PBO is the fraction of partitions whose logit is < 0 — i.e. the in-sample-best
    trial landed below the OOS median. Deterministic (no RNG): the same matrix always
    yields the same PBO.
    """
    n_time = len(performance)
    if n_time == 0:
        raise ValueError("performance matrix is empty")
    n_trials = len(performance[0])
    if n_trials < 2:
        raise ValueError(f"need >= 2 trials to rank; got {n_trials}")
    if any(len(row) != n_trials for row in performance):
        raise ValueError("performance matrix must be rectangular (every row has N trials)")
    if n_splits % 2 != 0 or n_splits < 2:
        raise ValueError(f"n_splits must be an even integer >= 2; got {n_splits}")
    if n_splits > n_time:
        raise ValueError(f"n_splits={n_splits} cannot exceed T={n_time} rows")

    subsets = _contiguous_groups(n_time, n_splits)
    logits: list[float] = []
    for combo in combinations(range(n_splits), n_splits // 2):
        is_set = {t for s in combo for t in subsets[s]}
        is_rows = sorted(is_set)
        oos_rows = [t for t in range(n_time) if t not in is_set]
        is_perf = [sharpe([performance[t][n] for t in is_rows]) for n in range(n_trials)]
        oos_perf = [sharpe([performance[t][n] for t in oos_rows]) for n in range(n_trials)]
        best = is_perf.index(max(is_perf))  # in-sample-best trial (first on ties)
        # Relative rank of the IS-best trial among OOS performances, mapped into (0, 1):
        # rank = how many trials it is >= to (1..N), normalized by N+1 to avoid 0 and 1.
        rank = sum(1 for v in oos_perf if v <= oos_perf[best])
        omega = rank / (n_trials + 1)
        logits.append(math.log(omega / (1.0 - omega)))
    pbo = sum(1 for lam in logits if lam < 0.0) / len(logits)
    return PBOResult(pbo=pbo, logits=tuple(logits), n_trials=n_trials, n_paths=len(logits))
