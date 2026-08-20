# FINDINGS

What measurement established, as distinct from what was designed (`DESIGN.md`)
or what is planned (`TASKS.md`). Everything here came from running against live
Hyperliquid mainnet or from the system's own data, and several entries reversed
a decision that looked obvious beforehand.

Dates are when the measurement was taken. Nothing here is a claim about the
market — no gate has passed, so the magnet hypothesis remains untested.

---

## Venue mechanics

**The public trade tape carries both counterparty addresses.** *(2026-08-08)*
Every print on `hl.trades` includes `users: [buyer, seller]`, so the venue's
entire fill flow is reconstructable from one non-user subscription. `users[0]`
is the buyer — verified 8/8 by matching `tid` against those wallets' `userFills`.
`trade.side` is the **aggressor's** side and is *not* either counterparty's
direction; signing positions from it inverts the map for every passive fill.

**Websocket user tracking caps at 15 per connection.** *(2026-08-08)*
Subscribing 1,200 registry addresses to `userFills` on one socket: 15 accepted,
1,185 rejected with `Cannot track more than 15 total users`. A 2,000-wallet
registry would need ~146 sockets. This killed the planned per-wallet fill
capture outright — the tape finding above replaced it. REST `userFills` has no
such cap; it is bounded by request weight only.

**Liquidations are visible from the counterparty, not the victim.**
*(2026-08-09)* A liquidation fill carries a `liquidation` object naming
`liquidatedUser`, `markPx` and `method` (`market` | `backstop`) — on the fill of
whoever took the other side. The liquidated wallet's own fill merely has a `dir`
beginning `"Liquidated"`. Across 117,485 fills from 70 wallets: **one** had a
`Liquidated` dir, while **16 of 70 wallets** had taken the other side of
someone's liquidation. So the cheap observer set is a handful of high-volume
wallets that absorb forced flow, not the whole registry. No HLP vault address
was needed.

**`startPosition` is on `userFills` only.** *(2026-08-09)* Not on the public
tape. So tape-based position reconstruction has **no per-fill checkpoint** and
cannot verify itself; drift is caught only by reconciling against the next
sweep. This corrected an earlier claim of mine that reconstruction was
self-verifying.

**HL publishes both mark and oracle.** The oracle is CEX-derived, so
`premium = (mark − oracle) / oracle` is a native, exact measure of global
pressure. This is what makes the single-venue design defensible and what
removed the planned Binance sidecar.

## Coverage and population

**The leaderboard exposes 41,392 wallets** *(2026-08-07)*, not the ~1,000 the
existing skills assume.

**Equity and volume select different populations, and both are needed.**

| seed (top 2,000) | share of venue OI | fraction holding any position |
|---|---|---|
| account equity | 61% | 28% |
| weekly volume | 39% | 46% |

Equity finds whoever holds size — the map. Volume finds whoever trades — the
skill cohort. One seed for both jobs gets one of them wrong.

**Coverage denominator correction.** *(2026-08-07)* HL's `openInterest` counts
each contract once, so venue-wide position notional is **2× OI notional**. The
headline "69% coverage" used the naive denominator and was wrong by a factor of
two. Real figures: **BTC ~30%, ETH ~36%, SOL ~27%.** Still unverified against
HL docs and still the single number the map is judged by.

**Polling the registry costs a full sweep of the budget.** *(2026-08-07)*
3,596 wallets: ~5 minutes wall-clock, 15,390 rate-limit waits, 6,041 s of
accumulated sleep. Polling frequency would have become the map's *resolution*.
This made reconstruct-from-tape mandatory rather than an optimisation.

**The liquidated population is not the mapped population.** *(2026-08-08)*
Of 48 observed liquidations: **2.9% of liquidated wallets** were in the
registry, but **21.9% of liquidated notional** was. The registry misses the long
tail of small forced exits and catches a fifth of the size — so per-position
scoring is not obviously dead, since it lives on the notional number.

*Confounded, and not yet believable:* `mapped` reads 0.0% partly because a
liquidated wallet no longer holds the position, so a snapshot taken afterwards
had nothing to predict. Separating "different populations" from "we looked too
late" needs the snapshot-then-observe cycle, not more analysis.

## Cascades, and the first look at whether fading one pays

**A wider observer set finds 6.5× more liquidations.** *(2026-08-13)* 200 observers,
53 productive, yielded **1,289 events** spanning 2024-08-11 → 2026-08-13, $26.3M
of notional. Median event is **$2,000**; p90 is $38,346. The observer count is
the binding constraint on census size, not the registry.

