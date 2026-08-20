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
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "data" / "raw" / "_manifest.jsonl"
LEDGER = ROOT / "data" / "ledger.jsonl"
STATE_PATH = ROOT / "data" / "ops" / "gapwatch_state.json"

# Cadences measured from manifest history 2026-08-07..20 (median inter-entry
# interval): ws streams rotate hourly (3600 s median, n=84 for trades and
# assetctxs; l2book rotates hourly too but has n=1 closed-file history — it was
# absent from the captured stream set 08-08..08-13); liqmap entries land at
# least once per 1 h scan pass. A gap opens at age > GAP_FACTOR x cadence.
CADENCE_S = {
    "hl.trades": 3600.0,
    "hl.l2book": 3600.0,
    "hl.assetctxs": 3600.0,
    "nat2.liqmap": 3600.0,
}
GAP_FACTOR = 2.0
UNITS = ("nat2-capture.service", "nat2-cycle.service")
DISK_MIN_FREE_GB = 20.0
OBS_SILENCE_S = 7200.0  # 2x the 1 h liquidation-scan cadence
WEEK_BUDGET_MIN = 60.0  # REDUCED_SPECS §7.2: gap-minutes/week per stream
NTFY_ENV = "NAT2_NTFY_TOPIC"


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


def last_observation_s(ledger: Path = LEDGER) -> float | None:
    for line in reversed(tail_lines(ledger)):
        try:
            entry = json.loads(line)
            if entry.get("kind") == "observation":
                return entry["ts"] / 1e9
        except (json.JSONDecodeError, KeyError):
            continue
    return None


def unit_active(unit: str) -> bool:
    result = subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", unit], check=False
    )
    return result.returncode == 0


def conditions(now_s: float) -> dict[str, tuple[bool, str]]:
    """{check_name: (bad, detail)} for every watched condition."""
    conds: dict[str, tuple[bool, str]] = {}
    ages = newest_ingest()
    for stream, cadence in CADENCE_S.items():
        age = now_s - ages.get(stream, 0.0)
        conds[f"stream:{stream}"] = (age > GAP_FACTOR * cadence, f"age {age / 60:.0f}m")
    for unit in UNITS:
        conds[f"unit:{unit}"] = (not unit_active(unit), "inactive")
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
        state["week"], state["gap_minutes"] = week, {}
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
    events = tick(state, conditions(now_s), now_s)
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
