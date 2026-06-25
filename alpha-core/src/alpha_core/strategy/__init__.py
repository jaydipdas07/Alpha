"""Strategy layer — the portable `(bars, params) -> signals` contract + examples.

Strategies implement the :class:`alpha_core.core.interfaces.Strategy` ABC, carry their
own validated config (no magic numbers — tunables live in ``config/strategies/``), and
are pure of broker SDKs. The same strategy runs in backtest and live.
"""
