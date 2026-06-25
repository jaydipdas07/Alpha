"""Alpha core domain layer — enums, immutable money/time-safe models, and the
strategy contract.

This is the portable surface imported identically by the research-box backtester and
the live worker. B0.7 lands the minimal slice (`Bar`/`Tick`/`Signal` + the `Strategy`
ABC); the full engine (`Order`/`Fill`/`Position`, the order FSM, broker adapters) is
lifted wholesale from Vega in B0.9.
"""
