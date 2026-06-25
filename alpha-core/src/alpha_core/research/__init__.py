"""Research-box coordination: heartbeat staleness + work leases (R8).

So a dead or stalled research box never hangs a discovery workflow — its heartbeat
goes stale (alertable) and its backtest lease expires (reclaimable by another box).
Pure, injected-`now`, tz-aware-UTC logic; the research box / pod supply the actual
`research_status` table I/O around it.
"""
