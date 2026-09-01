# 12 — Capture shared budget (T2)

**Effort** 1 h · **Status** done, 2026-08-30 · **Branch** `feat/capture-universe-retry`

## What
`cli.py` built `Capture(config, on_status=_print_status)` and never passed
`budget=`, so the daemon fell back to a private in-memory account. The 10 s poll
is 6/min × weight 20 = **120 of the 1,200 weight/min per-IP ceiling, permanently
invisible** in `data/ratelimit.sqlite`. Every other command already passed
`_budget()`; capture was the sole omission.

The cost is not mis-reporting. The sweep and the gates admitted themselves as if
all 1,200 were free while the venue saw up to 1,320/min.

## How
One keyword — `budget=_budget()`. `SharedWeightBudget` shares the base class's
whole public surface, so it is a drop-in.

## Verify
```
sqlite3 'file:data/ratelimit.sqlite?mode=ro' \
  "SELECT COUNT(*), SUM(weight) FROM spend WHERE ts > strftime('%s','now')-60;"
```
Parent build: `0|0` — confirmed on the live box before the change. This build:
rows appear at the poll cadence.

## Done when
Done. The spec's warning checks out, and the number is now derived rather than
quoted: `snapshot.py` reserves `SWEEP_RESERVE_FRACTION = 0.6` of the ceiling
(720) for `LEASE_TTL_S = 120 s`, leaving `max(180, 1200-720) = 480`, so capture's
poll is refused once global spend exceeds **460**.

That is safe, and worth writing down because it looks alarming:

- **A refusal delays the poll, it never drops it.** `acquire_async` loops on
  `_try` and sleeps; the sleep is bounded by the 60 s window.
- **The worst delay is ~60 s against a 10 s poll interval and a 300 s stall
  watch**, so a sweep cannot make capture stand down.
- The alternative — the status quo — is worse: the venue seeing 1,320/min
  against a 1,200 ceiling risks 429s for every process on this IP.

Whether capture should hold a *reserved* share rather than compete for the
remainder is a ledger decision, not part of this diff.
