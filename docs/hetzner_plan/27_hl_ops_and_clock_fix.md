# 27 — hl.ops and the clock fix (T16)

**Effort** 2 days · **Depends on** 22, 25 · todo

## What
Two things the research layer needs.

**A.** A derived WORM stream carrying what only the process knows: the coin set
and git sha at start, reconnects and their reason, poll errors, a periodic clock
and venue status, the stop cause. It lets a hole be classified
venue-vs-host-vs-local **from the tape alone**, and marks backlog replays so
duplicate tids can be deduped on read.

**B.** The frame keys event features on `t_event` while its docstring promises
arrival time. **593 of 11,035 liquidations (5.37%) arrived over two hours late**.
Every frame computed before this embeds lookahead.

## How
An empty channel keeps the stream out of the subscription builder, and the
capture unit passes no `--streams`, so it inherits the new default with **no unit
edit**. No header record inside the trade parts: that would change the payload
contract every reader assumes.

## Verify
A feed audit's verdict is unchanged by the stream's presence on the same window;
the leak regression fails on the parent commit.

## Done when
Both, with `--legacy-clock` so old numbers stay reproducible.
