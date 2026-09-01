# 22 — The seven-day count

**Effort** 7 days wall clock · **Depends on** 18, 19, 20, 21 · **Status** todo

## What
Accumulate the evidence the whole migration is judged on: seven consecutive
clean UTC days against the ledgered definition, then keep counting to fourteen.

## How
Day 1 is the first **complete** UTC day after the cutover — say so out loud, so
nobody later back-dates it.

Run the five-minute daily loop and **time it**. If it exceeds five minutes, cut
items: an unread dashboard is worse than none, because it manufactures
confidence.

From day 2, **no non-critical capture restart**. Every restart is a seam in the
record being accumulated.

Do the first restore drill while there is slack, not on day 12.

## Verify
Daily: the streak, tapecheck's cause taxonomy, `nat2 log verify`, the dead-man
ping, backup age, disk headroom.

## Done when
The streak reads 7 and an observation records it verbatim.

If it broke, tapecheck says which day and why: fix the cause, restart from zero,
**and do not renegotiate the definition**. That renegotiation is the likeliest
way this produces a number nobody can defend.
