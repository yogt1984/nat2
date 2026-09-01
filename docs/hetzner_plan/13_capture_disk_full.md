# 13 — Capture disk-full (T3)

**Effort** half a day · **Status** done, 2026-08-30 · **Branch**
`feat/capture-universe-retry`

## What
A write error in the tape path was unobserved. `run()`'s shutdown does
`gather(*tasks, return_exceptions=True)` and never inspects the result, so an
`OSError` from the write or the flush froze the tape with no counter and no
exit — and the watchdog then reported `silent hl.trades`, the wrong diagnosis at
the worst moment. Worse: the poller's write sat inside a broad
`except Exception`, so a full disk turned it into a silent no-op that never
exited.

## How
`_write_failed(stream, exc)`: sets state, calls `stop()`, never raises — the
watchdog's own shape, working because `run()`'s `finally` appends the manifest
entries. `CaptureWriteFailed` is distinct from `CaptureStalled`: a stall says
nothing arrived, this says something arrived and could not be kept.

## Verify
```
uv run pytest tests/test_capture_disk_full.py -q     # 7 passed
```

## Done when
Done. Three things measured rather than assumed:

- **zstd buffers, so the flush is the only realistic discovery path.** 200
  records produce **zero** underlying writes; the fd is first touched at
  `FLUSH_FRAME`. Guarding the per-record write is defensive; guarding the flush
  is the fix, and that is what moves detection from 300 s to the 30 s tick.
- **`close()` was an unguarded loop.** The first store that could not be
  manifested prevented every later one from being — so the done-when ("other
  streams manifested") failed on the old code no matter how good the new
  handler was. That same `OSError`, escaping `run()`'s `finally`, also masked
  the stand-down entirely and landed as a traceback.
- **The stall watch could overwrite the disk message** with "silent …". It
  shares the flusher's 30 s period and is created second. `run()` now checks
  `write_failure` first and the watch bails early.

Also found: `_rotate_if_needed` calls `close()` on the **first** write
(`worm.py:212`, `_hour` starts unset), so an hour-boundary disk-full surfaces
inside `_tape` rather than at shutdown. The `_tape` guard covers it.

**Limitation, stated plainly:** the tests inject the `OSError` at the writer
boundary. That tests the *handling*, not the *discovery* — a real ENOSPC needs
mount privileges the suite does not have. The discovery half is pinned by the
zstd buffering assertion, which is what makes the injection point correct.

**Follow-up across branches:** `deploy/tapecheck.py`'s `LOCAL_MARKS` contains
`"OSError"`, so a disk-full hole classifies as `local-network`. It needs a
`disk` cause ahead of that in `PRECEDENCE` — which requires `feat/tapecheck` and
this branch merged first. See `14` next.
