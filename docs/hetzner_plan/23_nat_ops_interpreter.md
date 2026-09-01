# 23 — nat ops interpreter (T15)

**Effort** 1.5 days · **Independent** · **Status** todo

## What
`scripts/ops/systemd_units.py:44` bakes `sys.executable` into ExecStart with no
preflight. When `/home/onat/FPGA/.venv/bin/python` vanished on 2026-08-19, three
units began exec-failing and never stopped: **~23,940 restarts each** on the l2
and position samplers, every one `status=203/EXEC`.

WP-2's position panel has accrued nothing for ten days against a 90-day
requirement. This touches no nat2 file and needs no Rust rebuild — do it in an
evening, not after the migration.

## How
A `resolve_python` / `preflight` pair that refuses to render or install a unit
whose interpreter does not exist. Honour an explicit `--python` **verbatim** —
the spec's chain walks past a missing one to `sys.executable`, so preflight would
inspect the substitute and never name the dead path.

Also: an ntfy sink beside the dead Telegram path, and a continuity auditor.

Ship as a rendered diff; you apply it. Nothing restarts a unit.

## Verify
Rendering with a missing interpreter raises and names it. Restart counters are
moving targets, so assert **floors**, not equalities.

## Done when
The three samplers are active and today's position directory appears.
