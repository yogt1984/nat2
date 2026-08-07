"""Liquidation prices.

**HL publishes `liquidationPx` per position, and that published value is the
source of truth.** This module derives the same quantity, for one reason only:
between snapshots the registry is maintained from the fill stream, and a
position whose size changed since the last snapshot has no published number
until the next one.

Deriving is therefore an approximation with a measured error, not a
replacement. `validate` reports that error against HL's own field, and the map
reports what fraction of its notional is published versus derived. Measured
2026-08-07 over 1,074 cross positions: exact (rel err < 1e-4) for 58% of
positions in accounts holding no isolated margin, and materially wrong
otherwise -- which is precisely why it is not trusted as the primary source.

Cross and isolated differ in what margin backs the position:

    cross     the whole account's equity, less maintenance margin already used
    isolated  only the margin segregated to that position

For cross, an account liquidates as a unit, so a position's liquidation price
is the price of *that* asset at which account equity meets maintenance margin,
holding the other marks fixed.
"""

from __future__ import annotations

from dataclasses import dataclass

# Maintenance leverage is twice the asset's max leverage, so the maintenance
# margin fraction is half the initial. VERIFY: asset-level tiering may make
# this size-dependent for large positions, which is a live suspect for the
# derivation's residual error.
MAINTENANCE_LEVERAGE_MULTIPLE = 2.0


@dataclass(frozen=True)
class Position:
    address: str
    coin: str
    szi: float              # signed size; negative is short
    mark: float
    max_leverage: float
    margin_type: str        # "cross" | "isolated"
    account_value: float    # cross: crossMarginSummary.accountValue
    maint_margin: float     # cross: crossMaintenanceMarginUsed
    isolated_margin: float = 0.0
    liquidation_px: float | None = None   # HL's published value, when known

    @property
    def size(self) -> float:
        return abs(self.szi)

    @property
    def side(self) -> int:
        return 1 if self.szi > 0 else -1

    @property
    def notional(self) -> float:
        return self.size * self.mark


def maintenance_fraction(max_leverage: float) -> float:
    return 1.0 / (MAINTENANCE_LEVERAGE_MULTIPLE * max_leverage)


def derive(position: Position) -> float | None:
    """Liquidation price from account state. None when it cannot be computed."""
    if position.size == 0 or position.mark <= 0 or position.max_leverage <= 0:
        return None
    l = maintenance_fraction(position.max_leverage)
    side = position.side
    if position.margin_type == "isolated":
        available = position.isolated_margin - position.size * position.mark * l
    else:
        available = position.account_value - position.maint_margin
    denominator = position.size * (1 - l * side)
    if denominator == 0:
        return None
    price = position.mark - side * available / denominator
    return price if price > 0 else None


def effective(position: Position) -> tuple[float | None, str]:
    """The liquidation price to use, and where it came from."""
    if position.liquidation_px and position.liquidation_px > 0:
        return position.liquidation_px, "published"
    return derive(position), "derived"


def validate(positions: list[Position]) -> dict:
    """Derivation error against HL's published `liquidationPx`.

    Reported on every map card. A derivation drifting from the published value
    is the earliest warning that HL's margin rules moved under us.
    """
    errors = []
    for position in positions:
        published = position.liquidation_px
        derived = derive(position)
        if published and derived and published > 0:
            errors.append(abs(derived - published) / published)
    if not errors:
        return {"n": 0}
    errors.sort()
    return {
        "n": len(errors),
        "median": errors[len(errors) // 2],
        "p90": errors[int(0.9 * len(errors))],
        "exact_frac": sum(1 for e in errors if e < 1e-4) / len(errors),
    }
