"""Hash-chained append-only ledger.

Gate verdicts, spec freezes and run records land here.  Each entry commits to
the previous one, so removing or editing a past entry breaks every hash after
it and ``nat2 log verify`` says exactly where.  The point is not security
against an attacker -- it is that a failed test you'd rather forget cannot
quietly leave the record.

`append` is a read-modify-write: it re-parses the chain to derive `seq` and the
previous hash, then writes.  Two writers that read seq N both emit seq N, and
`verify` calls the chain broken from that entry onwards -- permanently, because
nothing can recompute a hash over a predecessor that was never there.  Two
processes appending 25 entries each broke it in 12 of 12 trials.  Seven
appenders run in production and gapwatch reaches the ledger by shelling out to
`nat2 log add`, so the exclusion has to hold between processes, not merely
between threads.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

from nat2.core.clock import now_ns

GENESIS = "0" * 64

# How long an appender waits for the writer ahead of it, and how often it looks.
# Both numbers are the ones `hl/ratelimit.py` already waits with, so contention
# here behaves like contention there and no new threshold enters the code.
LOCK_TIMEOUT_S = 10.0
LOCK_POLL_S = 0.05


class LedgerBusy(RuntimeError):
    """Another writer held the ledger for longer than the deadline.

    Raised rather than waited out for good: the callers are daemons, and an
    append that blocks forever is a stall the watchdog can only report as
    silence.
    """


@dataclass(frozen=True)
class Entry:
    seq: int
    ts: int
    kind: str
    payload: dict
    prev_hash: str
    hash: str


def _parse(text: str) -> list[Entry]:
    return [Entry(**json.loads(line)) for line in text.splitlines() if line.strip()]


def _digest(seq: int, ts: int, kind: str, payload: dict, prev_hash: str) -> str:
    body = json.dumps(
        {"seq": seq, "ts": ts, "kind": kind, "payload": payload, "prev_hash": prev_hash},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def entries(self) -> list[Entry]:
        if not self.path.exists():
            return []
        return _parse(self.path.read_text())

    @contextmanager
    def _locked(self):
        """Exclusive access to the chain, held across the whole read-modify-write.

        The lock is taken on the ledger's own inode.  A sidecar lockfile would
        be a second artefact that has to travel with the store, and the cutover
        moves the store by hand -- one that arrived without its lock would look
        exactly like one that arrived with it.  The corollary is that nothing
        may ever rotate, rename or atomically replace `ledger.jsonl`: two
        writers on two inodes would each hold an uncontested lock.

        `flock` rather than `lockf`, which is not interchangeable here.  A POSIX
        record lock is dropped when the process closes *any* descriptor on the
        inode, and `entries()` reads through a separate `read_text()` -- that
        call alone would release a `lockf` lock while this code believed it
        still held one.

        `"a+"` is the only mode that creates the file without truncating it and
        is still readable, so the chain can be re-read from the very handle the
        lock is held on.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        with self.path.open("a+") as fh:
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError as exc:
                    # Exactly EAGAIN/EWOULDBLOCK, which is the only "someone else
                    # has it" answer flock gives.  Every other error it can raise
                    # -- EBADF, EINVAL, ENOLCK -- is a real fault and is left to
                    # propagate rather than retried until the deadline.
                    if time.monotonic() >= deadline:
                        raise LedgerBusy(
                            f"{self.path} was held by another writer for more than "
                            f"{LOCK_TIMEOUT_S:.0f}s"
                        ) from exc
                    time.sleep(LOCK_POLL_S)
            yield fh

    def append(self, kind: str, payload: dict) -> Entry:
        with self._locked() as fh:
            # An "a+" handle starts at EOF, so without the seek every append
            # would parse an empty chain and derive seq 0 -- a worse version of
            # the bug this lock exists to prevent.  The write still lands at the
            # end regardless of where the read left the position: O_APPEND.
            fh.seek(0)
            existing = _parse(fh.read())
            seq = len(existing)
            prev = existing[-1].hash if existing else GENESIS
            ts = now_ns()
            entry = Entry(seq, ts, kind, payload, prev, _digest(seq, ts, kind, payload, prev))
            fh.write(json.dumps(asdict(entry), separators=(",", ":")) + "\n")
            # Both before the lock drops.  A buffered write leaves the line in
            # userspace, where the next writer's re-read cannot see it -- it
            # would derive this same seq again, which is exactly the break being
            # fixed.  The fsync is the cheap half of the same argument: ~1 ms
            # against roughly fifteen appends a day, and it narrows the window
            # in which a power cut leaves a half-written final line.
            fh.flush()
            os.fsync(fh.fileno())
        return entry

    def verify(self) -> tuple[bool, str]:
        prev = GENESIS
        for i, entry in enumerate(self.entries()):
            if entry.seq != i:
                return False, f"entry {i}: seq is {entry.seq}"
            if entry.prev_hash != prev:
                return False, f"entry {i}: prev_hash does not match entry {i - 1}"
            expect = _digest(entry.seq, entry.ts, entry.kind, entry.payload, entry.prev_hash)
            if entry.hash != expect:
                return False, f"entry {i}: content does not match its hash (edited)"
            prev = entry.hash
        return True, "chain intact"

    def latest(self, kind: str, **match) -> Entry | None:
        for entry in reversed(self.entries()):
            if entry.kind != kind:
                continue
            if all(entry.payload.get(k) == v for k, v in match.items()):
                return entry
        return None
