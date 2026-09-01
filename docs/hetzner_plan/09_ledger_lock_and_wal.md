# 09 — Ledger lock, registry WAL (T4)

**Effort** 2 h · **Blocks** 18 · **Status** done, 2026-08-30 · **Branch**
`fix/ledger-lock-and-wal`

## What
`Ledger.append` was an unguarded read-modify-write: it re-parsed the chain,
derived `seq = len(existing)`, then opened `"a"` and wrote. Seven production
appenders exist, plus gapwatch as a **subprocess**, so the lock had to be
inter-process.

Two writers reading seq N both emit seq N, and `verify()` then declares the
chain broken from there **forever**. Reproduced: 12 of 12 trials; 0 of 12 with
the lock.

## How
An exclusive `fcntl.flock` on the ledger's own inode, non-blocking with a 10 s
deadline, raising `LedgerBusy`. The whole read-modify-write happens inside it:
`"a+"`, `seek(0)`, parse off the locked handle, write, `flush` + `fsync` before
release. `entries()` keeps its contract and stays lock-free.

`LedgerBusy` is caught at both named sites. `cycle.py` records the loss instead
of dying. `guard.record` does **not** swallow it — it writes an `unrecorded` L2
action to the lock-free action log and re-raises.

Registry gets `journal_mode=WAL` and an explicit 10 s busy timeout.

## Verify
```
uv run pytest -q                    # 682 passed
sqlite3 'file:data/registry.sqlite?mode=ro' 'PRAGMA journal_mode;'   # wal
nat2 log verify                     # chain intact
```
Two processes appending 25 entries each → 50 entries, seqs `0..49`, chain
intact. The test fails on `46584b4` on its seq assertion (`[0, 0, 2, 2, ...]`),
not on an import error, because `LedgerBusy` is imported inside the test bodies.

## Done when
Done. Five spec errors were found by building it:

- **"No new constants … already in `ratelimit.py`"** — they are *inline
  literals* (`:166`, `:187`), not importable constants, and `nat2.ledger` has no
  dependency on `nat2.hl`. Restated locally as named constants; the numbers are
  unchanged, so nothing needed pre-registering.
- **The cycle failure is one unit restart per scan pass (1 h), not a crash
  loop.** `job.started()` persists `last_run_ns` before the crash site.
- **"Catch `LedgerBusy` at `guard.py:38`" cannot mean swallow.** Thirteen — not
  fourteen — `record()` calls return a `Verdict` built from the entry, so a
  swallowed append would print PASS for a verdict `require` will never find.
- **The sqlite3 driver default busy timeout is already 5 s**, not zero. The
  change is 5 s → 10 s.
- **`liqscan.candidate_observers` opened its own bare connection**, so the busy
  timeout would not have reached it.

An adversarial review raised 13 candidate findings across five lenses; all 13
were refuted on inspection.

One consequence to carry forward: **applying the diff does not close the race
until the appender restarts.** `nat2-cycle` imports `nat2.ledger.chain` at
module scope and its unit has no `RuntimeMaxSec`, unlike capture's 5 h recycle.
It was restarted deliberately on 2026-08-30 — cycle writes only derived streams,
so it costs no tape continuity. See `10` next.
