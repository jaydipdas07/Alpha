"""Free-data ingest transforms (B1a.1b).

Pure, network-free converters from a source's raw rows into core ``Bar`` models
(Decimal money, tz-UTC). The actual HTTP fetch + cold-store write lives in
``scripts/ingest_cold_store.py`` — keeping these transforms unit-testable in CI.
"""
