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


# --- whole-population position series from the tape (TASK_2/07) -----------
#
# `apply` above carries the *registry* forward one delta batch at a time. The
# functions below answer a different question: the position path of every
# wallet on the tape over a window, with the anchor problem made explicit.
#
# A cumulative sum is only a position if it started from a known one. Three
# cases, in order of preference, and nothing else:
#   published -- the wallet was in the last sweep, window starts at/after it;
#   userfills -- a REST `userFills` fill carries `startPosition`, a checkpoint;
#   none      -- `anchored=False`. Such rows are an estimate and estimates do
#                not enter gates. A capture gap inside the window demotes every
#                wallet to `none`: the tape is missing prints, so no sum is a
#                position. No interpolation across gaps, ever -- an interpolated
#                position is exactly the "estimated map" the repo refuses.

import polars as pl

from nat2.features.fills import signed_parties

SERIES_SCHEMA = {
    "address": pl.Utf8, "coin": pl.Utf8, "ts_ns": pl.Int64, "tid": pl.Int64,
    "szi": pl.Float64, "anchored": pl.Boolean, "anchor_source": pl.Utf8,
}


@dataclass(frozen=True)
class Anchor:
    address: str
    coin: str
    szi: float
    ts_ns: int
    source: str          # published | userfills
    tid: int = 0         # userfills: the checkpointed fill; published: 0


def tape_gaps(entries, from_ns: int, to_ns: int, cadence_s: float) -> list[tuple[int, int]]:
    """Holes between consecutive closed `hl.trades` files overlapping the window."""
    files = sorted((e.first_ingest, e.last_ingest) for e in entries
                   if e.stream == "hl.trades" and e.last_ingest >= from_ns and e.first_ingest <= to_ns)
    limit = int(2 * cadence_s * 1e9)
    return [(a_end, b_start) for (_, a_end), (b_start, _) in zip(files, files[1:])
            if b_start - a_end > limit]


def ingest_silences(records, from_ns: int, to_ns: int, max_silence_s: float) -> list[tuple[int, int]]:
    """Holes *inside* files: stretches of `t_ingest` (our clock) with no record.

    The manifest sees rotation, not content -- a 33-minute hole inside an
    otherwise healthy hourly file (2026-08-20 18:21-18:54 CEST) is only visible
    here. Our clock, not the exchange's: after a reconnect the first records
    carry a backlog of prints with older `time`s, which would make the outage
    look 12 minutes shorter than it was.
    """
    times = sorted({int(r["t_ingest"]) for r in records
                    if from_ns <= int(r.get("t_ingest") or -1) <= to_ns})
    limit = int(max_silence_s * 1e9)
    return [(a, b) for a, b in zip(times, times[1:]) if b - a > limit]


@dataclass(frozen=True)
class Checkpoint:
    address: str
    coin: str
    ts_ns: int
    tid: int             # 0: block-level checkpoint (position before any fill in the block)
    szi_before: float


USERFILLS_CAP = 2000     # HL returns at most this many most-recent fills


def checkpoints(fills, address: str, coin: str) -> list[Checkpoint]:
    """Position at the start of each block, from `userFills.startPosition`.

    At the cap the response's oldest block may be cut mid-block -- its root is
    then some later fill's start, a wrong anchor that shows up as a constant
    offset on every later checkpoint (seen: 2.47 BTC). So the oldest block of
    a capped response is not a checkpoint.

    Measured, not assumed: `tid` order is *not* execution order inside a block
    (1790/1999 consecutive fills of one market maker fail to chain by tid, yet
    all 1999 chain by `startPosition` continuity and never reverse block time).
    So the only checkpoint the field defines is per block: the one fill whose
    `startPosition` is not any other fill's end position in that block. A block
    without a unique root is skipped rather than guessed.
    """
    blocks: dict[int, list[dict]] = {}
    oldest = None
    for f in fills or []:
        try:
            t = int(f["time"])
        except (KeyError, TypeError, ValueError):
            continue
        oldest = t if oldest is None else min(oldest, t)
        if f.get("coin") == coin and f.get("startPosition") is not None:
            blocks.setdefault(t, []).append(f)
    out = []
    for t, group in blocks.items():
        try:
            ends = {round(float(f["startPosition"]) + (1 if f["side"] == "B" else -1) * float(f["sz"]), 8)
                    for f in group}
            roots = {round(float(f["startPosition"]), 8) for f in group} - ends
        except (KeyError, TypeError, ValueError):
            continue
        if len(roots) == 1:
            out.append(Checkpoint(address, coin, t * 1_000_000, 0, roots.pop()))
    out.sort(key=lambda c: c.ts_ns)
    if len(fills or []) >= USERFILLS_CAP and out and out[0].ts_ns == oldest * 1_000_000:
        out = out[1:]
    return out


