"""Install systemd --user units for nat2: capture + cycle (static files in
``packaging/systemd/``) and the gapwatch oneshot + timer (rendered here).

Pure render function (unit-testable, no host writes) + an ``install`` entry
point that writes the units to ``~/.config/systemd/user/`` and enables them.
Modeled on nat's ``scripts/ops/systemd_units.py``; the cron+tmux supervision
pattern is what let capture die silently on 2026-08-13, so these processes run
under systemd only.

All paths are absolute (a relative or deleted-venv path is the failure mode
that killed nat's gap-alert cron line); gapwatch runs on the system python3.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC_UNITS = ROOT / "packaging" / "systemd"

CAPTURE_UNIT = "nat2-capture.service"
CYCLE_UNIT = "nat2-cycle.service"
GAPWATCH_UNIT = "nat2-gapwatch.service"
GAPWATCH_TIMER = "nat2-gapwatch.timer"
# Non-guessable alert channel (TASK_2/TASKS/01); subscribe to it in the ntfy app.
NTFY_TOPIC = "nat2-ops-0db264232a4c36cd56e4"


def render_units(root: Path = ROOT) -> dict[str, str]:
    """Return {unit_filename: file_text} for the gapwatch oneshot + timer.

    Capture and cycle are NOT rendered here: their units are the static files in
    ``packaging/systemd/`` (origin ``9b9a2f0`` + ``017102b``), which carry the
    load-bearing ``RuntimeMaxSec=5h`` recycle and the ``--min-volume`` filter.
    ``install()`` copies them verbatim so there is exactly one source of truth.
    """
    # Watchdog: timer-driven oneshot, stdlib-only script run with the system
    # python3 so it cannot die with a deleted venv (nat's gap-alert failure mode).
    gapwatch = f"""\
[Unit]
Description=nat2 gapwatch (manifest-gap + ops watchdog, ntfy alerts)

[Service]
Type=oneshot
WorkingDirectory={root}
Environment="NAT2_NTFY_TOPIC={NTFY_TOPIC}"
ExecStart=/usr/bin/python3 {root / "deploy" / "gapwatch.py"} check
"""

    gapwatch_timer = f"""\
[Unit]
Description=nat2 gapwatch timer (5 min)

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
"""
    return {GAPWATCH_UNIT: gapwatch, GAPWATCH_TIMER: gapwatch_timer}


def install() -> None:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name in (CAPTURE_UNIT, CYCLE_UNIT):
        src = STATIC_UNITS / name
        if not src.exists():
            sys.exit(f"refusing to install: {src} does not exist")
        (unit_dir / name).write_text(src.read_text())
        print(f"copied {src} -> {unit_dir / name}")
    for name, text in render_units().items():
        (unit_dir / name).write_text(text)
        print(f"wrote {unit_dir / name}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", CAPTURE_UNIT, CYCLE_UNIT, GAPWATCH_TIMER],
        check=True,
    )
    subprocess.run(["loginctl", "enable-linger"], check=False)
    print("units enabled and started; linger requested")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "render":
        for name, text in render_units().items():
            print(f"# --- {name}\n{text}")
    else:
        install()
