"""Carry positions forward from the trade tape between snapshots.

A registry sweep costs five minutes and the whole weight budget, so it runs
every six hours. Between sweeps the map goes stale, and stale is not a
cosmetic problem here: a wallet liquidated four hours after a snapshot is
scored against a four-hour-old position, and one that opened its position
after the snapshot is not on the map at all. Both push `gate map`'s predictive
check toward failing for reasons that have nothing to do with whether the
magnet is real.

**The published liquidation price is discarded the moment size changes.** HL's
`liquidationPx` described the position as it was; keeping it against a
different size would be a silent lie, and a silent lie in the map is worse
than a gap. Carried-forward positions are marked `derived` and re-priced by
`liqmath.derive`, whose error is measured and disclosed rather than assumed.

**What the tape cannot give.** `startPosition` -- the per-fill checkpoint that
would let reconstruction verify itself -- is on `userFills`, not on the public
trades channel we reconstruct from. There is no free checkpoint here, so drift
is caught only by reconciling against `clearinghouseState` at the next sweep.
That is the whole reason the sweep still exists.

Account equity and maintenance margin are carried unchanged from the last
sweep. They are stale by construction: a wallet's PnL moves with every tick and
the tape does not report equity. This bounds how good a derived liquidation
price can be, and is why `derived` is a fallback rather than a replacement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from nat2.features.fills import Delta
from nat2.features.liqmath import Position

# Positions smaller than this fraction of their own last size are treated as
# closed. Exact zero rarely happens in floating point, and a dust residue would
# otherwise sit on the map forever with a meaningless liquidation price.
DUST_FRACTION = 1e-6


@dataclass
class ReconResult:
    upserts: list[tuple[Position, str]] = field(default_factory=list)
    closes: list[tuple[str, str]] = field(default_factory=list)
    updated: int = 0
    opened: int = 0
    unpriceable: int = 0      # opened by a wallet we have never swept
    flipped: int = 0          # crossed through zero, long <-> short
    ignored: int = 0          # delta for an address outside the registry

    def summary(self) -> dict:
        return {
            "updated": self.updated,
            "opened": self.opened,
            "closed": len(self.closes),
            "flipped": self.flipped,
            "unpriceable": self.unpriceable,
            "ignored": self.ignored,
        }


def _account_context(positions: list[Position]) -> dict[str, Position]:
    """One position per address, to borrow account equity for new positions."""
    context: dict[str, Position] = {}
    for position in positions:
        context.setdefault(position.address, position)
    return context


def apply(
    positions: list[Position],
    deltas: list[Delta],
    marks: dict[str, float] | None = None,
) -> ReconResult:
    """Fold tape deltas into the last observed positions."""
    marks = marks or {}
    known = {(p.address, p.coin): p for p in positions}
    context = _account_context(positions)
    result = ReconResult()

    for delta in deltas:
        key = (delta.address, delta.coin)
        existing = known.get(key)
        mark = marks.get(delta.coin) or delta.last_px
        if existing is None:
            account = context.get(delta.address)
            if account is None:
                # Never swept, so no equity figure exists and no liquidation
                # price can be derived. Counted, not invented.
                result.ignored += 1
                continue
            new_szi = delta.dsz
            if abs(new_szi) <= DUST_FRACTION:
                continue
            result.opened += 1
            carried = Position(
                address=delta.address,
                coin=delta.coin,
                szi=new_szi,
                mark=mark,
                max_leverage=0.0,       # unknown for a coin never swept for this wallet
                margin_type=account.margin_type,
                account_value=account.account_value,
                maint_margin=account.maint_margin,
                isolated_margin=0.0,
                liquidation_px=None,
            )
            if not carried.max_leverage:
                result.unpriceable += 1
            result.upserts.append((carried, "derived"))
            continue

        new_szi = existing.szi + delta.dsz
        if abs(new_szi) <= abs(existing.szi) * DUST_FRACTION or new_szi == 0:
            result.closes.append(key)
            continue
        if new_szi * existing.szi < 0:
            result.flipped += 1
        result.updated += 1
        result.upserts.append((
            Position(
                address=existing.address,
                coin=existing.coin,
                szi=new_szi,
                mark=mark,
                max_leverage=existing.max_leverage,
                margin_type=existing.margin_type,
                account_value=existing.account_value,
                maint_margin=existing.maint_margin,
                isolated_margin=existing.isolated_margin,
                # Discarded: it described a size this position no longer has.
                liquidation_px=None,
            ),
            "derived",
        ))
    return result


def drift(derived: list[Position], published: list[Position]) -> dict:
    """Compare carried-forward sizes against a fresh sweep.

    The only check available, since the public tape carries no per-fill
    checkpoint. Reported as a relative size error per position.
    """
    truth = {(p.address, p.coin): p.szi for p in published}
    errors = []
    missing = 0
    for position in derived:
        actual = truth.get((position.address, position.coin))
        if actual is None:
            missing += 1
            continue
        if actual == 0:
            continue
        errors.append(abs(position.szi - actual) / abs(actual))
    errors.sort()
    return {
        "compared": len(errors),
        "missing": missing,
        "median": errors[len(errors) // 2] if errors else 0.0,
        "p90": errors[int(0.9 * len(errors))] if errors else 0.0,
        "exact_frac": (sum(1 for e in errors if e < 1e-6) / len(errors)) if errors else 0.0,
    }
