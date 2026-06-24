"""Alpha core — the thin, portable strategy contract + risk/cost/reconcile/rigor kernel.

Imported *identically* by the research-box backtester and the live worker; that shared
import is the backtest-equals-live parity guarantee (CLAUDE.md). The kernel never reads
wall-clock time (``now`` is injected, so backtest equals live) and never imports a broker
SDK. Money is always :class:`decimal.Decimal`, never a float.

Scaffolded in Phase 0 (B0.1); modules are filled in per task against ``TASKS.md``.
"""

__version__ = "0.0.0"
