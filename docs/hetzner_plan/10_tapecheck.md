# 10 — tapecheck (T7)

**Effort** 5 h · **Blocks** 18, 19, 20, 21 · **Status** done, 2026-08-30 ·
**Branch** `feat/tapecheck`

## What
The artefact that can answer "what did the tape lose, and why". gapwatch answers
only "is it live now": its counter is wiped on the Monday roll, it reconstructs a
hole only when its own tick ran ≥900 s late, and its detector is hardwired to
`hl.trades`.

**Land this before 19.** tapecheck owns `holes(entries, since, until, min_s,
stream=None)`; the others import it.

## How
`deploy/tapecheck.py`, stdlib-only, verified under `env -i /usr/bin/python3` with
no venv on the path. Per stream: holes over the floor, seq breaks, orphans,
parts-per-hour, spacing. Causes from the journal, resolved in precedence order
`host-pause > recycle > stall-exit > local-network > venue`.

A hole is `prev.last_ingest -> next.first_ingest`. Of the nine pairings of the
manifest's three timestamps only this one is meaningful — the others give 8, 223
or 225 holes, and `closed_at` is stamped *after* the successor begins ingesting,
so it yields negative gaps.

## Verify
```
python3 deploy/tapecheck.py --since 2026-08-20T00:00:00Z \
  --until 2026-08-28T14:00:00Z --json
```
hl.trades: **26 holes, 1251.2 gap-minutes**, every one classified, zero
`unknown` (host-pause 17, local-network 4, recycle 1, stall-exit 3, venue 1).
Writes nothing: no `open(...,'w')`, no `mkdir`, no `unlink` anywhere in the file,
so the store's checksums cannot change.

## Done when
Done. What building it found:

- **The floor is not in the code, and the acceptance test cannot validate it.**
  Every floor in (46.368, 101.726] yields the identical 26 / 1251.236, so a
  number chosen here would look measured while being unfalsifiable. It is read
  from a ledgered `tapecheck_v1` pre-registration carrying `hole_floor_s`, and
  the tool **refuses** without one — the rule 14 sets for `keep_daily`.
  **This is the one thing still owed: the pre-registration does not exist yet.**
- **The "6,461 phantom holes" are `nat2.liqmap2` alone**, and the figure is
  literally its part count in the window. Snapshot streams open and close a
  writer per snapshot, so the gap between parts *is* the cadence.
- **A stream absent for the whole window read as the healthiest one.** Holes are
  measured *between* parts, so zero parts gives zero holes: `hl.l2book` showed
  `0 holes, 0.0 gap-min` and exit 0 across an 84 h absence while live streams
  showed ~1160 gap-minutes. Absence is now its own finding and its own exit code.
- **`unknown` was unreachable.** A bare `\b5\d\d\b` for HTTP 5xx matched PIDs and
  byte counts — an ordinary journal hour holds 546, 565, 555, 545, 515 — so
  `venue` absorbed everything. It is anchored to an exception name now, and bare
  `RuntimeError` is out of the venue vocabulary.
- **`journalctl --since` parses in LOCAL time** even with `--utc` output, and this
  box is Europe/Rome. The tool passes `@<epoch>`; a two-hour shift would have
  classified every hole against the wrong journal.
- **20 orphan parts sit on disk** unmanifested (0 manifest entries lack a file).
  83.4 of hl.trades' 1251.2 gap-minutes are still on disk — reported separately
  and never subtracted, because an orphan is unproven rather than absent.

An adversarial review raised 54 findings across five lenses; 27 survived
refutation and the substantive ones are fixed above. See `11` next.
