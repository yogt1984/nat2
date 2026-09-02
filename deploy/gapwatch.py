"""nat2 gapwatch — manifest-derived gap detection with ntfy.sh alerts.

TASK_2/TASKS/01. The REDUCED_SPECS §7.2 uptime criterion is defined against
``_manifest.jsonl``, and this file computes both the metric and the alarm from
the same code so they cannot disagree. Edge-triggered like nat's proven
``gap_alert.py``: one alert when a condition opens, one on recovery, never
per-tick. Stdlib only — this watchdog must not share the deleted-venv failure
mode it exists to catch. READ-ONLY: it stats files and queries systemd, never
mutates anything but its own state JSON.

CLI: ``gapwatch.py check`` (systemd timer, 5 min) · ``gapwatch.py report``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "raw" / "_manifest.jsonl"
LEDGER = ROOT / "data" / "ledger.jsonl"
STATE_PATH = ROOT / "data" / "ops" / "gapwatch_state.json"
EVLOG_STATE = ROOT / "data" / "ops" / "evlog_state.json"
EVLOG_CADENCE_S = 300.0  # nat2-evlog.timer; the events stream is a poll, not a file rotation

# Cadences measured from manifest history 2026-08-07..20 (median inter-entry
# interval): ws streams rotate hourly (3600 s median, n=84 for trades and
# assetctxs; l2book rotates hourly too but has n=1 closed-file history — it was
# absent from the captured stream set 08-08..08-13); liqmap entries land at
# least once per 1 h scan pass. A gap opens at age > GAP_FACTOR x cadence.
CADENCE_S = {
    "hl.trades": 3600.0,
    "hl.l2book": 3600.0,
    "hl.assetctxs": 3600.0,
    # Re-measured 2026-09-02 over the last 400 parts of each stream: the ws
    # streams still rotate hourly (3599.8 / 3598.0 / 3596.1 s medians), but the
    # snapshot streams land every 64.5 s, not hourly. The 3600 here was true
    # when liqmap was written once per scan pass and stopped being true when
    # `mapsnap_interval_ns` became 60 s (io/cycle.py) -- 56x too slow, so an
    # 18-hour liqmap outage paged after two hours instead of ten minutes.
    # 300 s rather than the measured 64.5: five cadences of slack absorbs a
    # slow pass without crying wolf, and GAP_FACTOR doubles it again.
    "nat2.liqmap": 300.0,
    # Absent entirely until now, so 210 MB of snapshots went unwatched.
    "nat2.liqmap2": 300.0,
}
GAP_FACTOR = 2.0
# Intra-file holes: a 33-min capture outage on 2026-08-20 18:21-18:54 sat inside
# an hourly file that rotated normally, invisible to the manifest. The writer
# appends to the open .zst every few seconds, so its mtime is a heartbeat.
TAPE_DIR = ROOT / "data" / "raw" / "hl.trades"
TAPE_SILENCE_S = 300.0
# A reboot silences this watchdog with the capture; at its first tick afterwards the open file
# is fresh again and no edge opens (the 55-min hole of 2026-08-21 was booked as 14). So a tick
# this late measures the hole itself: last write before the capture unit's restart, to the restart.
TICK_S = 300.0
WATCHDOG_DOWN_FACTOR = 3.0
NAT2 = ROOT / ".venv" / "bin" / "nat2"   # the ledger is written through the CLI, never from here
ACTIONS = ROOT / "data" / "actions.jsonl"   # L0 ops records, same shape as nat2.io.actions (TASK_2/13)
UNITS = ("nat2-capture.service", "nat2-cycle.service", "nat2-statuspage.timer")
# Recorded into the state JSON for the status page (TASK_2/06 reads files only,
# never queries systemd itself) but not alerted on: nat has its own watchdog.
REPORT_UNITS = ("nat-ingestor.service", "nat2-evlog.timer", "nat2-gapwatch.timer")
DISK_MIN_FREE_GB = 20.0
OBS_SILENCE_S = 7200.0  # 2x the 1 h liquidation-scan cadence
WEEK_BUDGET_MIN = 60.0  # REDUCED_SPECS §7.2: gap-minutes/week per stream
NTFY_ENV = "NAT2_NTFY_TOPIC"

# tapecheck owns the hole definition (hetzner_plan 10). Imported rather than
# re-derived: two implementations of "hole" that disagree is worse than either
# alone, which is why 10 had to land before this.
sys.path.insert(0, str(ROOT / "deploy"))
from tapecheck import CONTINUOUS, for_stream, holes as tc_holes  # noqa: E402
from tapecheck import preregistered_floor, read_manifest as tc_manifest  # noqa: E402

NS = 1_000_000_000


def tail_lines(path: Path, max_bytes: int = 65536) -> list[str]:
    if not path.exists():
        return []
    with path.open("rb") as fh:
        fh.seek(max(0, path.stat().st_size - max_bytes))
        chunk = fh.read().decode(errors="replace")
    lines = chunk.splitlines()
    return lines[1:] if path.stat().st_size > max_bytes else lines  # drop torn first line


def newest_ingest(manifest: Path = MANIFEST) -> dict[str, float]:
    """{stream: newest last_ingest, epoch seconds} from the manifest tail."""
    out: dict[str, float] = {}
    for line in tail_lines(manifest):
        try:
            entry = json.loads(line)
            out[entry["stream"]] = max(out.get(entry["stream"], 0.0), entry["last_ingest"] / 1e9)
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    return out


def tape_heartbeat_s(tape_dir: Path = TAPE_DIR) -> float:
    """mtime of the newest hl.trades file (open or closed), epoch seconds; 0 if none."""
    try:
        return max((f.stat().st_mtime for f in tape_dir.glob("*/*.zst")), default=0.0)
    except OSError:
        return 0.0


def last_observation_s(ledger: Path = LEDGER) -> float | None:
    for line in reversed(tail_lines(ledger)):
        try:
            entry = json.loads(line)
            if entry.get("kind") == "observation":
                return entry["ts"] / 1e9
        except (json.JSONDecodeError, KeyError):
            continue
    return None


def unit_active_since_s(unit: str) -> float | None:
    """Wall-clock epoch at which `unit` last became active; None if unknown.

    Read straight off systemd with `--timestamp=unix` rather than added up from
    `ActiveEnterTimestampMonotonic` plus boot time. CLOCK_MONOTONIC does not
    advance while the box is suspended and /proc/uptime's CLOCK_BOOTTIME does,
    so the two drift apart by however long the machine has slept: measured on
    this host today, **908 minutes** for nat2-capture and 42 for nat2-cycle.
    Every restart reconstructed from the old arithmetic was that far in the
    past, and so was every hole measured from it.

    Needs systemd >= 247 for `--timestamp=unix`; this box runs 255.
    """
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "-p", "ActiveEnterTimestamp",
             "--timestamp=unix", "--value", unit],
            capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return float(out.lstrip("@")) or None
    except ValueError:
        return None


def week_start_s(now_s: float) -> float:
    """Midnight UTC on the Monday of `now_s`'s ISO week.

    The booking window matches the accounting window: `gap_minutes` is keyed by
    ISO week and cleared when the week rolls, so booking further back would
    credit this week with last week's outages.
    """
    moment = datetime.fromtimestamp(now_s, timezone.utc)
    monday = moment - timedelta(days=moment.weekday(), hours=moment.hour,
                                minutes=moment.minute, seconds=moment.second,
                                microseconds=moment.microsecond)
    return monday.timestamp()


def book_holes(state: dict, now_s: float, manifest: Path = MANIFEST,
               ledger: Path = LEDGER) -> list[tuple[str, str]]:
    """Book every manifest hole this ISO week, once each.

    The reconstruction this replaces only ran when the watchdog's own tick was
    >= 900 s late, so every outage it *survived* was invisible: the state books
    62.3 min for ISO-W35 against 758.4 min sitting in the manifest. Absence is
    not less real for having been observed on time.

    Idempotent on the hole's absolute start in nanoseconds. An offset from
    "now" is a different key every tick, which books one outage repeatedly --
    and a counter that grows without an outage is worse than one that misses.

    The floor is policy (`tapecheck_v1`). Without it nothing is booked and the
    watchdog carries on watching: refusing to run would blind the only alarm
    over a number that governs bookkeeping.
    """
    # Drop hole keys for streams that are no longer booked. The snapshot
    # streams were briefly booked by an earlier version of this function, and
    # left 2,771.9 phantom minutes in the W36 counter against a 60-minute
    # budget -- a number the status page and the digest were both reporting.
    # Pruning here rather than by hand because the same thing happens to any
    # week that straddles a change in which streams are booked.
    gaps = state.get("gap_minutes") or {}
    for key in [k for k in gaps if k.endswith(":hole")
                and k.removeprefix("stream:").removesuffix(":hole") not in CONTINUOUS]:
        del gaps[key]

    floor = preregistered_floor(ledger)
    if floor is None:
        return []
    # Not wrapped in a bare `except OSError`. The first version passed a
    # directory where tapecheck wants the manifest file, and the resulting
    # IsADirectoryError -- an OSError -- would have been swallowed into "no
    # holes today", forever, silently. A watchdog that books nothing must fail
    # loudly enough to be noticed; a missing file is the only tolerated case.
    if not manifest.exists():
        return []
    entries = tc_manifest(manifest)

    booked: dict = state.setdefault("booked_holes", {})
    since_ns, until_ns = int(week_start_s(now_s) * NS), int(now_s * NS)
    events: list[tuple[str, str]] = []
    # CONTINUOUS, not CADENCE_S. The cadence table answers "is this stream live
    # now" and rightly includes the snapshot streams; hole booking answers "was
    # there an absence between parts", which for a stream that opens a writer
    # per snapshot is meaningless -- its 64.5 s cadence clears the 60 s floor,
    # so every ordinary gap books as a hole. Measured before this line existed:
    # 7,512 phantom holes and 10,077 phantom minutes per snapshot stream for a
    # single week, which would have buried the 1,817 real ones.
    for stream in CONTINUOUS:
        series = for_stream(entries, stream)
        for hole in tc_holes(series, since_ns, until_ns, floor):
            key = str(hole["from_ns"])
            if key in booked:
                continue                       # per hole, so a NEW one still pages
            minutes = hole["seconds"] / 60
            booked[key] = round(minutes, 3)
            name = f"stream:{stream}:hole"
            gaps = state.setdefault("gap_minutes", {})
            gaps[name] = gaps.get(name, 0.0) + minutes
            state["holes"] = (state.get("holes") or [])[-19:] + [
                {"stream": stream, "from_s": hole["from_ns"] / NS,
                 "to_s": hole["to_ns"] / NS, "minutes": minutes}]
            span = " -> ".join(
                datetime.fromtimestamp(t / NS, timezone.utc).strftime("%m-%d %H:%M:%SZ")
                for t in (hole["from_ns"], hole["to_ns"]))
            events.append(("open", f"TAPE HOLE {stream} {minutes:.0f}m ({span}); "
                                   "`deploy/tapecheck.py` has the cause"))
            state.setdefault("pending_incidents", []).append({
                "name": "tape_hole", "stream": stream, "from_ts": hole["from_ns"],
                "to_ts": hole["to_ns"], "minutes": minutes,
                "cause": "manifest gap over the pre-registered floor; see tapecheck"})
    return events


def ledger_incident(payload: dict) -> bool:
    """Append an incident through the CLI; False when the venv is unavailable (retried next tick)."""
    try:
        return subprocess.run([str(NAT2), "log", "add", "--kind", "incident", "--json", json.dumps(payload)],
                              capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def record_action(kind: str, message: str, now_s: float, path: Path = ACTIONS) -> None:
    """One L0 line per watchdog event; a copied idiom, not an import (stdlib-only rule)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps({"t_ingest": int(now_s * 1e9), "level": "L0", "kind": f"gapwatch:{kind}",
                                 "payload": {"message": message}}, separators=(",", ":")) + "\n")
    except OSError:
        pass   # the alert already went out; the log is a convenience, not the record


