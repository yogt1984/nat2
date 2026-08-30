"""The recorded failures, reproduced on loopback.

Each test names the incident it replays. They drive the real `Capture`,
`WsClient` and `InfoClient` -- not mocks of them -- so what is under test is the
reconnect loop, the stall watchdog and the weight accounting as shipped.
"""

from __future__ import annotations

import asyncio

import pytest

import nat2.hl.info as info_module
import nat2.hl.ws as ws_module
from fake_hl import SETTLE_S, closed_port, fake_info, fake_venue
from nat2.hl.info import MAX_ATTEMPTS, InfoClient
from nat2.hl.ratelimit import SharedWeightBudget, weight_of
from nat2.io.capture import Capture, CaptureConfig, CaptureStalled
from nat2.io.worm import read_records


def _capture(tmp_path, streams=("hl.trades",), coins=("BTC",), **kw) -> Capture:
    """Unlike the helper in test_capture_stall.py this one keeps its subscriptions:
    these tests are about what happens on the wire, not about the watchdog alone."""
    return Capture(CaptureConfig(root=tmp_path, coins=list(coins), streams=list(streams),
                                 status_interval_s=99.0, **kw))


async def _run_briefly(capture, seconds: float):
    """Run the daemon, stop it after `seconds`, and re-raise whatever it raised."""
    async def stopper():
        await asyncio.sleep(seconds)
        capture.stop()

    runner = asyncio.create_task(capture.run())
    guard = asyncio.create_task(stopper())
    done, pending = await asyncio.wait({runner, guard}, timeout=seconds + 10,
                                       return_when=asyncio.FIRST_EXCEPTION)
    for task in pending:
        task.cancel()
    for task in done:
        task.result()


def test_a_socket_that_answers_pings_but_sends_nothing_still_exits(tmp_path, monkeypatch):
    """2026-08-22: the daemon stayed alive through a 5.1-hour outage because the
    websocket kept reconnecting and the poller swallowed every error, so systemd saw
    an active unit and `Restart=always` never fired. 408 minutes of tape in 30 hours.
    Liveness has to be measured in records written, and here it is."""
    async def scenario():
        async with fake_venue(behaviour="silent") as venue:
            monkeypatch.setattr(ws_module, "WS_URL", venue.url)
            capture = _capture(tmp_path, stall_s=0.5)
            with pytest.raises(CaptureStalled, match="silent hl.trades"):
                await _run_briefly(capture, 5.0)
            assert capture.stalled and "restart" in capture.stalled
            assert venue.connections == 1            # connected the whole time
            assert venue.subscriptions == [{"type": "trades", "coin": "BTC"}]
            assert capture.stats.written == {}       # and never wrote a thing

    asyncio.run(scenario())


def test_a_live_venue_is_captured_into_the_store(tmp_path, monkeypatch):
    """The control: the same wiring, a venue that behaves, and the prints land."""
    async def scenario():
        async with fake_venue(behaviour="stream", batch=2, interval_s=0.02) as venue:
            monkeypatch.setattr(ws_module, "WS_URL", venue.url)
            capture = _capture(tmp_path, stall_s=0)
            await _run_briefly(capture, 0.6)
            assert venue.connections == 1
            assert capture.stats.written["hl.trades"] > 1
            records = list(read_records(tmp_path, "hl.trades"))
            assert records, "the tape is empty"
            assert all(r["stream"] == "hl.trades" for r in records)

    asyncio.run(scenario())


def test_a_reconnect_replays_the_backlog_and_nothing_dedupes_it(tmp_path, monkeypatch):
    """HL resends a backlog after a reconnect and the store dedupes nothing on write,
    so the same tid can appear twice in the tape. Any consumer that counts prints has
    to dedupe on read; this test exists so that stays a known property, not a surprise."""
    async def scenario():
        async with fake_venue(behaviour="replay", batch=3) as venue:
            monkeypatch.setattr(ws_module, "WS_URL", venue.url)
            capture = _capture(tmp_path, stall_s=0)
            await _run_briefly(capture, 0.8)

            assert venue.connections > 1, "the venue never dropped the connection"
            tids = [t["tid"] for record in read_records(tmp_path, "hl.trades")
                    for t in record["payload"]]
            assert len(tids) > len(set(tids)), "no duplicate survived to the tape"
            assert set(range(3)) <= set(tids)        # the replayed batch, more than once

            # And the blind spot that makes it hard to see: a *clean* server close ends
            # the frame iteration without raising, so ws.py's `while` loop re-enters
            # immediately -- no backoff, and the reconnect counter never moves.
            assert capture.ws.stats.reconnects == 0

    asyncio.run(scenario())


