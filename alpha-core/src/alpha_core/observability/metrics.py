"""Prometheus metrics for the trading loop (ADR engineering standards).

Counters/gauges for orders, fills, risk decisions, P&L, open positions, feed lag,
and errors. A dedicated registry keeps tests isolated and avoids global-state
collisions across imports.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge

registry = CollectorRegistry()

orders_placed = Counter(
    "vega_orders_placed_total",
    "Orders submitted to a broker",
    ["venue", "strategy"],
    registry=registry,
)
orders_rejected = Counter(
    "vega_orders_rejected_total",
    "Orders rejected (risk or broker)",
    ["reason"],
    registry=registry,
)
fills = Counter(
    "vega_fills_total",
    "Fills received",
    ["venue", "side"],
    registry=registry,
)
risk_decisions = Counter(
    "vega_risk_decisions_total",
    "Pre-trade risk decisions",
    ["outcome"],
    registry=registry,
)
errors = Counter(
    "vega_errors_total",
    "Errors by class",
    ["kind"],
    registry=registry,
)
kill_switch_trips = Counter(
    "vega_kill_switch_trips_total",
    "Kill-switch trips",
    ["trigger"],
    registry=registry,
)

open_positions = Gauge(
    "vega_open_positions",
    "Current number of open positions",
    registry=registry,
)
realized_pnl = Gauge(
    "vega_realized_pnl",
    "Running realized P&L (quote ccy)",
    registry=registry,
)
feed_lag_seconds = Gauge(
    "vega_feed_lag_seconds",
    "Seconds since the last valid tick",
    registry=registry,
)
heartbeat_alive = Gauge(
    "vega_heartbeat_alive",
    "1 if the loop has beaten within its staleness budget, else 0",
    registry=registry,
)

_server_started = False


def start_metrics_server(port: int = 9090) -> None:
    """Expose ``registry`` over HTTP for Prometheus to scrape (idempotent)."""
    global _server_started
    if _server_started:
        return
    from prometheus_client import start_http_server

    start_http_server(port, registry=registry)
    _server_started = True
