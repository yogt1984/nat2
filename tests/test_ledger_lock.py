"""The ledger with two writers at once, and what the two daemons do about it.

`append` derives `seq` from a re-read of the whole chain, so two processes that
read seq N both write seq N -- and `verify` calls the chain broken from that
entry onwards for good, because no later hash can be recomputed over a
predecessor that was never there.  Two processes appending 25 entries each
broke it in 12 of 12 trials before the lock: the collision is the normal case
under contention, not a rare race.

These tests spawn real processes.  Seven appenders run in production and
gapwatch reaches the ledger by shelling out to `nat2 log add`, so a
`threading.Lock` would pass a test that the deployment fails.
"""

from __future__ import annotations

import asyncio
import fcntl
import multiprocessing as mp
from pathlib import Path

import pytest

from nat2.core.clock import NS, now_ns
from nat2.core.guard import record
from nat2.io import actions
from nat2.ledger import chain
from nat2.ledger.chain import Ledger

WRITERS = ("a", "b")
PER_WRITER = 25

# Long enough that the lock is provably taken, short enough that a test that
# waits it out costs a fifth of a second rather than the production ten.
HELD_TIMEOUT_S = 0.2

# Nothing here should ever reach these; they exist so that a writer which dies
# at import fails the test instead of hanging the suite, which has no timeout
# plugin to rescue it.
BARRIER_TIMEOUT_S = 30.0
JOIN_TIMEOUT_S = 60.0


def _append_many(path: str, tag: str, barrier) -> None:
    """One writer's whole run, in its own process, starting when the other does."""
    ledger = Ledger(Path(path))
    barrier.wait(timeout=BARRIER_TIMEOUT_S)
    for i in range(PER_WRITER):
        ledger.append("observation", {"writer": tag, "i": i})


def test_two_processes_appending_at_once_leave_the_chain_intact(tmp_path):
    path = tmp_path / "ledger.jsonl"
    # spawn, not fork: by the time the whole suite reaches this file the pytest
    # process has threads, and forking a multi-threaded process is a documented
    # deadlock risk that Python 3.12 warns about.  Spawn also makes the writers
    # honest -- fresh interpreters, nothing inherited but the arguments, which
    # is what `nat2 log add` is when gapwatch shells out to it.
    ctx = mp.get_context("spawn")
    barrier = ctx.Barrier(len(WRITERS))
    procs = [ctx.Process(target=_append_many, args=(str(path), tag, barrier))
             for tag in WRITERS]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(timeout=JOIN_TIMEOUT_S)
        assert proc.exitcode == 0, f"a writer died with exitcode {proc.exitcode}"

    entries = Ledger(path).entries()
    assert [e.seq for e in entries] == list(range(len(WRITERS) * PER_WRITER))
    # Serialising is not enough on its own: a lock that dropped a write would
    # still leave a gapless run of seqs.  Every writer's every entry is here.
    assert sorted((e.payload["writer"], e.payload["i"]) for e in entries) == sorted(
        (tag, i) for tag in WRITERS for i in range(PER_WRITER)
    )
    assert Ledger(path).verify() == (True, "chain intact")


def test_a_ledger_held_by_another_writer_raises_rather_than_blocking(tmp_path, monkeypatch):
    # Imported here rather than at module scope so this file still collects
    # against the commit before the lock, where the race test above is meant to
    # fail on its assertion rather than take the module down at import.
    from nat2.ledger.chain import LedgerBusy

    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path)
    ledger.append("observation", {"n": 0})
    monkeypatch.setattr(chain, "LOCK_TIMEOUT_S", HELD_TIMEOUT_S)

    # flock belongs to the open file description rather than to the process, so
    # a second handle opened here contends exactly as another process would.
    with path.open("a") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        with pytest.raises(LedgerBusy):
            ledger.append("observation", {"n": 1})

    # A deadline, not a death: the same ledger appends once the holder lets go.
    ledger.append("observation", {"n": 1})
    assert [e.payload["n"] for e in ledger.entries()] == [0, 1]
    assert ledger.verify()[0]


def test_a_gate_verdict_that_cannot_be_recorded_is_not_returned(tmp_path, monkeypatch):
    from nat2.ledger.chain import LedgerBusy

    path = tmp_path / "data" / "ledger.jsonl"
    path.parent.mkdir(parents=True)
    ledger = Ledger(path)
    monkeypatch.setattr(chain, "LOCK_TIMEOUT_S", HELD_TIMEOUT_S)

    with path.open("a") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        with pytest.raises(LedgerBusy):
            record(ledger, "feed", True, {"verdict": "pass"})

    # No entry, and no Verdict handed back claiming there was one.
    assert ledger.entries() == []
    # The action log takes no lock, so the loss is on the record even though the
    # verdict is not.
    lost = [a for a in actions.read(root=tmp_path)
            if a["payload"].get("verdict") == "unrecorded"]
    assert [a["payload"]["reason"] for a in lost] == ["LedgerBusy"]


def test_the_cycle_survives_a_busy_ledger(tmp_path, monkeypatch):
    from nat2.features.liqmath import Position
    from nat2.features.liquidations import LiquidationEvent
    from nat2.io.cycle import Cycle, CycleConfig

    path = tmp_path / "l.jsonl"
    cycle = Cycle(
        CycleConfig(
            registry_path=tmp_path / "r.sqlite",
            ledger_path=path,
            raw_root=tmp_path / "raw",
            snapshot_interval_ns=6 * 3600 * NS,
            scan_interval_ns=3600 * NS,
        ),
        budget=None,
    )
    cycle.registry.replace_positions([(
        Position(address="0xa", coin="BTC", szi=1.0, mark=100.0, max_leverage=40,
                 margin_type="cross", account_value=10.0, maint_margin=0.0,
                 liquidation_px=95.0),
        "published",
    )])
    cycle.registry.record_liquidations([
        LiquidationEvent(1, now_ns(), "BTC", "0xa", 95.0, "market", 95.0, 1.0, "0xo",
                         "counterparty")
    ])
    monkeypatch.setattr(chain, "LOCK_TIMEOUT_S", HELD_TIMEOUT_S)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
        results = asyncio.run(cycle.run_once(force=True))

    # The pass completed and said what it lost, rather than taking the daemon
    # down with it -- the whole point of catching it at that one call site.
    assert results["coverage"]["error"].startswith("LedgerBusy:")
    assert "scan" in results
