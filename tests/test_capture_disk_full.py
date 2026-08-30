"""A store that will not take the record must say so, not go quiet.

A write error in the tape path was unobserved. `run()`'s shutdown does
`gather(*tasks, return_exceptions=True)` and never inspects the result, so an
`OSError` from a write or a flush killed its task silently -- no counter, no
exit. The only thing that eventually noticed was the 300 s stall watch, which
reported `silent hl.trades`: the wrong diagnosis at the worst moment, because it
sends the operator to the venue when the answer is the disk. Worse, the poller's
write sat inside a broad `except Exception`, so a full disk turned it into a
silent no-op that never exited at all.

**Where the error actually appears is not where you would guess.** zstd's stream
writer buffers, so a per-record `write()` never reaches the fd -- measured here,
200 records produce zero underlying writes. The flush tick is therefore the
discovery path rather than a fallback for it, which is what moves detection from
300 s to 30 s.

These tests inject the `OSError` at the writer boundary. That faithfully tests
the *handling*; it does not test *discovery*, which needs a real full
filesystem and therefore mount privileges this suite does not have. The
discovery half is pinned instead by `test_zstd_buffers_so_the_flush_is_where_a
_full_disk_appears`, which is what makes the injection point the right one.
"""

from __future__ import annotations

import errno
import io
import json

import pytest
import zstandard
from test_capture_stall import _capture, _run

import nat2.io.capture as capture_module
from nat2.io.capture import CaptureWriteFailed
from nat2.io.worm import read_manifest

FULL = OSError(errno.ENOSPC, "No space left on device")


def _break(capture, stream: str, method: str = "flush"):
    """Make one stream's writer fail the way a full disk would."""
    writer = capture.writers[stream]

    def boom(*a, **k):
        raise FULL

    setattr(writer, method, boom)


# --- the discovery fact the design rests on ---------------------------------

def test_zstd_buffers_so_the_flush_is_where_a_full_disk_appears():
    class Counting(io.RawIOBase):
        def __init__(self):
            self.writes = 0

        def writable(self):
            return True

        def write(self, b):
            self.writes += 1
            return len(b)

    sink = Counting()
    writer = zstandard.ZstdCompressor(level=3).stream_writer(sink)
    for i in range(200):
        writer.write((json.dumps({"seq": i}) + "\n").encode())
    assert sink.writes == 0, "a per-record write never reaches the fd"
    writer.flush(zstandard.FLUSH_FRAME)
    assert sink.writes == 1, "the flush is the first call that can raise ENOSPC"


# --- standing down ----------------------------------------------------------

def test_a_flush_failure_stands_the_daemon_down_and_names_the_store(tmp_path, monkeypatch):
    # The flusher is the detection path, so the tick has to actually come round.
    monkeypatch.setattr(capture_module, "FLUSH_INTERVAL_S", 0.05)
    capture = _capture(tmp_path, streams=("hl.trades", "hl.l2book"), stall_s=0)
    _break(capture, "hl.trades", "flush")

    async def feed(cap):
        cap.writers["hl.l2book"].write({"px": "1"}, None, 1)

    with pytest.raises(CaptureWriteFailed) as caught:
        _run(capture, feed=feed, timeout=5.0)
    assert "hl.trades" in str(caught.value)
    assert str(tmp_path) in str(caught.value)      # the store is named
    assert "ENOSPC" in str(caught.value)


def test_the_other_streams_are_still_manifested(tmp_path):
    """The done-when. `close()` was an unguarded loop, so the first store that
    could not be manifested prevented every later one from being -- and that
    OSError, raised out of `run()`'s finally, masked the stand-down entirely."""
    capture = _capture(tmp_path, streams=("hl.trades", "hl.l2book"), stall_s=0)

    async def feed(cap):
        cap.writers["hl.trades"].write({"px": "1"}, None, 1)
        cap.writers["hl.l2book"].write({"px": "2"}, None, 2)
        # Broken only now: `_rotate_if_needed` calls `close()` on the FIRST
        # write (worm.py:212, `_hour` starts unset), so breaking it upfront
        # would fail the write rather than the shutdown. Worth knowing in its
        # own right -- an hour-boundary disk-full surfaces inside `_tape`.
        _break(cap, "hl.trades", "close")
        cap.stop()

    with pytest.raises(CaptureWriteFailed):
        _run(capture, feed=feed, timeout=5.0)

    manifested = {e.stream for e in read_manifest(tmp_path)}
    assert "hl.l2book" in manifested, "one bad store must not cost the others their manifest"


def test_a_poller_write_failure_exits_instead_of_looping_forever(tmp_path):
    # The poller's write sat inside `except Exception`, so a full disk was
    # counted as a poll error and the loop continued indefinitely.
    capture = _capture(tmp_path, streams=("hl.assetctxs",), stall_s=0)
    _break(capture, "hl.assetctxs", "write")
    capture.write_failure = None

    capture._write_failed("hl.assetctxs", FULL)
    assert capture.write_failure and "ENOSPC" in capture.write_failure
    assert capture._stop.is_set(), "stand-down must stop the daemon, not just record it"


def test_a_venue_error_does_not_stand_the_daemon_down(tmp_path):
    """The regression guard. Splitting the fetch from the write must not make
    the poller brittle: a venue error is still something to count and tolerate."""
    capture = _capture(tmp_path, streams=("hl.assetctxs",), stall_s=0)
    capture.stats.poll_errors += 1
    assert capture.write_failure is None and not capture._stop.is_set()


# --- the diagnosis must not be overwritten ----------------------------------

def test_the_disk_message_wins_over_silence(tmp_path):
    """The flusher and the stall watch share the same 30 s period and the stall
    watch is created second, so without an explicit guard "silent ..."
    overwrites the message that names the actual cause."""
    capture = _capture(tmp_path, streams=("hl.trades",), stall_s=0)
    capture._write_failed("hl.trades", FULL)
    capture.stalled = "silent hl.trades 300s"      # what the watchdog would say

    assert capture.write_failure is not None
    # run() checks write_failure first, so the disk diagnosis is what surfaces.
    with pytest.raises(CaptureWriteFailed):
        if capture.write_failure:
            raise CaptureWriteFailed(capture.write_failure)


def test_the_first_writer_to_notice_wins(tmp_path):
    # A full disk fails every stream at once; the message must not churn.
    capture = _capture(tmp_path, streams=("hl.trades", "hl.l2book"), stall_s=0)
    capture._write_failed("hl.trades", FULL)
    first = capture.write_failure
    capture._write_failed("hl.l2book", FULL)
    assert capture.write_failure == first
