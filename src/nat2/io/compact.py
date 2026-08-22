"""Compaction: closed WORM files -> Parquet, for the read side.

Only manifested (closed) files are compacted.  The open file is skipped by
construction, so compaction can never race the writer or capture a torn tail.

Payloads stay as a JSON string column.  Typing them here would mean deciding
what HL's messages mean at compaction time, before any feature code has an
opinion -- and a schema change at the venue would then corrupt history rather
than just the parse.  Structure is imposed on read, per stream.
"""

from __future__ import annotations

import json
from pathlib import Path

import polars as pl

from nat2.io.worm import read_manifest
from nat2.io.worm import SUFFIX  # noqa: F401  (documents the raw file suffix)
import zstandard


def _read_file(path: Path) -> list[dict]:
    dec = zstandard.ZstdDecompressor()
    rows = []
    with path.open("rb") as fh, dec.stream_reader(fh) as reader:
        data = reader.read()
    for line in data.split(b"\n"):
        if line.strip():
            rows.append(json.loads(line))
    return rows


def compact(root: Path, out: Path, streams: list[str] | None = None) -> list[dict]:
    """Compact every closed file not already present in the Parquet tree."""
    root, out = Path(root), Path(out)
    written = []
    for entry in read_manifest(root):
        if streams and entry.stream not in streams:
            continue
        src = root / entry.path
        dst = out / entry.stream / (Path(entry.path).name.replace(SUFFIX, ".parquet"))
        if dst.exists() or not src.exists():
            continue
        rows = _read_file(src)
        if not rows:
            continue
        frame = pl.DataFrame(
            {
                "seq": [r["seq"] for r in rows],
                "t_ingest": [r["t_ingest"] for r in rows],
                "t_event": [r.get("t_event") for r in rows],
                "payload": [json.dumps(r["payload"], separators=(",", ":")) for r in rows],
            },
            schema={"seq": pl.Int64, "t_ingest": pl.Int64, "t_event": pl.Int64, "payload": pl.Utf8},
        )
        dst.parent.mkdir(parents=True, exist_ok=True)
        frame.write_parquet(dst, compression="zstd")
        written.append({"stream": entry.stream, "path": str(dst), "rows": len(rows)})
    return written


def raw_covers_parquet(root: Path, out: Path) -> list[str]:
    """Parquet parts whose raw file is gone. The read side (`nat2 eval`, `gate magnet`) reads
    raw only, so a pruned raw root would silently shrink every evaluation window; until a
    retention policy exists (TASK_2/14: raw is never pruned), a non-empty answer is a refusal."""
    missing = []
    for parquet in sorted(Path(out).rglob("*.parquet")):
        stream = parquet.parent.name
        name = parquet.name.replace(".parquet", SUFFIX)
        if "-" not in name or stream == Path(out).name:
            continue                                     # derived frames, not compacted parts
        day = name.split("-", 1)[1][:8]
        raw = Path(root) / stream / f"{day[:4]}-{day[4:6]}-{day[6:8]}" / name
        if not raw.exists():
            missing.append(f"{stream}/{name}")
    return missing

