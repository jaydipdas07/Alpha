"""Portfolio-risk overlay (P3) — the Risk stage of the 5-stage pipeline.

A **pure, non-bypassable** clamp on the allocator's target weights, applied every
rebalance before the targets become orders (scaling design D9). It enforces three
caps, in order: per-name, per-sector (the correlation-cluster proxy — names in one
sector move together), then gross. Each cap *scales down* the offending weights
rather than rejecting the basket, so the result is always a compliant portfolio.

This is independent of the allocator's own ``portfolio.yaml`` params (defence in
depth) and of the per-order OMS risk gate, which still applies to every order.
Sectors come from the index constituents (``data.universe.load_sectors``); an
unknown symbol is its own bucket (never grouped with unrelated names).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal

from alpha_core.core.models import TargetExposure
from alpha_core.risk.limits import PortfolioLimits


def _clamp(weight: Decimal, limit: Decimal) -> Decimal:
    """Clamp magnitude to ``limit``, preserving sign."""
    if weight > limit:
        return limit
    if weight < -limit:
        return -limit
    return weight


class PortfolioRiskOverlay:
    """Clamp target weights to the portfolio limits (non-bypassable)."""

    def __init__(self, limits: PortfolioLimits, sectors: Mapping[str, str] | None = None) -> None:
        self._limits = limits
        self._sectors = sectors or {}

    def _sector(self, symbol: str) -> str:
        # Unknown → its own bucket (key off the symbol) so unrelated unknowns are
        # never capped together.
        return self._sectors.get(symbol) or symbol

    def apply(self, targets: Sequence[TargetExposure]) -> list[TargetExposure]:
        """Return ``targets`` with weights clamped to the portfolio limits."""
        weights = {t.symbol: _clamp(t.weight, self._limits.max_weight_per_name) for t in targets}

        # Per-sector: scale a sector's names down if their gross exceeds the cap.
        by_sector: dict[str, list[str]] = {}
        for symbol in weights:
            by_sector.setdefault(self._sector(symbol), []).append(symbol)
        sector_cap = self._limits.max_weight_per_sector
        for symbols in by_sector.values():
            gross = sum((abs(weights[s]) for s in symbols), Decimal(0))
            if gross > sector_cap and gross > 0:
                scale = sector_cap / gross
                for s in symbols:
                    weights[s] *= scale

        # Gross: scale everything down if total gross exceeds the cap.
        total = sum((abs(w) for w in weights.values()), Decimal(0))
        if total > self._limits.max_gross_weight and total > 0:
            scale = self._limits.max_gross_weight / total
            for s in weights:
                weights[s] *= scale

        return [t.model_copy(update={"weight": weights[t.symbol]}) for t in targets]