def unit_active(unit: str) -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", unit], check=False
    )
    return result.returncode == 0


def conditions(now_s: float) -> dict[str, tuple[bool, str]]:
    """{check_name: (bad, detail)} for every watched condition."""
    conds: dict[str, tuple[bool, str]] = {}
    ages = newest_ingest()
    try:
        last_poll = json.loads(EVLOG_STATE.read_text())["last_poll_ns"] / 1e9
    except (OSError, ValueError, KeyError):
        last_poll = 0.0
    age = now_s - last_poll
    conds["stream:events"] = (age > GAP_FACTOR * EVLOG_CADENCE_S, f"age {age / 60:.0f}m")
    for stream, cadence in CADENCE_S.items():
        age = now_s - ages.get(stream, 0.0)
        conds[f"stream:{stream}"] = (age > GAP_FACTOR * cadence, f"age {age / 60:.0f}m")
    age = now_s - tape_heartbeat_s(TAPE_DIR)
    conds["stream:hl.trades:heartbeat"] = (age > TAPE_SILENCE_S, f"open file silent {age / 60:.0f}m")
    for unit in UNITS:
        active = unit_active(unit)
        conds[f"unit:{unit}"] = (not active, "active" if active else "inactive")
    for unit in REPORT_UNITS:
        conds[f"report:{unit}"] = (False, "inactive" if not unit_active(unit) else "active")
    free_gb = shutil.disk_usage(ROOT).free / 1e9
    conds["disk"] = (free_gb < DISK_MIN_FREE_GB, f"{free_gb:.0f}GB free")
    obs = last_observation_s()
    obs_age = now_s - obs if obs else float("inf")
    conds["observations"] = (obs_age > OBS_SILENCE_S, f"age {obs_age / 60:.0f}m")
    return conds


