"""Job scheduling for the recurring cycle.

Deliberately small and pure: when a job is due is a decision worth testing on
its own, separately from the work it triggers.

Two properties the rest of the system depends on. **Last-run times persist**,
so restarting the daemon does not trigger an immediate registry sweep -- a
sweep costs five minutes and the entire weight budget, and a crash-loop that
re-swept on every restart would starve capture indefinitely. And **a job never
overlaps itself**: if a sweep runs long, the next tick skips rather than
queues, because two concurrent sweeps would double the weight spend to produce
one snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass

from nat2.core.clock import NS, now_ns


@dataclass
class Job:
    name: str
    interval_ns: int
    last_run_ns: int = 0
    runs: int = 0
    failures: int = 0
    running: bool = False

    def due(self, now: int | None = None) -> bool:
        if self.running:
            return False
        now = now_ns() if now is None else now
        return now - self.last_run_ns >= self.interval_ns

    def next_due_in_s(self, now: int | None = None) -> float:
        now = now_ns() if now is None else now
        return max(0.0, (self.last_run_ns + self.interval_ns - now) / NS)

    def started(self, now: int | None = None) -> None:
        self.running = True
        self.last_run_ns = now_ns() if now is None else now

    def finished(self, ok: bool) -> None:
        self.running = False
        self.runs += 1
        if not ok:
            self.failures += 1


class JobStore:
    """Last-run times, in the registry database beside everything else mutable."""

    def __init__(self, registry):
        self.registry = registry
        self.registry.ensure_jobs_table()

    def load(self, name: str, interval_ns: int) -> Job:
        row = self.registry.job(name)
        if row is None:
            return Job(name, interval_ns)
        return Job(
            name=name,
            interval_ns=interval_ns,
            last_run_ns=row["last_run_ns"],
            runs=row["runs"],
            failures=row["failures"],
        )

    def save(self, job: Job) -> None:
        self.registry.save_job(job.name, job.last_run_ns, job.runs, job.failures)
