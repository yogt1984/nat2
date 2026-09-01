# 20 — Clean-days and pre-registration (T8)

**Effort** 1 day · **Depends on** 10, 14 · **Blocks** 22 · todo

## What
The acceptance bar is "7 consecutive clean UTC days" and **nothing can compute
one**. The only durable counter is gapwatch's weekly dict, wiped on the Monday
roll, with no per-day breakdown. A number invented after the migration would be
fitted to whatever the box happened to do.

## How
A pure module scoring each complete UTC day against eight criteria whose
thresholds live in a ledger entry, refusing with `preregistration_missing` if it
is absent or a number moved. You append the payload.

**Sequencing trap:** two criteria depend on things that do not exist yet — the
backup sidecar (14) and the gates timer, rendered on 08-22 and never installed.
Without both, every day scores dirty and the count can never start.

Three numbers to settle in the entry: `min_scans_per_day` 24 or 23; map-gap
fraction 0.05 or 0.01; whether a refused sweep makes the day dirty.

## Verify
Against the live store: **0 clean days of 8**, streak 0, with 2026-08-23 failing
exactly three criteria.

## Done when
That reproduces, and without the entry it refuses, writing nothing.
