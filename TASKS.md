# TASKS

Next steps, ordered. Each item names what it unblocks — anything that doesn't
unblock a gate is not on this list.

Status: M0 done (`gate feed` PASS). M1 built, `gate map` FAIL on `predictive`.

---

## 1. ~~Identify the backstop liquidator~~ — done, and it found a bigger problem

No vault address was needed. `userFills` carries a `liquidation` object on the
**counterparty's** fill naming `liquidatedUser`, `markPx` and `method`, and REST
has no 15-user cap. Measured: 40 observers yielded 48 liquidations from 9
productive observers. `nat2 liq scan` / `nat2 liq list` ship this.

**The finding that matters:** of 48 observed liquidations, **0 were scoreable**
— 24 predated the snapshot (correctly excluded as lookahead) and 24 belonged to
wallets not in the registry. The map is built from wallets holding the most
notional; the wallets that actually get liquidated are small and leveraged. They
are different populations, and per-position scoring may never accumulate.

## 1b. Fix the population mismatch — now the real blocker for `gate map`

Pick one, on evidence:

- **Seed a third registry by liquidation risk** — wallets whose margin ratio or
  leverage puts them near their liquidation price, which is computable from data
  already held. Costs another sweep; directly targets the population that fires.
- **Score at cluster level instead of per-position** — did liquidations land in
  the price buckets the map marked, regardless of whose they were. Weaker claim,
  but answerable with the registry as it stands.
- **Measure the ceiling first** (cheap, do this before either): over a week of
  scans, what fraction of liquidated wallets ever appear in the registry? If it
  is near zero, per-position scoring is dead and cluster scoring is the design.

This also qualifies the map itself: 31% coverage *by notional* may correspond to
far less than 31% of liquidation *events*. Coverage should be reported both ways.

## 2. Run capture continuously — unblocks everything

Nothing downstream can be validated on data we don't own, and `predictive`
needs map snapshots *followed by* liquidation prints. Capture accrues calendar
time; start it before writing more code, not after.

- `nat2 capture hl --all` under a supervisor, restart-safe.
- Snapshot the registry on a schedule (~6h; a sweep costs ~5 min and the whole
  weight budget, so it cannot be more frequent).
- Persist each map snapshot so `predictive` has a before-state to score against.

## 3. Position reconstruction from the tape — completes M1

`features/fills.py` produces signed deltas; nothing yet carries them into the
registry between snapshots.

- Apply deltas to registry positions, tagging carried-forward rows `derived`.
- Reconcile against `clearinghouseState` on a rotating sample and alarm on
  drift — drift FAILs `gate feed`.
- Bootstrap problem: the tape gives *changes*, snapshots give *levels*. A
  wallet is only reconstructable from its last snapshot forward.

## 4. M2 — labels and the magnet experts

Blocked on 1–3 for data, not for code. Buildable in parallel:

- Two-barrier race (Stage A) and triple-barrier on touch events (Stage B),
  evaluated on mark price, with uniqueness weights.
- `magnet_a`, `magnet_b`; LightGBM depth 3.
- Purged walk-forward with embargo `h`, isotonic calibration on OOS folds only.
- `nat2 eval` enforcing the mandatory `baseline()` — must beat `sign(imb)` net
  of hourly funding and fees, or it does not enter the pool.

---

## Verify before coding

Open items, each needing a test that fails loudly if the answer moves:

- **OI convention** — is `openInterest` one-sided? `OI_SIDES = 2` is assumed and
  is a factor of two on coverage, the one number the map is judged by.
- **Liquidation formula** — reproduces HL's `liquidationPx` for only 41% of
  positions (58% where the account holds no isolated margin). Published value is
  used as source of truth, so this is not blocking, but the residual is
  unexplained. Prime suspect: size-tiered maintenance margin on large positions.
- **Mark-price construction** and precisely which price triggers liquidation.
- **Funding** formula, cadence, clamp.
- **Rate-limit weights**; `hl/ratelimit.py` carries a `VERIFIED_ON` stamp that
  `gate feed` warns on after 90 days.
- **WS connection limit per IP** — the 15-users-per-connection cap is measured;
  the connection cap is not, and it bounds any future per-user work.
- **Node data** format, retention, access cost — sizes M3.

## Known gaps

- `hl.l2book` cadence is sparser than the 1s hint implies (45 records / 70s
  across 3 coins). Measure properly before `book_thin` depends on it.
- Clock skew flipped sign between capture runs (−196ms then +395ms). Within the
  2s tolerance, but a ~600ms shift over minutes is unexplained — NTP discipline
  or HL timestamp semantics.
- Branch is still named `feat/m0-capture` and now carries M1 as well.
