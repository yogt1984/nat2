# 19 — gapwatch honesty (T5)

**Effort** half a day · **Depends on** 10 · **Status** done, 2026-09-02 ·
**Branch** `feat/gapwatch-honesty`

## What
Three faults in the only durable uptime counter nat2 has. All three re-measured
before the fix, and two are worse than the plan recorded.

## How
**Wall-clock restarts.** `unit_active_since_s` added `ActiveEnterTimestampMonotonic`
to a boot time derived from `/proc/uptime`. CLOCK_MONOTONIC does not advance
across suspend and CLOCK_BOOTTIME does, so the two drift by however long the box
has slept — the plan measured 7,501 s; **today it is 908 minutes for
nat2-capture** and 42 for nat2-cycle. Now read straight from
`systemctl show -p ActiveEnterTimestamp --timestamp=unix` (systemd 255 here,
needs ≥247).

**Every hole, not only the ones it slept through.** The reconstruction ran only
when the watchdog's own tick was ≥900 s late, so outages it *survived* were
invisible. `book_holes` runs each tick over the current ISO week, importing
`holes()` from task 10 rather than keeping a second definition — which is why 10
had to land first. Idempotent on the hole's absolute start in nanoseconds; an
offset from "now" is a new key every tick and books one outage repeatedly. The
dedupe set is cleared with the counter it guards on the Monday roll.

**The cadence table.** Re-measured over the last 400 parts per stream: the ws
streams still rotate hourly (3599.8 / 3598.0 / 3596.1 s), but `nat2.liqmap`
lands every **64.5 s**, not 3600 — the entry was true when liqmap was written
once per scan pass and stopped being true when `mapsnap_interval_ns` became
60 s. 56× too slow, so the 18-hour liqmap outage paged after two hours instead
of ten minutes. Both snapshot streams are now 300 s, and `nat2.liqmap2` is in
the table at all for the first time (210 MB previously unwatched).

## Verify
```
uv run pytest tests/test_gapwatch.py -q          # 11 passed
```
The booker reports **1,817.2 min for hl.trades in 2026-W35** where the state
file holds 62.3 — a 29× under-count corrected, against the plan's ≥750 bar.
The status-page golden is unchanged.

## Done when
Done. One fault found while building, which the plan did not anticipate:

- **Booking iterated the cadence table**, which now includes the snapshot
  streams — and their 64.5 s cadence clears the 60 s floor, so *every ordinary
  gap booked as a hole*: **7,512 phantom holes and 10,077 phantom minutes per
  snapshot stream for a single week**, which would have buried the 1,817 real
  ones. Booking is scoped to tapecheck's `CONTINUOUS` instead. The cadence table
  answers "is this stream live"; it must not answer "was that absence a hole".
- A first version passed a directory where tapecheck wants the manifest file,
  and the resulting `IsADirectoryError` was being swallowed by a bare
  `except OSError` into "no holes today", silently and forever. The swallow is
  gone; only a missing file is tolerated.

The floor comes from the ledgered `tapecheck_v1`. Without it nothing is booked
and the watchdog keeps watching — refusing to run would blind the only alarm
over a number that governs bookkeeping. See `20` next.
