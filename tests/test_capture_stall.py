"""TASK_2/12 follow-up: a capture that has gone silent must exit, not spin.

On 2026-08-22 the daemon stayed *alive* through a 5.1-hour DNS outage -- the websocket
reconnected forever (`gaierror` x11/min) and the poller swallowed every error, so systemd
saw an active unit and `Restart=always` never fired. 408 minutes of tape were lost in 30
hours. Liveness is therefore measured in records written, not in whether the process
exists, and the watchdog counts from the first record so a slow start is not a stall.
"""

import asyncio

import pytest

from nat2.io.capture import Capture, CaptureConfig, CaptureStalled
from nat2.io.worm import read_manifest, read_records


def _capture(tmp_path, **kw) -> Capture:
    """A daemon with no subscriptions, so no websocket is opened: these tests are about
    the watchdog, and a unit test that reaches the venue tests the venue."""
    capture = Capture(CaptureConfig(root=tmp_path, coins=["BTC"], streams=["hl.trades"],
                                    status_interval_s=99.0, **kw))
    capture._subscriptions = lambda: []
    return capture


def _run(capture, feed=None, timeout=15.0):
    """Run the daemon with `feed(capture)` as its only writer, until it stops."""
    async def main():
        tasks = [asyncio.create_task(capture.run())]
        if feed:
            tasks.append(asyncio.create_task(feed(capture)))
        done, pending = await asyncio.wait(tasks, timeout=timeout, return_when=asyncio.FIRST_EXCEPTION)
        for t in pending:
            t.cancel()
        capture.stop()
        for t in done:
            t.result()          # re-raise CaptureStalled
    return asyncio.run(main())


def test_a_silent_capture_exits_so_the_supervisor_can_restart_it(tmp_path):
    capture = _capture(tmp_path, stall_s=0.5)

    async def one_record(cap):
        cap.writers["hl.trades"].write([{"px": "1"}], None, 1)
        cap.stats.bump("hl.trades")           # ... and then nothing, ever again

    with pytest.raises(CaptureStalled, match="silent hl.trades"):
        _run(capture, one_record)
    assert capture.stalled and "restart" in capture.stalled
    # Shutdown still closed the writer, so the part it did capture is manifested, not orphaned.
    assert len(list(read_records(tmp_path, "hl.trades"))) == 1
    assert [e.lines for e in read_manifest(tmp_path)] == [1]


def test_a_capture_that_keeps_writing_is_never_killed(tmp_path):
    # 20x headroom between writes and the stall threshold: this test must not fail because
    # the machine was busy, or it would teach the reader to ignore it.
    capture = _capture(tmp_path, stall_s=2.0)

    async def keep_writing(cap):
        for i in range(12):
            cap.writers["hl.trades"].write([{"px": str(i)}], None, i + 1)
            cap.stats.bump("hl.trades")
            await asyncio.sleep(0.1)
        cap.stop()

    _run(capture, keep_writing)               # no CaptureStalled
    assert capture.stalled is None and capture.stats.written["hl.trades"] == 12


def test_a_capture_that_never_connects_exits_too(tmp_path):
    """The observed case: one process alive 5.4 hours writing nothing, because it came up
    during the outage. The clock therefore starts at start-up, not at the first record."""
    capture = _capture(tmp_path, stall_s=0.5)
    with pytest.raises(CaptureStalled, match="hl.trades"):
        _run(capture)
    assert "silent" in capture.stalled and list(read_records(tmp_path, "hl.trades")) == []


def test_one_live_stream_does_not_mask_another_that_died(tmp_path):
    """`assetctxs` ticked 117 -> 118 while `trades` stayed frozen at 22944, so a watchdog on
    the sum of all streams would have reset its own clock and seen nothing wrong."""
    capture = _capture(tmp_path, stall_s=1.5)
    capture.writers["hl.assetctxs"] = capture.writers["hl.trades"].__class__(tmp_path, "hl.assetctxs")

    async def only_assetctxs(cap):
        for i in range(40):
            cap.writers["hl.assetctxs"].write([{"i": i}], None, i + 1)
            cap.stats.bump("hl.assetctxs")
            await asyncio.sleep(0.1)

    with pytest.raises(CaptureStalled, match="hl.trades"):
        _run(capture, only_assetctxs)
    assert "hl.assetctxs" not in capture.stalled and capture.stats.written["hl.assetctxs"] >= 5


def test_the_watchdog_can_be_disabled(tmp_path):
    capture = _capture(tmp_path, stall_s=0)

    async def never(cap):
        await asyncio.sleep(0.8)
        cap.stop()

    _run(capture, never)
    assert capture.stalled is None
