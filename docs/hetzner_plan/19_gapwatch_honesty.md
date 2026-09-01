# 19 — gapwatch honesty (T5)

**Effort** half a day · **Depends on** 10 · **Status** todo

## What
Three faults in the only durable uptime counter nat2 has.
`unit_active_since_s` mixes CLOCK_MONOTONIC with CLOCK_BOOTTIME — they differ by
**7,501 s** here, putting the reconstructed restart 125 minutes before the truth.
The reconstruction runs only when the watchdog's own tick was ≥900 s late, so
every outage it survived is invisible: the state books **62.3 min for ISO-W35
against 758.4 min** in the manifest. And `nat2.liqmap2` is not in the cadence
table, so 210 MB of snapshots go unwatched.

## How
Wall-clock restarts from `systemctl show --timestamp=unix`. Manifest holes booked
on **every** tick, idempotent by start time, importing 10's function rather than
writing a second one. liqmap2 at **300 s**, not 3600 — the measured median gap
is 62.9 s.

Two corrections: the dedupe must recompute absolutely or the same outage is
booked twice; and the guard must scope to the gap-minutes increment, or it
suppresses the page and the ledger incident too.

## Verify
The booker reports ≥750 min for W35 where the state file holds 62.3.

## Done when
That, and the status-page golden is unchanged.
