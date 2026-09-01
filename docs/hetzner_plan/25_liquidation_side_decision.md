# 25 — Liquidation-side decision (T9 vs T10)

**Effort** 1 day, whichever is chosen · **Status** todo — **decide first**

## What
Three answers to one question are in flight, which is exactly the contamination
the admission rules exist to prevent. Declare which artefact the detector reads,
pre-register the derivation, and make the others read it. **Do not land both.**

- **T9** stores the observer side on `registry.liquidations` — seven additive
  columns, plus a `scan_runs` table for the per-observer yield the scanner
  computes and discards.
- **T10** derives it from the tape's users index into a derived parquet, never
  touching the registry.

## How
The premise that split them is false: over all **1,101 matched rows** the users
index and the venue's own `side` field agree perfectly. T10 is a measurement, not
a schema position.

If T9: append fields in an order that keeps all four positional call sites
working, and fix `test_registry_round_trips_every_field`, which compares a
readback and will fail.

## Verify
For 2026-08-27: **572 events, 320 matched, 299 short / 21 long**, classes summing
to the event count on every row.

## Done when
One artefact exists, the other is explicitly closed, and the registry's hashes
are unchanged.
