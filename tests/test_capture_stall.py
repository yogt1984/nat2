"""TASK_2/12 follow-up: a capture that has gone silent must exit, not spin.

On 2026-08-22 the daemon stayed *alive* through a 5.1-hour DNS outage -- the websocket
reconnected forever (`gaierror` x11/min) and the poller swallowed every error, so systemd
saw an active unit and `Restart=always` never fired. 408 minutes of tape were lost in 30
hours. Liveness is therefore measured in records written, not in whether the process
exists, and the watchdog counts from the first record so a slow start is not a stall.
"""

import asyncio

import pytest

from nat2.io.capture import Capture, CaptureConfig, CaptureStalled, CaptureTaskFailed
from nat2.io.worm import read_manifest, read_records


def _capture(tmp_path, streams=("hl.trades",), **kw) -> Capture:
    """A daemon with no subscriptions, so no websocket is opened: these tests are about
    the watchdog, and a unit test that reaches the venue tests the venue.

    `streams` is a parameter because the disk-full tests need more than one: the
    done-when there is that the OTHER streams are still manifested when one of
    them cannot be written."""
    capture = Capture(CaptureConfig(root=tmp_path, coins=["BTC"], streams=list(streams),
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


def test_a_clock_jump_across_a_suspend_is_not_a_stall(tmp_path, monkeypatch):
    """su-35 is a laptop. `asyncio.sleep` runs on `time.monotonic()`, which does not tick
    across s2idle, so a watchdog reading the wall clock saw the whole suspend as silence:
    three kills between 09-13 and 09-14, one of them reporting an age of 95,360 s for a
    process that had been asleep rather than quiet, and all three of a daemon that then
    wrote normally for hours. The wall clock is what jumps here; records keep arriving."""
    import nat2.io.capture as capture_module

    real, offset = capture_module.now_ns, {"ns": 0}
    monkeypatch.setattr(capture_module, "now_ns", lambda: real() + offset["ns"])
    capture = _capture(tmp_path, stall_s=2.0)

    def write(cap, i):
        cap.writers["hl.trades"].write([{"px": str(i)}], None, i + 1)
        cap.stats.bump("hl.trades")

    async def suspend_like_a_laptop(cap):
        for i in range(8):
            write(cap, i)
            await asyncio.sleep(0.1)
        # s2idle: the process is frozen, so nothing is written while it is out, and
        # the wall clock it wakes to has moved an hour it did not spend being silent.
        # The freeze has to outlast more than one watchdog tick or a write still in
        # flight refreshes the clock and hides the step -- which is also why the real
        # outage needed a *long* suspend to show up. 1.2 s of real silence against a
        # 2.0 s threshold: the hour is the only thing that can trip this.
        offset["ns"] = 3600 * 10**9
        await asyncio.sleep(1.2)
        for i in range(8, 16):
            write(cap, i)
            await asyncio.sleep(0.1)
        cap.stop()

    _run(capture, suspend_like_a_laptop)
    assert capture.stalled is None, "the wall clock jumped, not the tape"
    assert capture.stats.written["hl.trades"] == 16


def test_waiting_on_our_own_rate_limiter_is_not_silence(tmp_path):
    """Three non-outage kills landed inside a 6-hourly registry sweep, which reserves 60%
    of the per-IP budget; the poller was queued behind it, and every one of those exits
    printed "no errors reported" because there had been no error. Being held off by our
    own accounting is not the venue going quiet."""
    capture = _capture(tmp_path, streams=("hl.assetctxs",), stall_s=0.5)

    async def held_off_by_the_budget(cap):
        for _ in range(10):                # 1.0 s of starvation against 1.5 s of allowance
            cap.budget.waits += 1
            cap.budget.wait_s += 0.05      # the shape `WeightBudget._try` leaves behind
            await asyncio.sleep(0.1)
        cap.stop()

    _run(capture, held_off_by_the_budget)
    assert capture.stalled is None and capture.budget.waits == 10


def test_the_budget_excuse_is_bounded_so_a_real_outage_still_exits(tmp_path):
    """The carve-out above is the door the 2026-08-22 failure came through -- a process
    that never dies. Budget pressure buys 2 x stall_s of grace and no more."""
    capture = _capture(tmp_path, streams=("hl.assetctxs",), stall_s=0.5)

    async def held_off_forever(cap):
        for _ in range(60):
            cap.budget.waits += 1
            cap.budget.wait_s += 0.05
            await asyncio.sleep(0.1)

    with pytest.raises(CaptureStalled, match="silent hl.assetctxs"):
        _run(capture, held_off_forever)


def test_a_stall_is_still_reported_when_the_budget_is_idle(tmp_path):
    """The other half of the carve-out: an idle limiter excuses nothing."""
    capture = _capture(tmp_path, streams=("hl.assetctxs",), stall_s=0.5)
    with pytest.raises(CaptureStalled, match="silent hl.assetctxs"):
        _run(capture)
    assert capture.budget.waits == 0


def test_a_stream_this_process_does_not_produce_is_not_watched(tmp_path):
    """`nat2.liqmap` shares the store but is written by `nat2 cycle`. Handed to capture as
    a `--stream` it was an unconditional kill every `stall_s`, for ever."""
    capture = _capture(tmp_path, streams=("hl.trades", "nat2.liqmap"), stall_s=0.5)

    async def keep_writing(cap):
        for i in range(15):
            cap.writers["hl.trades"].write([{"px": str(i)}], None, i + 1)
            cap.stats.bump("hl.trades")
            await asyncio.sleep(0.1)
        cap.stop()

    _run(capture, keep_writing)
    assert capture.stalled is None


def test_a_status_line_that_cannot_be_printed_does_not_kill_the_capture(tmp_path):
    """The other side of naming a dead task: every task that writes or guards the tape is
    now fatal when it dies, and the heartbeat is neither. A journald restart under the
    daemon must not cost the tape, so this one is counted rather than raised."""
    capture = Capture(CaptureConfig(root=tmp_path, coins=["BTC"], streams=["hl.trades"],
                                    status_interval_s=0.1, stall_s=0),
                      on_status=lambda _cap: (_ for _ in ()).throw(OSError("broken pipe")))
    capture._subscriptions = lambda: []

    async def keep_writing(cap):
        for i in range(10):
            cap.writers["hl.trades"].write([{"px": str(i)}], None, i + 1)
            cap.stats.bump("hl.trades")
            await asyncio.sleep(0.1)
        cap.stop()

    _run(capture, keep_writing)
    assert capture.stats.status_errors >= 5, "the status line never failed; nothing was proved"
    assert capture.stats.written["hl.trades"] == 10


def test_a_dead_task_names_itself_instead_of_masquerading_as_a_stall(tmp_path):
    """`run()` parks on `_stop`, so a task that dies of anything but an `OSError` was
    swallowed by `gather(..., return_exceptions=True)` and surfaced `stall_s` later as
    "silent hl.trades" -- sending the operator to the venue for a defect of ours."""
    capture = _capture(tmp_path, stall_s=5.0)
    original = capture.writers["hl.trades"].flush

    def explode():
        original()
        raise ValueError("zstd frame went sideways")

    capture.writers["hl.trades"].flush = explode
    # The flusher's own period, so the test does not wait 30 s to see it die.
    import nat2.io.capture as capture_module
    capture_module.FLUSH_INTERVAL_S = 0.1
    try:
        with pytest.raises(CaptureTaskFailed, match="flusher died: ValueError"):
            _run(capture, timeout=5.0)
    finally:
        capture_module.FLUSH_INTERVAL_S = 30.0
    assert capture.stalled is None, "a dead task is not a stall"
