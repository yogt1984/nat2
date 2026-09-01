# 14 — Backup code (T13)

**Effort** 1 day · **Depends on** 09 · **Blocks** 18, 20 · **Status** code done,
2026-09-01 · **Branch** `feat/backup` · **Acceptance blocked on 15**

## What
There was **zero** backup code: `grep -rn backup` returned a README line, a
docstring, and `report.py:216`, which hardcoded "none configured". The asset is
not regenerable — now **22,736 parts and 4.62 GB** (the spec said 17,000 / 3.9
GB), and the venue serves no historical replay for trades or the book.

## How
`deploy/backup.py`, stdlib-only on `/usr/bin/python3` — a backup tool that needs
the venv cannot run on the day the venv is what broke. Four subcommands:
`stage`, `snapshot`, `verify --restored PATH`, `status`.

`keep_daily` and `spot_check_n` come from a ledgered `backup_v1`
pre-registration and the tool **refuses** without one.

## Verify
```
python3 deploy/backup.py verify --restored <tree>
```
Against the live store: **20/20 parts matched their sha256** across all five
streams (`hl.trades 8, hl.l2book 5, hl.assetctxs 3, nat2.liqmap 2,
nat2.liqmap2 2`), chain intact, in 0.52 s.

## Done when
**Code done; acceptance is not.** The done-when is "two independently
restorable copies before the count starts", and the second is the Storage Box
from `15`, which needs the Hetzner account. `restic` is not installed here, so
`snapshot` refuses rather than pretending. What building it established:

- **A `cp` of the WAL registry does not give a stale database — it gives one
  with no `positions` table.** Reproduced directly: the schema itself is still
  in the `-wal`. A restore from that reports zero positions as though that were
  the answer, which is the exact failure `io/snapshot.py` exists to prevent.
  Staging uses `sqlite3.Connection.backup()`: stdlib, consistent against a live
  writer, and it produces one self-contained file with no sidecars to lose.
- **The registry is irreplaceable, not merely expensive.** `sweep()` and
  `scan()` write their raw responses nowhere in the WORM store, and `liqscan`
  reads `userFills`, capped at 2,000 per wallet — so its 12,596 liquidations
  cannot be re-fetched once they scroll out. Losing that file loses the
  dependent variable of the magnet hypothesis.
- **The manifest is not a file list.** Twenty parts are on disk and absent from
  it, two of them holding 46,379 records. A manifest-driven selector would drop
  them and the seq range `_resume_seq` needs, so a restored store would resume
  numbering over a hole. Walk the tree.
- **A uniform spot-check checks almost nothing.** The websocket streams are
  4.7% of parts and **90.3% of the bytes**; a uniform draw of twenty misses at
  least one of them in >90% of trials. Stratified: a floor of 2 per stream,
  remainder by bytes.
- `report.py:216`'s hardcoded string can now read `data/ops/backup_state.json`,
  which carries an age — the number `22`'s five-minute daily loop asks for.

Three test fixtures had to be rebuilt before they proved anything: a repetitive
pad compressed away and inverted the byte skew; three parts per stream left the
weighting nowhere to go; and a fixture small enough to write to disk cannot
reproduce a 4.7% ratio, so the uniform comparison is built from the real
proportions instead. All three discriminating tests were run against reverted
code and fail there.

See `15` next — and note `18` step 6 needs a restic repo that `15` creates,
which its `Depends on` line does not say.
