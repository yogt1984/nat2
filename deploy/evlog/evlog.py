"""evlog — point-in-time record of when public information became public.

Capture only (REDUCED_SPECS §3 A5). Appends dual-timestamped NDJSON records to
``data/events/YYYY-MM-DD.ndjson``; ``receipt_ts`` is a monotonic-anchored wall
clock in ns, because the whole value of this file is *when we saw it*. Closed
day files get a sha256 + line count in ``data/events/_manifest.jsonl`` — the
same idiom as ``io/worm.py``, copied not imported: this tool shares nat2's data
directory and file discipline, never its code.

    evlog.py once     poll every source, append new records, close old files
    evlog.py report   counts per source and the last receipt, as JSON

No analysis happens here. The moment this file classifies or scores an event it
has jumped the §3b pre-registration gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sources import SOURCES  # noqa: E402

SCHEMA = 1
ROOT = Path(os.environ.get("NAT2_HOME", Path(__file__).resolve().parents[2]))
EVENTS = ROOT / "data" / "events"
MANIFEST = EVENTS / "_manifest.jsonl"
STATE = ROOT / "data" / "ops" / "evlog_state.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) nat2-evlog/0.1"}
MAX_SEEN = 5000

# Wall clock anchored once per process; monotonic deltas from there, so a clock
# step mid-poll cannot reorder receipts.
_ANCHOR_NS = time.time_ns() - time.monotonic_ns()


def receipt_ns() -> int:
    return _ANCHOR_NS + time.monotonic_ns()


def fetch(url: str) -> str:
    r = httpx.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
    r.raise_for_status()
    return r.text


def load_state() -> dict:
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"seen": [], "last_poll_ns": 0, "errors": {}}


def save_state(state: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, STATE)


def day_file(ts_ns: int) -> Path:
    return EVENTS / (datetime.fromtimestamp(ts_ns / 1e9, timezone.utc).strftime("%Y-%m-%d") + ".ndjson")


def append_records(records: list[dict]) -> None:
    EVENTS.mkdir(parents=True, exist_ok=True)
    by_file: dict[Path, list[str]] = {}
    for rec in records:
        by_file.setdefault(day_file(rec["receipt_ts"]), []).append(json.dumps(rec, separators=(",", ":")))
    for path, lines in by_file.items():
        with path.open("a") as fh:
            fh.write("\n".join(lines) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def poll_all(state: dict, fetch_fn=fetch, sources=SOURCES, now_ns=receipt_ns) -> list[dict]:
    """Run every source; unseen event_ids become records. One source failing
    never blocks another — the failure is recorded in state, not raised."""
    seen = set(state["seen"])
    fresh: list[dict] = []
    for name, poll in sources:
        try:
            partial = poll(fetch_fn)
            state["errors"].pop(name, None)
        except Exception as exc:  # noqa: BLE001 — any source failure is data, not a crash
            state["errors"][name] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        for p in partial:
            if p["event_id"] in seen:
                continue
            seen.add(p["event_id"])
            fresh.append({"schema": SCHEMA, "class": p["class"], "source": name, "event_id": p["event_id"],
                          "source_ts": p["source_ts"], "receipt_ts": now_ns(), "payload": p["payload"]})
    state["seen"] = (state["seen"] + [r["event_id"] for r in fresh])[-MAX_SEEN:]
    return fresh


def close_finished_days(today: str) -> list[dict]:
    """Every day file older than today that is not yet in the manifest gets
    its sha256 + line count appended. Idempotent."""
    done = set()
    if MANIFEST.exists():
        for line in MANIFEST.read_text().splitlines():
            try:
                done.add(json.loads(line)["path"])
            except (json.JSONDecodeError, KeyError):
                continue
    closed = []
    for path in sorted(EVENTS.glob("????-??-??.ndjson")):
        rel = str(path.relative_to(ROOT))
        if path.stem >= today or rel in done:
            continue
        h, lines = hashlib.sha256(), 0
        with path.open("rb") as fh:
            for chunk in fh:
                h.update(chunk)
                lines += 1
        entry = {"stream": "events", "path": rel, "lines": lines, "bytes": path.stat().st_size,
                 "sha256": h.hexdigest(), "closed_at": receipt_ns()}
        with MANIFEST.open("a") as fh:
            fh.write(json.dumps(entry, separators=(",", ":")) + "\n")
        closed.append(entry)
    return closed


def cmd_once() -> int:
    state = load_state()
    fresh = poll_all(state)
    if fresh:
        append_records(fresh)
    state["last_poll_ns"] = receipt_ns()
    save_state(state)
    closed = close_finished_days(datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    print(json.dumps({"new": len(fresh), "closed": [c["path"] for c in closed], "errors": state["errors"]}))
    return 0


def cmd_report() -> int:
    counts: dict[str, int] = {}
    last = 0
    for path in EVENTS.glob("????-??-??.ndjson"):
        for line in path.read_text().splitlines():
            rec = json.loads(line)
            counts[rec["source"]] = counts.get(rec["source"], 0) + 1
            last = max(last, rec["receipt_ts"])
    state = load_state()
    print(json.dumps({"records": counts, "last_receipt_ns": last, "last_poll_ns": state["last_poll_ns"],
                      "errors": state["errors"]}, indent=1))
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "once"
    sys.exit({"once": cmd_once, "report": cmd_report}[cmd]())