def tick(state: dict, conds: dict[str, tuple[bool, str]], now_s: float) -> list[tuple[str, str]]:
    """Pure state transition: mutates state, returns [(event, message)] to send.

    Events fire on edges only. Gap-minutes accrue per open check into the
    ISO-week counter; the counter resets when the week rolls.
    """
    week = datetime.fromtimestamp(now_s, timezone.utc).strftime("%G-W%V")
    if state.get("week") != week:
        # `booked_holes` is cleared with the counter it guards: kept across the
        # roll it would suppress a hole that this week has not yet counted.
        state["week"], state["gap_minutes"], state["booked_holes"] = week, {}, {}
    events: list[tuple[str, str]] = []
    open_since: dict[str, float] = state.setdefault("open", {})
    # Accrue BEFORE processing edges, so the segment between the previous tick
    # and a recovery in this tick still lands in the weekly counter.
    last = state.get("last_tick") or now_s
    for name, since in open_since.items():
        add = (now_s - max(last, since)) / 60
        if add > 0:
            state.setdefault("gap_minutes", {})[name] = state["gap_minutes"].get(name, 0.0) + add
    for name, (bad, detail) in conds.items():
        if bad and name not in open_since:
            open_since[name] = now_s
            events.append(("open", f"{name} GAP OPEN ({detail})"))
        elif not bad and name in open_since:
            minutes = (now_s - open_since.pop(name)) / 60
            events.append(("recovery", f"{name} recovered after {minutes:.0f}m"))
    state["last_tick"] = now_s
    return events


