"""Alpha core domain layer — enums, the broker-error taxonomy, immutable money/time-safe
models, the order FSM, and the venue-agnostic interfaces (`Strategy` / `BrokerAdapter` /
`DataFeed` / `PortfolioConstructor`).

The portable surface imported identically by the research-box backtester and the live
worker (the backtest-equals-live parity guarantee). Pure Python — no broker SDKs; money
is `Decimal`, time is tz-aware UTC, and `now` is injected. Lifted wholesale from Vega in
B0.9a; the execution / risk / backtest layers follow (B0.9b onward).
"""
