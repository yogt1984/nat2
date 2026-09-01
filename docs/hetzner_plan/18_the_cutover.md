# 18 — The cutover

**Effort** 2 h, one sitting · **Depends on** 07, 09–14
**Status** todo · **The only irreversible step**

## What
Move the tape, registry and hash chain to the VM, and make it canonical.

## How
The order **is** the task. `Ledger.append` derives `seq` from a re-read, and
seven writers append. If su-35 appends once after the copy, both chains hold a
different entry at the same seq, both verify clean locally, and the divergence
is unrecoverable.

1. **Stop every writer on su-35**: cycle, gapwatch, statuspage, evlog, capture.
2. `nat2 log verify` → chain intact. Record byte sizes.
3. `sqlite3 ".backup"` the databases — never `cp`.
4. rsync `data/raw`, then ledger, actions, events, ops, `pairs.toml`.
5. On the VM: `log verify`, then `tape compare` between the two roots.
6. `restic backup` the landed baseline before anything writes to it.
7. Append the two pre-registrations as the **first** VM writes.
8. `systemd_units.py install primary`; watch the first hour by hand.
9. Disable `nat2-cycle` on su-35 permanently.

## Verify
Step 5 shows zero differing hours; a difference means the copy is wrong.

## Done when
The VM captures and su-35's chain is a dead branch.
