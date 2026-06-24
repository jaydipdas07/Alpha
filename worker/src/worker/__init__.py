"""Alpha worker — the sole live executor (a static-IP VPS process).

The worker imports :mod:`alpha_core` (the same kernel code the research-box backtester
runs) and is the *only* component that places real orders and holds broker truth: the
pod never trades, and Lemma is never on the money path (TEST-8). Risk is checked first
on every order and the kill-switch latches across restarts.

Scaffolded in Phase 0 (B0.1); modules are filled in per task against ``TASKS.md``.
"""

__version__ = "0.0.0"