**Cascades are rare and power-law distributed.** Bucketed into one-minute
windows: **52 windows ≥ $25k, 12 ≥ $100k, 3 ≥ $1M.** The single largest is
$13.2M across 305 events (2026-08-10 17:00).

**The largest cascades are in instruments the design excludes.** That $13.2M
window is `xyz:BRENTOIL`, a builder-deployed perp, and builder-deployed names
dominate the event count (BRENTOIL 309, CBRS 91, CXMT 44) against BTC 190 and
ETH 181. `DESIGN.md` excludes builder-deployed perps by default. That exclusion
may be discarding exactly the venue's most cascade-prone instruments, and it is
now an open question rather than a settled default.

**One backstop liquidation, out of 1,288.** The first non-`market` method
observed. Even the $3.8M ETH minute was absorbed by the book unaided — so the
liquidity to take the other side of forced flow is already there, and whoever
supplies it is doing so passively.

**First reversion measurement: the fade does not clear cost.** *(2026-08-13)*
Windows ≥ $100k, builder-deployed excluded, signed against the forced flow so
positive means the fade wins:

| horizon | median | mean | wins | n |
|---|---|---|---|---|
| +5m | +2.4 bps | −0.1 | 2/3 | 3 |
| +15m | −2.2 bps | +4.5 | 2/5 | 5 |
| +60m | −0.3 bps | −9.5 | 2/5 | 5 |

Against a measured ~11 bps round trip, **nothing clears**. The two material ETH
cascades were both preceded by declines and continued down afterwards (−48 and
−72 bps at 15m and 60m on the larger); the snapback the magnet thesis predicts
is not visible in them.

*This is n = 5 and settles nothing.* It is recorded because it is the first
evidence in either direction, and because it points *away* from the branch that
had the better mechanism story. Two known weaknesses: the direction is proxied
by the pre-event return because liquidation side is not persisted, and three of
eight qualifying windows were unreachable because their candles had expired.

**Venue history caps, confirmed.** *(2026-08-13)* ~5000 bars per interval, as
the sibling project measured independently: **1m reaches 3.6 days**, 5m 17.5,
15m 52.2, 1h 208.4. So a cascade's minute structure is unrecoverable after
~3.5 days and any cascade older than ~7 months is invisible at any resolution.
This is what makes running capture urgent rather than merely important — the
data is not delayed, it is deleted.

## Liquidation mathematics

**Our derivation does not reproduce HL's published `liquidationPx`.**
*(2026-08-07, 1,074 cross positions)*

| variant | median relative error | exact (<1e-4) |
|---|---|---|
| `marginSummary.accountValue` | 2.8e-2 | 45.5% |
| `crossMarginSummary.accountValue` | 3.5e-3 | 48.7% |
| accounts holding no isolated margin | 1.8e-11 | 58% |

`crossMarginSummary` is correct (isolated margin is not available to cross
liquidation), but the residual is **unexplained**. Prime suspect: size-tiered
maintenance margin on large positions. Consequence: **HL's published value is
the source of truth**, and the derivation exists only to carry positions
forward between sweeps, with its error measured and disclosed on every map card.

## Defects found by running the system

Each was found by live operation, not by review.

| defect | consequence | status |
|---|---|---|
| WORM appended to an already-manifested file on restart | a restart became indistinguishable from tampering | fixed — restarts open a new part |
| `replace_positions([])` on a fully failed sweep | snapshots 4–6 **destroyed the map**; hours of measurement read an empty table while reporting success | fixed — a sweep that learned nothing refuses to publish |
| `WeightBudget` was per process | `gate map` 429'd immediately after `liq scan`; HL limits per IP | fixed — spend journalled to SQLite |
| Errors counted, never attributed | 2,298 poll failures and 2,177 sweep failures with the reason discarded | fixed — `core/errors.py` |
| Bar `available_at` could precede its close by 42 s | a feature could read a bar that had not finished forming | fixed — availability is `max(close, last arrival)` |
| `read_records` crashed on a half-written tail | replay died while capture was mid-write | fixed — unterminated tail skipped, terminated corruption still raises |
| Map defaults showed 8 rows at 0.25% | hid the **largest** cluster ($4.4M at −8.44%; $75.9M at −11.05% further out), and rescaled every bar to a false maximum | fixed — resolution/span/depth are options, truncation is reported |
| `nat2` not on PATH; data paths relative to cwd | from `/tmp`, `nat2 wallets status` reported an **empty registry as fact** | fixed — installed default + resolution reported on every command |
| Project marker was "any `data/` directory" | matched a stray `/tmp/data` left by an unrelated command | fixed — `.nat2` marker or a nat2 `pyproject.toml` |

