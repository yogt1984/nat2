"""Scheduling, and the two ways it could quietly ruin the weight budget.

A restart must not trigger an immediate registry sweep — a sweep costs five
minutes and the whole IP allowance, so a crash-loop that re-swept every restart
would starve capture indefinitely. And a job must never overlap itself: two
concurrent sweeps spend twice the weight to produce one snapshot.
"""

from __future__ import annotations

import asyncio

import pytest

from nat2.core.clock import NS, now_ns
from nat2.core.registry import Registry
from nat2.core.schedule import Job, JobStore
from nat2.io.cycle import Cycle, CycleConfig
from nat2.ledger.chain import Ledger

HOUR = 3600 * NS


# --- due logic -------------------------------------------------------------

def test_a_fresh_job_is_due_immediately():
    # last_run_ns of 0 is the epoch, so against any real clock it is overdue.
    assert Job("scan", HOUR).due()
    assert Job("scan", HOUR).due(now=HOUR)


def test_a_job_is_not_due_before_its_interval():
    job = Job("scan", HOUR, last_run_ns=1000)
    assert not job.due(now=1000 + HOUR - 1)


def test_a_job_is_due_exactly_on_its_interval():
    job = Job("scan", HOUR, last_run_ns=1000)
    assert job.due(now=1000 + HOUR)


def test_an_overdue_job_is_due_once_not_repeatedly_queued():
    # Ten missed intervals still means one run, not ten.
    job = Job("scan", HOUR, last_run_ns=1000)
    assert job.due(now=1000 + 10 * HOUR)
    job.started(now=1000 + 10 * HOUR)
    job.finished(ok=True)
    assert not job.due(now=1000 + 10 * HOUR)
    assert job.runs == 1


def test_a_running_job_is_never_due():
    job = Job("snapshot", HOUR, last_run_ns=0)
    job.started(now=now_ns())
    assert not job.due()


def test_next_due_is_never_negative():
    job = Job("scan", HOUR, last_run_ns=0)
    assert job.next_due_in_s(now=10 * HOUR) == 0.0


def test_failures_are_counted_but_still_advance_the_clock():
    # A failing job must not retry in a tight loop against the rate limit.
    job = Job("scan", HOUR)
    job.started(now=5000)
    job.finished(ok=False)
    assert job.runs == 1 and job.failures == 1
    assert not job.due(now=5001)


# --- persistence -----------------------------------------------------------

def test_last_run_survives_a_restart(tmp_path):
    registry = Registry(tmp_path / "r.sqlite")
    store = JobStore(registry)
    job = store.load("snapshot", 6 * HOUR)
    job.started()
    job.finished(ok=True)
    store.save(job)

    # A new process, as a restart would be: the expensive sweep must not fire
    # again just because we came back up.
    reloaded = JobStore(Registry(tmp_path / "r.sqlite")).load("snapshot", 6 * HOUR)
    assert reloaded.last_run_ns == job.last_run_ns
    assert reloaded.runs == 1
    assert not reloaded.due()


def test_unknown_job_loads_with_a_zero_clock(tmp_path):
    job = JobStore(Registry(tmp_path / "r.sqlite")).load("brand-new", HOUR)
    assert job.last_run_ns == 0 and job.runs == 0 and job.due()


# --- cycle wiring ----------------------------------------------------------

def _cycle(tmp_path, **kw) -> Cycle:
    config = CycleConfig(
        registry_path=tmp_path / "r.sqlite",
        ledger_path=tmp_path / "l.jsonl",
        # Never the default `data/raw`: from the repo root that is the live tape, and a
        # forced cycle would replay all of it (minutes, gigabytes) inside a unit test.
        raw_root=tmp_path / "raw",
        snapshot_interval_ns=kw.pop("snapshot_ns", 6 * HOUR),
        scan_interval_ns=kw.pop("scan_ns", HOUR),
    )
    return Cycle(config, budget=None)


def test_cycle_skips_jobs_that_are_not_due(tmp_path):
    cycle = _cycle(tmp_path)
    for job in cycle.jobs.values():
        job.last_run_ns = now_ns()
    assert asyncio.run(cycle.run_once()) == {}


def test_cycle_force_overrides_the_interval(tmp_path):
    cycle = _cycle(tmp_path)
    for job in cycle.jobs.values():
        job.last_run_ns = now_ns()
    results = asyncio.run(cycle.run_once(force=True))
    # Registry is empty, so both jobs report a skip rather than doing work.
    assert results["snapshot"] == {"skipped": "registry is empty"}
    assert results["scan"] == {"skipped": "registry is empty"}


def test_a_failing_job_does_not_abort_the_other(tmp_path):
    cycle = _cycle(tmp_path)

    async def boom():
        raise RuntimeError("HL said no")

    cycle._run_snapshot = boom
    results = asyncio.run(cycle.run_once(force=True))
    assert "HL said no" in results["snapshot"]["error"]
    assert "scan" in results, "one job failing must not skip the next"
    assert cycle.jobs["snapshot"].failures == 1
    assert cycle.jobs["scan"].failures == 0


def test_failure_is_persisted_so_a_restart_does_not_retry_immediately(tmp_path):
    cycle = _cycle(tmp_path)

    async def boom():
        raise RuntimeError("nope")

    cycle._run_snapshot = boom
    asyncio.run(cycle.run_once(force=True))
    reloaded = _cycle(tmp_path)
    assert not reloaded.jobs["snapshot"].due()
    assert reloaded.jobs["snapshot"].failures == 1


def test_coverage_is_recorded_to_the_ledger_after_a_scan(tmp_path):
    from nat2.features.liqmath import Position
    from nat2.features.liquidations import LiquidationEvent

    cycle = _cycle(tmp_path)
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
    asyncio.run(cycle.run_once(force=True))

    entry = Ledger(tmp_path / "l.jsonl").latest("observation", name="liq_population")
    assert entry is not None
    assert entry.payload["events"] == 1
    assert Ledger(tmp_path / "l.jsonl").verify()[0]


def test_no_coverage_entry_when_nothing_was_scanned(tmp_path):
    cycle = _cycle(tmp_path)
    for job in cycle.jobs.values():
        job.last_run_ns = now_ns()
    asyncio.run(cycle.run_once())
    assert Ledger(tmp_path / "l.jsonl").latest("observation", name="liq_population") is None


def test_status_reports_both_jobs(tmp_path):
    status = _cycle(tmp_path).status()
    assert "snapshot" in status and "scan" in status


@pytest.mark.parametrize("window,expected_h", [("1h", 1), ("6h", 6), ("30m", 0.5)])
def test_intervals_come_from_the_window_parser(window, expected_h):
    from nat2.core.clock import parse_window

    assert parse_window(window) == pytest.approx(expected_h * HOUR)


def test_every_job_that_ran_is_an_action_in_the_cycle_home(tmp_path):
    """TASK_2/13: L1 for a job that ran, L0 for one that failed -- next to this cycle's data home."""
    from nat2.io import actions
    cycle = _cycle(tmp_path)

    async def boom():
        raise RuntimeError("HL said no")

    cycle._run_snapshot = boom
    asyncio.run(cycle.run_once(force=True))
    by_kind = {r["kind"]: r["level"] for r in actions.read(tmp_path)}
    assert by_kind["cycle:snapshot"] == "L0" and by_kind["cycle:scan"] == "L1"
