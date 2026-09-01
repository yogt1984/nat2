# 14 — Backup code (T13)

**Effort** 1 day · **Depends on** 09 · **Blocks** 18, 20 · **Status** todo

## What
There is **zero** backup code today: `grep -rn backup` returns a README line, a
docstring, and `report.py:216`, which hardcodes "none configured". The asset is
not regenerable — 3.9 GB over ~17,000 parts, and the venue serves no historical
replay for trades or the book.

## How
Three pieces. The interim nightly rsync pull to the laptop, live from day one,
no repo code. Then `deploy/backup.py`: a nightly restic snapshot and a weekly
restore-verify that restores to scratch, runs `nat2 log verify` on the restored
ledger, and re-hashes 20 manifest parts stratified across all five streams — a
uniform draw misses the sparse ones ~93% of the time.

Stage the databases first: `sqlite3 ".backup"`, never `cp`. After 09 lands, a
raw copy without the `-wal`/`-shm` sidecars is guaranteed torn.

`keep_daily=90` and `spot_check_n=20` are **policy**, not repo constants — the
tool refuses until they are on the ledger.

## Verify
One restore-verify run exits 0 with 20/20 parts matching their sha256 after a
real restore.

## Done when
Two independently restorable copies exist before the count starts.