## Operational

**Long-running capture degrades.** *(2026-08-09)* After 19.5 hours: 1,737
websocket reconnects and 2,298 poll errors — a 49% failure rate on
`metaAndAssetCtxs`, which produced 846 context records where ~4,700 were
expected. Invisible at the time because errors were counted, not attributed.
A restart on the same code gave 0 errors and 0 reconnects over the following
five minutes, so this is accumulated process state rather than a standing API
problem. Cause still unknown.

**Clock skew flips sign between runs.** Median ingest lag was −196 ms in one
capture and +395 ms in the next. Negative means HL's timestamp is *ahead* of
our receipt clock — the condition that voids the `t_ingest` guarantee. Well
inside the 2 s tolerance, but a ~600 ms shift over minutes is unexplained: NTP
discipline or HL timestamp semantics.

## Modelling

**The Stage A label degenerates: the race never runs.** *(2026-08-09, BTC)*
Positive rate **0.7%** over 291 labelled rows at a 2h horizon, 0.0% at 30m. The
cause is not a coding error but a mismatch the design never addressed: mapped
clusters sit ~0.7–1% from the mark, while BTC one-minute realized vol is ~1.3bp,
so a two-hour window offers roughly 14bp of travel against a 70bp target. That
is a five-sigma move, so virtually every outcome is a **timeout**, which the
binary label records as 0 — indistinguishable from "the opposite barrier won".

**Fixed 2026-08-11.** Timeouts are excluded rather than recorded as misses --
the race never finished, so it is not a negative answer -- and targets are
gated on reachability (`max_reach_sigma`), so a cluster the horizon cannot
traverse is never raced. Positive rate went from **0.7% to 51.8%**; the race
now runs. Of 2,885 BTC rows at a 2h horizon: 1,603 out of reach, 168 timeout,
762 labelled.

**And the fixed label finds nothing.** magnet_a log loss 0.7099 against a
baseline of 0.7017 and a constant-predictor floor of 0.6885 -- worse than both.
It does not beat its baseline and does not clear the floor.

The three options as originally written:

- Pick the horizon per row from the distance, so `sqrt(h)·sigma ≈ distance`.
- Restrict targets to clusters within a few sigma of what the horizon allows.
- Separate timeout from opposite-barrier in the label rather than collapsing
  both to 0, so the degenerate case is visible in the data instead of inferred
  from a base rate.

Related and confirmed working: at a 6h horizon the purge removes every training
row and the evaluation reports **0 folds** rather than producing a result from
what is left. Ten hours of tape cannot support a six-hour label.

**The map does not call the side.** *(2026-08-10, first answerable run)*
`gate map` predictive, scored against the snapshot that predated each event:
**side hit rate 42.8% over 208 liquidations, against a 50% coin flip.** Median
distance from the mark 0.31%. Set aside: 127 with no map for that coin, 86
predating the first snapshot, 253 with a map staler than five minutes.

Below chance, not merely unimpressive. On this sample the claim "forced flow
arrives on the side where the map put the mass" is not supported.

*A correction that matters more than the number.* An earlier version of this
check scored whether a liquidation landed within 1% of the nearest mapped
cluster price, and reported **82.1% against a 20% chance rate, lift 4.11x** —
which passed the gate. That measurement was close to tautological: clusters are
built from wallets' liquidation prices, so liquidations occur near them almost
by construction, and the "chance" model of a uniform price over ±10% was not
the right null. The side-based test with a coin-flip baseline replaced it and
reverses the verdict. The flattering number was mine; the sceptical replacement
was already in the repo.

## Unexplained

- **An unattributed write into the append-only store.** *(2026-08-09 12:53:54)*
  A one-coin `nat2.liqmap` snapshot appeared 39 s before the first deliberate
  one. The cycle daemon has no `mapsnap` job row and the tests write only to
  temp directories, so nothing that ran accounts for it. The record is valid, so
  nothing is corrupted — but an unattributable write into a WORM store is
  exactly what this design refuses to shrug at.
- The liquidation-formula residual (above).
- The capture degradation cause (above).
- The clock-skew sign flip (above).