def test_a_venue_that_is_down_costs_seven_and_a_half_seconds_before_it_gives_up(tmp_path):
    """D1's shape, and its price. `nat2 capture hl` resolves the coin universe before
    it opens a writer, and that call raises out of an unguarded block: 66 tracebacks
    over 112 unit starts, every one this exception.

    The elapsed time is the point, not incidental slowness. Each of the four attempts
    sleeps `0.5 * 2**attempt` -- including the last, which nothing then uses -- so one
    resolve against a dead venue costs 0.5+1+2+4 = 7.5s. Any retry-and-cache fix has to
    budget that per outer attempt, or a boot during an outage takes minutes."""
    async def scenario():
        client = InfoClient(SharedWeightBudget(tmp_path / "rl.sqlite"))
        client.url = f"http://127.0.0.1:{closed_port()}/info"
        loop = asyncio.get_running_loop()
        started = loop.time()
        with pytest.raises(RuntimeError, match=f"failed after {MAX_ATTEMPTS} attempts"):
            await client.universe()
        elapsed = loop.time() - started
        await client.aclose()
        assert 7.0 < elapsed < 12.0, f"the cost of a dead venue moved: {elapsed:.1f}s"

    asyncio.run(scenario())


def test_the_weight_budget_is_charged_once_per_attempt_even_when_every_one_fails(tmp_path,
                                                                                 monkeypatch):
    """Retries are not free: every attempt is acquired from the shared per-IP account
    before the request goes out, so a venue outage still spends weight the sweep will
    not see coming."""
    monkeypatch.setattr(info_module, "MAX_ATTEMPTS", 2)      # 2 attempts, not 4: 1.5s not 7.5s

    async def scenario():
        budget = SharedWeightBudget(tmp_path / "rl.sqlite")
        client = InfoClient(budget)
        client.url = f"http://127.0.0.1:{closed_port()}/info"
        with pytest.raises(RuntimeError):
            await client.post("metaAndAssetCtxs")
        await client.aclose()

        import sqlite3
        with sqlite3.connect(budget.path) as conn:
            spent = conn.execute("SELECT COALESCE(SUM(weight), 0) FROM spend").fetchone()[0]
        assert spent == 2 * weight_of("metaAndAssetCtxs")

    asyncio.run(scenario())


def test_a_429_is_honoured_retried_and_counted(tmp_path):
    """A 429 means our model of the limit is wrong. The client waits out the window
    the venue names rather than hammering it, and counts the fact so the budget
    monitor can see it."""
    async def scenario():
        with fake_info(statuses=[(429, {"retry-after": "0.05"}), 200]) as venue:
            client = InfoClient(SharedWeightBudget(tmp_path / "rl.sqlite"))
            client.url = venue.url
            loop = asyncio.get_running_loop()
            started = loop.time()
            result = await client.post("metaAndAssetCtxs")
            elapsed = loop.time() - started
            await client.aclose()

            assert result == {"universe": [{"name": "BTC"}]}
            assert venue.calls == 2                  # refused once, then served
            assert client.throttled == 1
            assert elapsed >= 0.05                   # it actually waited
            assert venue.requested[0]["type"] == "metaAndAssetCtxs"

    asyncio.run(scenario())


def test_a_429_without_a_retry_after_waits_out_the_whole_window(tmp_path, monkeypatch):
    """No header, so the client falls back to the constant that is deliberately longer
    than the sliding window it just violated."""
    monkeypatch.setattr(info_module, "THROTTLED_BACKOFF_S", 0.05)

    async def scenario():
        with fake_info(statuses=[429, 200]) as venue:
            client = InfoClient(SharedWeightBudget(tmp_path / "rl.sqlite"))
            client.url = venue.url
            loop = asyncio.get_running_loop()
            started = loop.time()
            await client.post("metaAndAssetCtxs")
            assert loop.time() - started >= 0.05
            assert client.throttled == 1 and venue.calls == 2
            await client.aclose()

    asyncio.run(scenario())


def test_the_harness_reaches_nothing_but_loopback():
    """A scenario that quietly hit the real venue would be worse than no scenario."""
    with fake_info() as info_venue:
        assert info_venue.url.startswith("http://127.0.0.1:")

    async def scenario():
        async with fake_venue() as venue:
            assert venue.url.startswith("ws://127.0.0.1:")

    asyncio.run(scenario())
    assert SETTLE_S < 1.0