def notify(message: str, priority: str = "default") -> bool:
    topic = os.environ.get(NTFY_ENV)
    if not topic:
        print(f"WARNING: {NTFY_ENV} unset; NOT sent: {message}", file=sys.stderr)
        return False
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode(),
        headers={"Title": "nat2 gapwatch", "Priority": priority},
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except OSError as exc:
        print(f"WARNING: ntfy send failed: {exc}", file=sys.stderr)
        return False


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_PATH)


def cmd_check() -> None:
    now_s = time.time()
    state = load_state()
    conds = conditions(now_s)
    state["units"] = {
        name.split(":", 1)[1]: detail
        for name, (_, detail) in conds.items()
        if name.startswith(("unit:", "report:"))
    }
    events = tick(state, {k: v for k, v in conds.items() if not k.startswith("report:")}, now_s)
    # After `tick`, because the week roll inside it clears both the counter and
    # the dedupe set; booking first would credit the new week and then have the
    # record of having done so wiped.
    events += book_holes(state, now_s)
    state["pending_incidents"] = [p for p in state.get("pending_incidents", []) if not ledger_incident(p)][-20:]
    for kind, message in events:
        record_action(kind, message, now_s)
    # A failed send (DNS blip, ntfy outage) must not lose the alert: queue it
    # in state and retry next tick. Bounded so a long offline stretch can't
    # grow the state file without limit.
    queue = [(k, m) for k, m in state.get("pending_alerts", [])] + events
    state["pending_alerts"] = [
        (kind, message)
        for kind, message in queue
        if not notify(message, priority="high" if kind == "open" else "default")
    ][-20:]
    save_state(state)
    open_names = sorted(state.get("open", {}))
    print(f"open={open_names or 'none'} events={len(events)} unsent={len(state['pending_alerts'])}")


def cmd_report() -> None:
    now_s = time.time()
    state = load_state()
    ages = newest_ingest()
    print(
        json.dumps(
            {
                "week": state.get("week"),
                "budget_min_per_stream": WEEK_BUDGET_MIN,
                "gap_minutes": state.get("gap_minutes", {}),
                "open": sorted(state.get("open", {})),
                "stream_age_s": {s: round(now_s - t) for s, t in ages.items()},
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    cmd_report() if (len(sys.argv) > 1 and sys.argv[1] == "report") else cmd_check()
