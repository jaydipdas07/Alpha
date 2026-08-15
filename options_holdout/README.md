# options_holdout/ — what is (and is not) in version control

This directory is the **options holdout root**: the sealed, gate-only partition of the options
research data (TEST-3, holdout isolation).

**Tracked in git: `_windows.json` only** — the seal-boundary manifest (window start/end instants +
a content-version hash per series). It must be versioned: the seal protocol pins each holdout
boundary monotonically against the previously recorded window, so the manifest is the tamper
evidence that a boundary never moved backward.

**Never tracked: the data.** The holdout market data (`*.parquet`) lives here only on research
machines and is excluded by the repository-wide `*.parquet` gitignore rule. It has never been
committed (verified over the full history). Discovery agents cannot read this partition; the
one-shot holdout gate is the single legitimate reader.
