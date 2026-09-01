# 24 — nat Rust reconnect (T14)

**Effort** 1 day + ~6 min per rebuild · **Status** todo · the one unaudited spec

## What
Three faults, one cause: nat treats a transient network failure as fatal.

A single failed reconnect exits the process — **34 exits in 8 days**, each
blanking 49 columns for up to an hour. The initial-connect loop sleeps a flat
2 s, bypassing the existing backoff: 90 attempts/min against a documented 30 per
minute per IP. And the REST client bails on non-2xx with no backoff — **2,781
429s in 7 days**.

## How
**Keep the `?` at all four sites**; put a bounded retry budget in front of the
exit, so a 30-second outage stops costing a restart while a dead link still
escalates. Drive the initial connect from the same backoff: 2, 4, 8, 16, 30.

Put the token bucket behind `Arc`: the client derives `Clone`, so plain fields
give every clone its own budget and the limit does nothing.

## Verify
24 h with zero "symbol task died" and zero "Clearinghouse request failed"; row
median per file within [17,800, 18,300].

## Done when
That holds seven days on its own box.
