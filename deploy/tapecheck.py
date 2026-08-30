#!/usr/bin/env python3
"""nat2 tapecheck — what the tape lost, and why (hetzner_plan task 10).

gapwatch answers "is it live now". This answers "what did the record lose over
a window, and which of those losses is a bug" — which is the question the
seven-day count and the cutover both actually need.

Stdlib only, on `/usr/bin/python3`. A dead venv is one of the failures this
reports, so it must not need one.

It owns `holes(entries, since_ns, until_ns, min_s)`. gapwatch (task 19) and the
clean-day scorer (task 20) import it rather than each growing their own
detector, because two implementations of "hole" that disagree is worse than
either of them alone.

Three things here are less obvious than they look.

*   **A hole is measured `prev.last_ingest -> next.first_ingest`.** Of the nine
    ways to pair the manifest's three timestamps, only this one is meaningful:
    `closed_at` is stamped *after* the successor has already begun ingesting, so
    it yields negative gaps, and `first_ingest -> first_ingest` measures part
    length rather than absence.

*   **Snapshot streams never get a hole count.** `nat2.liqmap*` open and close a
    writer per snapshot (io/mapsnap.py), so `first_ingest == last_ingest` and the
    "gap" between parts *is* the cadence. Counting holes there yields 6,460
    phantom ones on `nat2.liqmap2` alone. They get a present-fraction instead.

*   **Causes have a precedence, and it is not the obvious one.** A suspend ends
    with a socket close, so a classifier that reads the error counters first
    calls eight confirmed host pauses "venue". Host-pause is resolved first and
    absorbs everything inside its span.

Only `unknown` is a bug. `unavailable` is not: the journal is size-capped and
the store is not, so holes eventually age out of anything that could explain
them, and saying so is honest where guessing is not.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "raw" / "_manifest.jsonl"
RAW = ROOT / "data" / "raw"
LEDGER = ROOT / "data" / "ledger.jsonl"
SUFFIX = ".ndjson.zst"
NS = 1_000_000_000

# A long-lived writer rotating on the hour: consecutive parts abut (median 0.42 s),
# so absence between them is a real hole.
CONTINUOUS = ("hl.trades", "hl.l2book", "hl.assetctxs")
# One writer per snapshot, so every part is a single line with zero span. The
# cadence is `mapsnap_interval_ns = 60 * NS` at src/nat2/io/cycle.py:50, but it
# is measured from the data here rather than restated, so a changed interval
# cannot silently invalidate the reading.
SNAPSHOT = ("nat2.liqmap", "nat2.liqmap2")

PREREG_NAME = "tapecheck_v1"
FLOOR_KEY = "hole_floor_s"

CAUSES = ("host-pause", "recycle", "disk", "stall-exit", "local-network", "venue",
          "unavailable", "unknown")
# Resolved in this order, and the first match wins. Host-pause is first because
# a resumed process closes its sockets, which otherwise reads as a venue fault.
# `disk` sits above `local-network` because capture reports a full store as an
# OSError, and OSError is a local-network marker -- without this every
# disk-full hole would be filed as a network problem.
PRECEDENCE = ("host-pause", "recycle", "disk", "stall-exit", "local-network", "venue")

RECYCLE_MARK = "Service reached runtime time limit"
STALL_MARK = "capture stalled:"
# The store said no. `CaptureWriteFailed` names the stream and the root, which
# is the whole point of it being distinct from a stall.
DISK_MARK = "cannot write"
# `reason()` (core/errors.py) collapses an exception to its type name, so the
# vocabulary in the journal is exception classes. getaddrinfo and ENETUNREACH
# can only originate on this side of the wire; a timeout or a closed socket
# cannot distinguish itself from the venue being down.
LOCAL_MARKS = ("gaierror", "OSError")
# Bare "RuntimeError" is not here on purpose: it is the most common exception
# name in this journal and would make `venue` absorb everything, leaving
# `unknown` -- the only signal that means "bug" -- unreachable. The venue's own
# failure has a specific shape.
VENUE_MARKS = ("TimeoutError", "ConnectionClosedError", "metaAndAssetCtxs failed")
# Anchored to an exception name, because `reason()` renders "<Name> <status>".
# A bare \b5\d\d\b matches PIDs, byte counts and D-Bus names: an ordinary
# journal hour on this box contains 546, 565, 555, 545 and 515.
HTTP_5XX = re.compile(r"[A-Za-z]Error 5\d\d\b")


# --- the manifest ----------------------------------------------------------

def read_manifest(path: Path = MANIFEST) -> list[dict]:
    """Every manifest record, oldest append first, torn lines tolerated.

    Mirrors `nat2.io.worm.read_manifest` deliberately -- this file may not
    import it, because a broken venv is one of the conditions it reports on.
    """
    if not path.exists():
        return []
    out = []
    lines = path.read_text(errors="replace").splitlines()
    for i, line in enumerate(lines):
        record = line.strip("\x00 \t\r\n�")
        if not record:
            continue
        try:
            out.append(json.loads(record))
        except json.JSONDecodeError:
            # A crash leaves NUL-filled blocks and a part-written record. That
            # is incomplete, not corrupt. Anything malformed without the crash
            # signature is corruption and is surfaced rather than swallowed.
            if "\x00" in line or "�" in line or i == len(lines) - 1:
                continue
            raise
    return out


def for_stream(entries: list[dict], stream: str) -> list[dict]:
    """One stream's entries in time order.

    Sorted rather than trusted: two real appends land out of order (the
    snapshot streams each carry one, 149.7 s backwards), and differencing in
    append order would emit a negative gap.
    """
    return sorted((e for e in entries if e["stream"] == stream),
                  key=lambda e: e["first_ingest"])


def in_window(entries: list[dict], since_ns: int, until_ns: int) -> list[dict]:
    """Entries overlapping the window. The entry *preceding* the window is not
    reached back for: it would book the dead time before `--since` as a hole."""
    return [e for e in entries
            if e["last_ingest"] >= since_ns and e["first_ingest"] <= until_ns]


# --- the thing this file owns ----------------------------------------------

def holes(entries: list[dict], since_ns: int, until_ns: int,
          min_s: float, stream: str | None = None) -> list[dict]:
    """Absences between consecutive parts of one stream, over the floor.

    `entries` is one stream's records. The gap runs from where a part stopped
    ingesting to where the next started; anything shorter than `min_s` is a
    rotation seam, not a loss.

    Edges are deliberately not holes. A window that opens before the stream did
    would otherwise book the wait as data loss -- real downtime, but not
    something the tape lost, and `no_data_before`/`no_data_after` carry it
    instead.
    """
    # Defensive because this is the exported surface: tasks 19 and 20 import it,
    # and the spec spells it `holes(manifest, ...)`. Handed an unsorted or
    # multi-stream list it would otherwise difference one stream against
    # another and return confident nonsense.
    if stream is not None:
        entries = for_stream(entries, stream)
    elif len({e["stream"] for e in entries}) > 1:
        raise ValueError("holes() needs one stream; pass stream= to select it")
    else:
        entries = sorted(entries, key=lambda e: e["first_ingest"])
    window = in_window(entries, since_ns, until_ns)
    out = []
    for prev, nxt in zip(window, window[1:]):
        gap_ns = nxt["first_ingest"] - prev["last_ingest"]
        if gap_ns < 0:
            # Overlapping parts are their own defect, not an absence.
            continue
        if gap_ns / NS >= min_s:
            out.append({
                "from_ns": prev["last_ingest"],
                "to_ns": nxt["first_ingest"],
                "seconds": gap_ns / NS,
                "after": prev["path"],
                "before": nxt["path"],
            })
    return out


def gap_minutes(found: list[dict]) -> float:
    return sum(h["seconds"] for h in found) / 60.0


# --- the other per-stream checks -------------------------------------------

def seq_breaks(entries: list[dict]) -> list[dict]:
    """Where the record numbering does not chain. A forward jump means records
    left no part; a backward one means a part was rewritten, which is worse."""
    out = []
    for prev, nxt in zip(entries, entries[1:]):
        expected = prev["last_seq"] + 1
        if nxt["first_seq"] == expected:
            continue
        out.append({
            "kind": "gap" if nxt["first_seq"] > expected else "overlap",
            "missing": nxt["first_seq"] - expected,
            "after": prev["path"],
            "before": nxt["path"],
        })
    return out


def orphans(entries: list[dict], raw_root: Path = RAW) -> dict:
    """Parts on disk that no manifest entry claims, and entries whose file is
    gone. The manifest line is appended at clean writer close, so any unclean
    death leaves its open part outside the checksum guarantee -- the data is
    usually still there, which is why this is reported separately from holes
    rather than folded into them."""
    claimed = {e["path"] for e in entries}
    found = {}
    if raw_root.exists():
        for part in raw_root.rglob(f"*{SUFFIX}"):
            rel = str(part.relative_to(raw_root))
            if rel not in claimed:
                found[rel] = int(part.stat().st_mtime * NS)
    return {
        "unmanifested": dict(sorted(found.items())),
        "missing_file": sorted(p for p in claimed if not (raw_root / p).exists()),
    }


def recoverable(hole: dict, stream: str, unmanifested: dict) -> bool:
    """Whether an unmanifested part was still being written inside this hole.

    An absence in the manifest is not the same as an absence on disk. The
    manifest line is appended only at a clean writer close, so an unclean death
    leaves its part orphaned -- outside the checksum guarantee, but present.
    Distinguishing the two is the difference between "the tape lost this" and
    "the tape cannot prove it kept this", and roughly 55 of the acceptance
    window's 1251 gap-minutes are the second kind.
    """
    for path, mtime in unmanifested.items():
        if path.startswith(f"{stream}/") and hole["from_ns"] <= mtime <= hole["to_ns"]:
            return True
    return False


def _lead_in(window: list[dict], series: list[dict],
             since_ns: int, until_ns: int) -> float | None:
    """Seconds at the start of the window with no data. Clamped at zero: a part
    that straddles `since` means there was no absence, not a negative one."""
    if window:
        return round(max(0.0, (min(e["first_ingest"] for e in window) - since_ns) / NS), 1)
    return _whole_window(series, since_ns, until_ns)


def _lead_out(window: list[dict], series: list[dict],
              since_ns: int, until_ns: int) -> float | None:
    if window:
        return round(max(0.0, (until_ns - max(e["last_ingest"] for e in window)) / NS), 1)
    return _whole_window(series, since_ns, until_ns)


def _whole_window(series: list[dict], since_ns: int, until_ns: int) -> float | None:
    """With no part in the window the absence is the window itself -- but only
    for a stream that exists at all, so a stream born later is not accused of
    losing time before it started."""
    if not series:
        return None
    return round((until_ns - since_ns) / NS, 1)


def parts_per_hour(entries: list[dict], since_ns: int, until_ns: int) -> dict:
    """Parts closed per UTC hour, and the hours with none. For an hourly stream
    a silent hour is the coarse shape of a hole; it is cheap corroboration that
    does not depend on the floor."""
    counts: Counter = Counter()
    for e in in_window(entries, since_ns, until_ns):
        counts[_hour(e["first_ingest"])] += 1
    hours = _hours_between(since_ns, until_ns)
    return {
        "median": _median([counts.get(h, 0) for h in hours]) if hours else 0,
        "silent_hours": [h for h in hours if counts.get(h, 0) == 0],
        "busiest": max(counts.values()) if counts else 0,
    }


def spacing(entries: list[dict], since_ns: int, until_ns: int) -> dict:
    """What a snapshot stream gets instead of a hole count.

    `present_frac` is observed samples over expected samples, where expected is
    the covered span divided by the measured median cadence. It is the metric
    that behaves: over the 16 h the box was off, an on-time-interval score reads
    97.7% because the few intervals that exist are punctual, while this reads
    19.8% because 1,080 samples are simply not there.
    """
    window = in_window(entries, since_ns, until_ns)
    if len(window) < 2:
        # One sample cannot establish a cadence, and zero cannot establish
        # anything -- but "no parts in this window" is itself the finding.
        return {"parts": len(window), "cadence_s": None, "present_frac": None,
                "absent": not window and bool(entries)}
    deltas = [(b["first_ingest"] - a["first_ingest"]) / NS
              for a, b in zip(window, window[1:])]
    cadence = _median(sorted(deltas))
    # Against the window clipped to the stream's own lifetime, NOT against the
    # span of the samples that survived. Anchoring to first-and-last-observed
    # makes a stream that stopped halfway through score near-perfect, because
    # the part that is missing is exactly the part not measured.
    # Clipped at the stream's birth but NOT at its death. A stream cannot lose
    # data before it existed, so counting from `since` would slander a stream
    # that simply started later. But a stream that STOPS mid-window has lost
    # exactly the part that is missing, and clipping there would score it
    # perfect for the same reason it deserves to score badly.
    born = entries[0]["first_ingest"]
    covered = (until_ns - max(since_ns, born)) / NS
    expected = covered / cadence + 1 if cadence and covered > 0 else 0
    return {
        "parts": len(window),
        "cadence_s": round(cadence, 3),
        "present_frac": round(len(window) / expected, 4) if expected else None,
        "p99_spacing_s": round(sorted(deltas)[int(len(deltas) * 0.99)], 1),
        "max_spacing_s": round(max(deltas), 1),
        "absent": False,
    }


# --- causes ----------------------------------------------------------------

def _journal(since_ns: int, until_ns: int, user: bool) -> list[dict]:
    cmd = ["journalctl", "-o", "json", "--no-pager",
           "--output-fields=MESSAGE,_PID,_BOOT_ID,__REALTIME_TIMESTAMP,__MONOTONIC_TIMESTAMP",
           "--since", _stamp(since_ns), "--until", _stamp(until_ns)]
    if user:
        cmd.insert(1, "--user")
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in done.stdout.splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def journal_reaches(since_ns: int) -> bool:
    """Whether the journal still covers a moment. The store grows forever and
    the journal is size-capped, so this stops being true for old windows --
    which is what separates `unavailable` from `unknown`."""
    try:
        done = subprocess.run(["journalctl", "--list-boots", "-o", "json", "--no-pager"],
                              capture_output=True, text=True, timeout=60)
        boots = json.loads(done.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
        return False
    starts = [b["first_entry"] for b in boots if isinstance(b.get("first_entry"), int)]
    return bool(starts) and min(starts) * 1000 <= since_ns


def _paused(records: list[dict], covers_s: float) -> bool:
    """A suspend or power-off: wall clock advanced further than the monotonic
    clock between two records of the same boot. Read from the journal's own
    timestamps rather than from `PM: suspend` kernel lines, which need group
    membership this script cannot assume."""
    prev = None
    for rec in records:
        try:
            real = int(rec["__REALTIME_TIMESTAMP"])
            mono = int(rec["__MONOTONIC_TIMESTAMP"])
            boot = rec.get("_BOOT_ID")
        except (KeyError, ValueError, TypeError):
            continue
        if prev and prev[2] == boot:
            skew = (real - prev[0]) - (mono - prev[1])
            # Half the hole, deliberately relative: a two-second suspend does
            # not explain a five-hour absence, and tying this to the ledgered
            # floor would let a policy change silently rewrite past diagnoses.
            if skew / 1e6 >= covers_s / 2:
                return True
        prev = (real, mono, boot)
    return False


def _messages(records: list[dict]) -> str:
    parts = []
    for rec in records:
        msg = rec.get("MESSAGE")
        if isinstance(msg, list):
            msg = "".join(chr(b) for b in msg if isinstance(b, int))
        if msg:
            parts.append(str(msg))
    # Rejoined, because rich wraps a stall message across three journal records
    # and the exception vocabulary straddles the break.
    return "\n".join(parts)


def classify(hole: dict, reachable: bool, margin_s: float = 180.0) -> str:
    if not reachable:
        return "unavailable"
    lo = hole["from_ns"] - int(margin_s * NS)
    hi = hole["to_ns"] + int(margin_s * NS)
    records = _journal(lo, hi, user=True) + _journal(lo, hi, user=False)
    if not records:
        return "unavailable"
    text = _messages(records)
    verdict = {
        "host-pause": _paused(records, hole["seconds"]),
        "recycle": RECYCLE_MARK in text,
        "disk": DISK_MARK in text,
        "stall-exit": STALL_MARK in text,
        "local-network": any(m in text for m in LOCAL_MARKS),
        "venue": any(m in text for m in VENUE_MARKS) or bool(HTTP_5XX.search(text)),
    }
    for cause in PRECEDENCE:
        if verdict[cause]:
            return cause
    return "unknown"


def _boots_within(records: list[dict]) -> int:
    return len({r.get("_BOOT_ID") for r in records if r.get("_BOOT_ID")})


# --- policy ----------------------------------------------------------------

def preregistered_floor(path: Path = LEDGER) -> float | None:
    """The hole floor, from the ledger.

    It is policy, not a repo constant: the acceptance window cannot tell 60 s
    from 90 s -- every floor in (46.4, 101.7] yields the same 26 holes -- so a
    number chosen here would be unfalsifiable and look measured. It is read
    from the newest `tapecheck_v1` pre-registration, and this refuses to run
    without one.
    """
    if not path.exists():
        return None
    floor = None
    for line in path.read_text(errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        payload = entry.get("payload") or {}
        if entry.get("kind") == "preregistration" and payload.get("name") == PREREG_NAME:
            value = payload.get(FLOOR_KEY)
            if isinstance(value, (int, float)):
                floor = float(value)
    return floor


# --- small helpers ---------------------------------------------------------

def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _hour(ns: int) -> str:
    return datetime.fromtimestamp(ns / NS, timezone.utc).strftime("%Y-%m-%dT%H")


def _hours_between(since_ns: int, until_ns: int) -> list[str]:
    out, cursor = [], since_ns - since_ns % (3600 * NS)
    while cursor <= until_ns:
        out.append(_hour(cursor))
        cursor += 3600 * NS
    return out


def _stamp(ns: int) -> str:
    """journalctl's `@<epoch>` form. A bare timestamp is parsed in LOCAL time
    even when the output is `--utc`, and this box is Europe/Rome -- so a naive
    ISO string would shift every window by two hours and classify the wrong
    journal against each hole. Seconds since the epoch cannot be misread."""
    return f"@{int(ns // NS)}"


def _iso(ns: int) -> str:
    return datetime.fromtimestamp(ns / NS, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(text: str) -> int:
    cleaned = text.strip().replace("Z", "+00:00")
    moment = datetime.fromisoformat(cleaned)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * NS)


# --- the report ------------------------------------------------------------

def check(since_ns: int, until_ns: int, min_s: float, classify_causes: bool = True,
          manifest: Path = MANIFEST, raw_root: Path = RAW) -> dict:
    entries = read_manifest(manifest)
    reachable = journal_reaches(since_ns) if classify_causes else None
    orphaned = orphans(entries, raw_root)
    report: dict = {
        "since": _iso(since_ns),
        "until": _iso(until_ns),
        "min_s": min_s,
        "journal_reaches_window": reachable,
        "causes_classified": classify_causes,
        "orphans": {
            "unmanifested": len(orphaned["unmanifested"]),
            "missing_file": len(orphaned["missing_file"]),
            "paths": sorted(orphaned["unmanifested"])[:20],
        },
        "streams": {},
    }
    for stream in CONTINUOUS + SNAPSHOT:
        series = for_stream(entries, stream)
        window = in_window(series, since_ns, until_ns)
        common = {
            "kind": "snapshot" if stream in SNAPSHOT else "continuous",
            "parts": len(window),
            "seq_breaks": seq_breaks(window),
            "no_data_before_s": _lead_in(window, series, since_ns, until_ns),
            "no_data_after_s": _lead_out(window, series, since_ns, until_ns),
            # A stream with no part at all in the window is the loudest thing
            # this tool can find, and it is invisible in a hole count: holes are
            # measured *between* parts, so zero parts means zero holes.
            "absent": not window and bool(series),
        }
        if stream in SNAPSHOT:
            # No hole count, by construction. See the module docstring.
            common["spacing"] = spacing(series, since_ns, until_ns)
        else:
            found = holes(series, since_ns, until_ns, min_s)
            causes: Counter = Counter()
            for hole in found:
                hole["recoverable"] = recoverable(hole, stream, orphaned["unmanifested"])
                hole["cause"] = classify(hole, bool(reachable)) if classify_causes \
                    else "unclassified"
                causes[hole["cause"]] += 1
            common.update({
                "holes": len(found),
                "gap_minutes": round(gap_minutes(found), 4),
                # Split out, never subtracted: the headline figure is what the
                # window lost, and an orphan is unproven rather than absent.
                "gap_minutes_on_disk": round(
                    gap_minutes([h for h in found if h["recoverable"]]), 4),
                "causes": dict(causes),
                "unknown": causes.get("unknown", 0),
                "parts_per_hour": parts_per_hour(series, since_ns, until_ns),
                "detail": [
                    {**h, "from": _iso(h["from_ns"]), "to": _iso(h["to_ns"]),
                     "seconds": round(h["seconds"], 3)}
                    for h in found
                ],
            })
        report["streams"][stream] = common
    report["absent_streams"] = sorted(
        name for name, data in report["streams"].items()
        if data.get("absent") or (data.get("spacing") or {}).get("absent"))
    report["unavailable"] = sum(
        data.get("causes", {}).get("unavailable", 0) for data in report["streams"].values())
    # `unknown` is the bug signal the spec names. An absent stream is not a hole
    # and so has no cause at all, but it is the largest loss this tool can find
    # -- it must never leave by the same exit code as a clean run.
    report["unknown"] = sum(data.get("unknown", 0) for data in report["streams"].values())
    report["bug"] = bool(report["unknown"]) or bool(report["absent_streams"])
    return report


def render(report: dict) -> str:
    lines = [f"tapecheck {report['since']} .. {report['until']}  floor {report['min_s']:g}s"]
    if not report["causes_classified"]:
        lines.append("  causes not classified (--no-causes): holes are counted, not explained")
    elif not report["journal_reaches_window"]:
        lines.append("  journal does not reach this window -- causes are 'unavailable', not 'unknown'")
    orph = report["orphans"]
    lines.append(f"  orphans: {orph['unmanifested']} unmanifested, {orph['missing_file']} missing file")
    for stream, data in report["streams"].items():
        if data["kind"] == "snapshot":
            sp = data["spacing"]
            if sp.get("absent"):
                lines.append(f"  {stream:<16} snapshot  ABSENT -- no part in this window at all")
                continue
            frac = "n/a" if sp["present_frac"] is None else f"{sp['present_frac'] * 100:.1f}%"
            cadence = "n/a" if sp["cadence_s"] is None else f"{sp['cadence_s']:g}s"
            lines.append(f"  {stream:<16} snapshot  {data['parts']:>6} parts  "
                         f"present {frac}  cadence {cadence}")
        else:
            causes = ", ".join(f"{k} {v}" for k, v in sorted(data["causes"].items())) or "-"
            if data.get("absent"):
                span = data["no_data_before_s"]
                lines.append(f"  {stream:<16} ABSENT -- no part in this window at all"
                             f"{f' ({span / 3600:.1f}h)' if span else ''}")
                continue
            recov = data["gap_minutes_on_disk"]
            note = f"  ({recov:.1f} still on disk, unmanifested)" if recov else ""
            lines.append(f"  {stream:<16} {data['holes']:>4} holes  "
                         f"{data['gap_minutes']:>9.1f} gap-min  [{causes}]{note}")
        if data["seq_breaks"]:
            lines.append(f"      seq breaks: {len(data['seq_breaks'])}")
    if report["absent_streams"]:
        verdict = "a stream is absent for the whole window -- " + ", ".join(report["absent_streams"])
    elif not report["causes_classified"]:
        verdict = "no verdict -- causes were not classified"
    elif report["unknown"]:
        verdict = "a hole is unexplained -- investigate"
    elif report["unavailable"]:
        verdict = (f"{report['unavailable']} hole(s) predate the journal and cannot be "
                   "explained -- not a bug, but not accounted for either")
    else:
        verdict = "every hole is accounted for"
    lines.append("  VERDICT: " + verdict)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="what the tape lost, and why")
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--min-s", type=float, default=None,
                        help="hole floor in seconds; without it the ledgered "
                             f"{PREREG_NAME} floor is used and this refuses if absent")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-causes", action="store_true",
                        help="skip the journal entirely (fast, holes stay unclassified)")
    args = parser.parse_args(argv)

    floor, ledgered = args.min_s, False
    if floor is None:
        floor = preregistered_floor()
        ledgered = floor is not None
        if floor is None:
            print(
                f"refusing: no {PREREG_NAME} pre-registration carries '{FLOOR_KEY}'.\n"
                "The hole floor is policy, not a repo constant -- this window cannot\n"
                "distinguish 60s from 90s, so a number chosen in code would look\n"
                "measured without being falsifiable. Register it first:\n"
                f"  nat2 log add --kind preregistration --json '{{\"name\": \"{PREREG_NAME}\", "
                f"\"{FLOOR_KEY}\": <seconds>, \"rationale\": \"...\"}}'\n"
                "or pass --min-s to explore without recording a verdict.",
                file=sys.stderr)
            return 2

    report = check(parse_ts(args.since), parse_ts(args.until), floor,
                   classify_causes=not args.no_causes)
    report["floor_preregistered"] = ledgered
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(render(report))
        if not ledgered:
            print("  (floor passed on the command line, not pre-registered)")
    return 1 if report["bug"] else 0


if __name__ == "__main__":
    sys.exit(main())
