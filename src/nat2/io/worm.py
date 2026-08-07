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
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        entry = ManifestEntry.from_json(json.loads(line))
        if stream is None or entry.stream == stream:
            out.append(entry)
    return out


def read_records(root: Path, stream: str, since_ns: int | None = None):
    """Yield records for a stream in file order, including the open file."""
    root = Path(root)
    dec = zstandard.ZstdDecompressor()
    for path in sorted((root / stream).rglob(f"*{SUFFIX}")):
        with path.open("rb") as fh, dec.stream_reader(fh) as reader:
            buf = b""
            while chunk := reader.read(1 << 20):
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    if not line:
                        continue
                    rec = json.loads(line)
                    if since_ns is None or rec["t_ingest"] >= since_ns:
                        yield rec
            if buf.strip():
                rec = json.loads(buf)
                if since_ns is None or rec["t_ingest"] >= since_ns:
                    yield rec


class WormWriter:
    """One writer per stream.  Hourly rotation, checksummed on close."""

    def __init__(self, root: Path, stream: str, level: int = 3):
        self.root = Path(root)
        self.stream = stream
        self._level = level
        self._hour: str | None = None
        self._path: Path | None = None
        self._fh = None
        self._zw = None
        self._lines = 0
        self._first_seq = 0
        self._first_ingest = 0
        self._last_ingest = 0
        self.seq = self._resume_seq()

    def _resume_seq(self) -> int:
        entries = read_manifest(self.root, self.stream)
        last = max((e.last_seq for e in entries), default=-1)
        # An open (unmanifested) file from a previous run may hold higher seqs.
        for path in sorted((self.root / self.stream).rglob(f"*{SUFFIX}")):
            if any(e.path == str(path.relative_to(self.root)) for e in entries):
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
        if self._zw is not None:
            self._zw.flush(zstandard.FLUSH_FRAME)
            self._fh.flush()
            os.fsync(self._fh.fileno())

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
