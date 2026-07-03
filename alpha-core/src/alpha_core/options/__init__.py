"""Options capability (ADR 0017) — pricing/Greeks math, chain selection.

Analytics live here; CONTRACT metadata lives on ``InstrumentSpec.option``
(``core.models.OptionContract``), and the risk-side caps/margins live in
``risk/`` (non-bypassable, as always). Nothing in this package reads a clock,
a network, or a store — pure functions the loops inject inputs into.
"""
