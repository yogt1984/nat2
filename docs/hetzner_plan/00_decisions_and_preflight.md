# 00 — Decisions and preflight

**Effort** 1 h · **Blocks** everything · **Status** todo

## What
Settle four decisions that four later tasks silently depend on, and check the
one fact that would invalidate the order.

## How
Write the answers into this file, then proceed:

1. **Capture roster.** There is no ledgered roster — the ledger holds zero
   `roster` entries, and `--roster` re-ranks against live venue volumes at every
   start. Freeze an explicit `--coins` list from one `roster apply` entry (this
   also means editing the unit's ExecStart), or keep `--all` and drop the
   reproducibility claim.
2. **Hole floor.** One number, pre-registered as `tapecheck_v1`. A real restart
   seam measured 59.82 s, so 60 s leaves 0.18 s of margin.
3. **Venue outage vs the streak.** Declare it `undecidable`, not `dirty`, before
   any day is scored.
4. **The 64 planted lines** in `data/actions.jsonl` — purge with a ledgered
   incident, or annotate and keep.

## Verify
```
getent ahosts api.hyperliquid.xyz      # A records only, no AAAA -> IPv4 is mandatory
```

## Done when
All four answers are written down and the venue resolves over IPv4.
