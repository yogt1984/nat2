"""Append-only capture store.

Raw records are written once and never rewritten.  Files rotate on the UTC
hour; when a file closes it is checksummed and an entry is appended to a
manifest.  The manifest is the thing ``gate feed`` audits: if a closed file's
bytes no longer hash to its recorded digest, the store has been tampered with
or corrupted and everything downstream is void.

Layout::

    data/raw/_manifest.jsonl
    data/raw/<stream>/<YYYY-MM-DD>/<stream>-<YYYYMMDDTHH>.ndjson.zst

One line per record::

    {"seq": 12, "stream": "hl.trades", "t_ingest": <ns>, "t_event": <ns|null>,
     "payload": {...}}

``seq`` is per-stream and continues across process restarts (resumed from the
manifest), so a gap in ``seq`` means records were lost -- not merely that the
daemon was restarted.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path

import zstandard

from nat2.core.clock import day_key, hour_key, now_ns

MANIFEST = "_manifest.jsonl"
SUFFIX = ".ndjson.zst"


@dataclass(frozen=True)
class ManifestEntry:
    stream: str
    path: str
    lines: int
    bytes: int
    sha256: str
    first_seq: int
    last_seq: int
    first_ingest: int
    last_ingest: int
    closed_at: int

    @classmethod
    def from_json(cls, raw: dict) -> "ManifestEntry":
        return cls(**{k: raw[k] for k in cls.__dataclass_fields__})


def sha256_file(path: Path) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def read_manifest(root: Path, stream: str | None = None) -> list[ManifestEntry]:
    root = Path(root)
    path = root / MANIFEST
    if not path.exists():
        return []
    out = []
    # A crash mid-append leaves a torn line -- on ext4 a run of NULs where the
    # blocks were allocated but never written, then however much of the record
    # reached the disk. It is incomplete rather than corrupt, exactly like the
    # open data file's tail in `read_records`, and the part it describes is
    # still on disk with its own seqs, which `_resume_seq` recovers by scanning
    # unmanifested files.
    #
    # Tolerated wherever it appears, not only last. Skipping a torn line only
    # when it is last is a rule the next append breaks: the live manifest holds
    # 999 NULs from one unclean shutdown at line 18960, and the first `mapsnap`
    # after the following boot appended into that gap and was itself truncated
    # mid-key -- so the one write that got through is what made the line
    # unreadable, and every `mapsnap` since has died on it.
    #
    # A malformed line with no crash signature is real corruption and still
    # raises, which is what `gate feed` reports on.
    lines = path.read_text(errors="replace").splitlines()
    for i, line in enumerate(lines):
        record = line.strip("\x00 \t\r\n\ufffd")
        if not record:
            continue
        try:
            payload = json.loads(record)
        except json.JSONDecodeError:
            if "\x00" in line or "\ufffd" in line or i == len(lines) - 1:
                continue
            raise
        entry = ManifestEntry.from_json(payload)
        if stream is None or entry.stream == stream:
            out.append(entry)
    return out


def read_records(root: Path, stream: str, since_ns: int | None = None):
    """Yield records for a stream in file order, including the open file.

    The current hour's file is being written concurrently, so its tail is
    routinely a half-written record. A complete record always ends in a
    newline, so an unterminated remainder is incomplete rather than corrupt
    and is skipped -- it will be read whole on the next pass. A decode failure
    on a *terminated* line is real corruption and still raises, which is what
    `gate feed` reports on.
    """
    root = Path(root)
    dec = zstandard.ZstdDecompressor()
    for path in sorted((root / stream).rglob(f"*{SUFFIX}")):
        if since_ns is not None and _file_end_ns(path) <= since_ns:
            # Files rotate on the t_ingest hour and carry it in their name, so a file
            # that ended before `since` cannot hold a record at or after it. Without
            # this every "last 24h" read decompressed and parsed the whole tape.
            continue
        with path.open("rb") as fh, dec.stream_reader(fh) as reader:
            buf = b""
            while True:
                try:
                    chunk = reader.read(1 << 20)
                except zstandard.ZstdError:
                    # Frame still open: the writer has not flushed it yet.
                    break
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    if not line:
                        continue
                    rec = json.loads(line)
                    if since_ns is None or rec["t_ingest"] >= since_ns:
                        yield rec


def _file_end_ns(path: Path) -> int:
    """Exclusive end of the hour a part covers, from `<stream>-<YYYYMMDDTHH>-<part>.ndjson.zst`."""
    try:
        hour = path.name.split(".ndjson", 1)[0].rsplit("-", 2)[-2]
        start = datetime.strptime(hour, "%Y%m%dT%H").replace(tzinfo=timezone.utc)
    except (IndexError, ValueError):
        return 1 << 63   # not a rotated part: never skip it
    return int(start.timestamp()) * 1_000_000_000 + 3600 * 1_000_000_000


class WormWriter:
    """One writer per stream.  Hourly rotation, checksummed on close."""

    def __init__(self, root: Path, stream: str, level: int = 3):
        self.root = Path(root)
        self.stream = stream
        self._level = level
        self._hour: str | None = None
        self._path: Path | None = None
        self._fh = None
        self._dirty = False
        self._zw = None
        self._lines = 0
        self._first_seq = 0
        self._first_ingest = 0
        self._last_ingest = 0
        self.seq = self._resume_seq()

    def _resume_seq(self) -> int:
        entries = read_manifest(self.root, self.stream)
        last = max((e.last_seq for e in entries), default=-1)
        # Membership as a set, not a scan per file: every snapshot opens its own part, so
        # this stream reached 2,571 of them and the quadratic version took over three
        # minutes per writer -- which is what starved `mapsnap` and left the map stale.
        manifested = {e.path for e in entries}
        # An open (unmanifested) file from a previous run may hold higher seqs.
        for path in sorted((self.root / self.stream).rglob(f"*{SUFFIX}")):
            if str(path.relative_to(self.root)) in manifested:
                continue
            for rec in _tail_seqs(path):
                last = max(last, rec)
        return last + 1

    def write(self, payload: dict, t_event: int | None, t_ingest: int | None = None) -> None:
        t_ingest = t_ingest if t_ingest is not None else now_ns()
        self._rotate_if_needed(t_ingest)
        record = {
            "seq": self.seq,
            "stream": self.stream,
            "t_ingest": t_ingest,
            "t_event": t_event,
            "payload": payload,
        }
        self._zw.write(json.dumps(record, separators=(",", ":")).encode() + b"\n")
        self._dirty = True
        if self._lines == 0:
            self._first_seq = self.seq
            self._first_ingest = t_ingest
        self._last_ingest = t_ingest
        self._lines += 1
        self.seq += 1

    def _rotate_if_needed(self, t_ingest: int) -> None:
        hour = hour_key(t_ingest)
        if hour == self._hour:
            return
        self.close()
        self._hour = hour
        directory = self.root / self.stream / day_key(t_ingest)
        directory.mkdir(parents=True, exist_ok=True)
        # A restart inside the same hour opens a NEW part rather than
        # appending. Appending would make an already-manifested file grow, so
        # its recorded digest would no longer match its bytes and the integrity
        # check could not tell a restart from tampering. One file, closed once,
        # immutable forever.
        part = 0
        while (path := directory / f"{self.stream}-{hour}-{part:02d}{SUFFIX}").exists():
            part += 1
        self._path = path
        self._fh = self._path.open("xb")
        self._zw = zstandard.ZstdCompressor(level=self._level).stream_writer(self._fh)
        self._lines = 0

    def flush(self) -> None:
        # Only when something was written: an empty flush still emits a 9-byte frame,
        # which kept the file's mtime fresh through every websocket silence and blinded
        # the gapwatch heartbeat (243 min of holes booked as 14 on 2026-08-20..22).
        if self._zw is not None and self._dirty:
            self._zw.flush(zstandard.FLUSH_FRAME)
            self._fh.flush()
            os.fsync(self._fh.fileno())
            self._dirty = False

    def close(self) -> None:
        if self._zw is None:
            return
        self._zw.close()
        self._fh.close()
        path, lines = self._path, self._lines
        self._zw = self._fh = None
        if lines == 0:
            return
        digest, size = sha256_file(path)
        entry = ManifestEntry(
            stream=self.stream,
            path=str(path.relative_to(self.root)),
            lines=lines,
            bytes=size,
            sha256=digest,
            first_seq=self._first_seq,
            last_seq=self.seq - 1,
            first_ingest=self._first_ingest,
            last_ingest=self._last_ingest,
            closed_at=now_ns(),
        )
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / MANIFEST).open("a") as fh:
            fh.write(json.dumps(entry.__dict__, separators=(",", ":")) + "\n")

    def __enter__(self) -> "WormWriter":
        return self

    def __exit__(self, *_) -> None:
        self.close()


def _tail_seqs(path: Path):
    dec = zstandard.ZstdDecompressor()
    try:
        with path.open("rb") as fh, dec.stream_reader(fh) as reader:
            buf = b""
            while chunk := reader.read(1 << 20):
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    if line:
                        yield json.loads(line)["seq"]
            if buf.strip():
                yield json.loads(buf)["seq"]
    except (zstandard.ZstdError, json.JSONDecodeError):
        # A truncated tail from a hard kill: the audit reports it; recovery is
        # not this writer's job.
        return
