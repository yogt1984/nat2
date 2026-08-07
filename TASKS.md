# TASKS

Next steps, ordered. Each item names what it unblocks — anything that doesn't
unblock a gate is not on this list.

Status: M0 done (`gate feed` PASS). M1 built, `gate map` FAIL on `predictive`.

---

## 1. Identify the backstop liquidator — unblocks `gate map`

The public trade tape has no liquidation marker; `userFills` has one (`dir:
"Liquidated…"`) but caps at 15 tracked users, so it cannot cover the registry.
The tractable path is the counterparty: HL's backstop liquidations are absorbed
by the HLP vault, whose address is public.

- Find the vault address from flow — the liquidator should appear as a
  counterparty on a wildly disproportionate share of prints. Confirm against
  HL's published vault list rather than inferring alone.
- Label prints against it as liquidations; everything else stays unlabelled.
- Cross-check on a 15-wallet sample via `userFills` `dir`, which is the only
  ground truth available. If tape-labelling disagrees with it, the tape method
  is wrong and this task is not done.
- Falls out for free: `hlp_delta`, the cascade-absorption feature M2's
  `magnet_b` needs to detect exhaustion.

Then `gate map`'s `predictive` check becomes answerable: do realized
liquidations land where the map said, at what hit rate.

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