def anchors_from_checkpoints(cps: list[Checkpoint], from_ns: int = 0, to_ns: int = 2**63 - 1) -> list[Anchor]:
    """Earliest checkpoint *inside the window* per wallet anchors it from that block on.

    A checkpoint before the window is a position the tape in hand cannot carry
    forward from -- the prints between it and `from_ns` were not read.
    """
    first: dict[tuple[str, str], Checkpoint] = {}
    for c in cps:
        if from_ns <= c.ts_ns <= to_ns:
            first.setdefault((c.address, c.coin), c)
    return [Anchor(c.address, c.coin, c.szi_before, c.ts_ns, "userfills", c.tid) for c in first.values()]


def series(trades, coin: str, anchors: list[Anchor], from_ns: int, to_ns: int,
           gap_free: bool = True) -> pl.DataFrame:
    """One linear pass over prints -> per-wallet position after each print.

    Signing is `signed_parties`: buyer +sz, seller -sz, `side` never consulted.
    """
    by_wallet: dict[str, Anchor] = {}
    for a in anchors:
        if a.coin == coin and (a.address not in by_wallet or a.ts_ns < by_wallet[a.address].ts_ns):
            by_wallet[a.address] = a
    prints = []
    for t in trades:
        if t.get("coin") != coin:
            continue
        try:
            ts = int(t["time"]) * 1_000_000
            tid = int(t["tid"])
        except (KeyError, TypeError, ValueError):
            continue
        if from_ns <= ts <= to_ns:
            prints.append((ts, tid, t))
    prints.sort(key=lambda p: (p[0], p[1]))

    pos: dict[str, float] = {}
    state: dict[str, tuple[bool, str]] = {}     # address -> (anchored, source)
    rows = []
    for ts, tid, t in prints:
        for address, signed in signed_parties(t):
            if address not in state:
                a = by_wallet.get(address)
                if a is not None and gap_free and a.ts_ns <= from_ns:
                    pos[address], state[address] = a.szi, (True, a.source)
                else:
                    pos[address], state[address] = 0.0, (False, "none")
            a = by_wallet.get(address)
            # A mid-window userFills anchor re-bases the wallet from its first
            # checkpoint: rows before it stay unanchored, rows from it on are exact.
            if (a is not None and gap_free and not state[address][0]
                    and a.source == "userfills" and (a.ts_ns, a.tid) <= (ts, tid)):
                pos[address], state[address] = a.szi, (True, a.source)
            pos[address] += signed
            anchored, source = state[address]
            rows.append((address, coin, ts, tid, pos[address], anchored, source))
    return pl.DataFrame(rows, schema=SERIES_SCHEMA, orient="row")


def drift_audit(frame: pl.DataFrame, cps: list[Checkpoint]) -> dict:
    """|tape position before a checkpointed block - block start position|, per checkpoint.

    This series is the measurement of whether tape reconstruction can be
    trusted at all. Reported, never used to correct anything.
    """
    errors: list[float] = []
    wallets: set[str] = set()
    skipped = 0
    for c in cps:
        prior = frame.filter(
            (pl.col("address") == c.address) & (pl.col("coin") == c.coin)
            & ((pl.col("ts_ns") < c.ts_ns) | ((pl.col("ts_ns") == c.ts_ns) & (pl.col("tid") < c.tid)))
            & pl.col("anchored")
        )
        if prior.is_empty():
            skipped += 1
            continue
        tape = prior.sort(["ts_ns", "tid"])["szi"][-1]
        errors.append(abs(tape - c.szi_before))
        wallets.add(c.address)
    errors.sort()
    n = len(errors)
    return {
        "name": "reconstruction_drift",
        "compared": n, "skipped": skipped, "wallets": len(wallets),
        "exact_frac": (sum(1 for e in errors if e < 1e-9) / n) if n else None,
        "median": errors[n // 2] if n else None,
        "p90": errors[int(0.9 * n)] if n else None,
        "max": errors[-1] if n else None,
    }
