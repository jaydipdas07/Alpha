"""B1b.4a — fire a nightly discovery run over the configured universe.

The portable entry point the Lemma ``nightly-discovery`` schedule (B1b.4b) invokes on the research
box. It reads the no-ACL cold store (in-sample only — the holdout is sealed away, TEST-3) and a
**durable** proposal ledger, sweeps every cell xstrategy template in ``config/discovery.yaml``
through the real engine + rigor gate, and prints the survivors and any quarantined combos. The
durable ledger means re-runs never re-count a trial (idempotent), so the DSR multiple-testing
penalty stays honest across nights.

Research-plane: no keys, no network, no live orders. Run:

    uv run python scripts/nightly_discovery.py
"""

from __future__ import annotations

from pathlib import Path

from alpha_core.data.store import BarStore
from alpha_core.research.nightly import run_nightly_discovery_from_config

_ROOT = Path(__file__).resolve().parents[1]
STORE_ROOT = _ROOT / "data_cold"  # gitignored cold store (B1a.1)
LEDGER_PATH = _ROOT / "proposal_ledger.sqlite"  # gitignored durable proposal ledger (cross-run)


def main() -> None:
    print(f"=== B1b.4a nightly discovery -> cold store {STORE_ROOT} ===")
    report = run_nightly_discovery_from_config(BarStore(STORE_ROOT), ledger_path=LEDGER_PATH)
    print(
        f"cycles run: {len(report.reports)}  |  survivors: {len(report.survivors)}  |  "
        f"quarantined: {len(report.quarantined)}"
    )
    for survivor in report.survivors:
        print(
            f"  PROMOTE  {survivor.market.value}/{survivor.window}  {survivor.template}  "
            f"{dict(survivor.params)}"
        )
    for q in report.quarantined:
        print(f"  QUARANTINE  {q.market.value}/{q.window}  {q.template}: {q.error}")
    print("=== done ===")


if __name__ == "__main__":
    main()
