"""Render and install systemd --user units for nat2 capture and cycle.

Pure render function (unit-testable, no host writes) + an ``install`` entry
point that writes the units to ``~/.config/systemd/user/`` and enables them.
Modeled on nat's ``scripts/ops/systemd_units.py``; the cron+tmux supervision
pattern is what let capture die silently on 2026-08-13, so these processes run
under systemd only.

All paths are absolute: the units survive the repo checkout moving branches,
and the ExecStart interpreter is the project venv's console script by absolute
path (a relative or deleted-venv path is the failure mode that killed nat's
gap-alert cron line).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NAT2_BIN = ROOT / ".venv" / "bin" / "nat2"

CAPTURE_UNIT = "nat2-capture.service"
CYCLE_UNIT = "nat2-cycle.service"


def render_units(root: Path = ROOT, nat2_bin: Path = NAT2_BIN) -> dict[str, str]:
    """Return {unit_filename: file_text} for the capture and cycle daemons."""
    capture = f"""\
[Unit]
Description=nat2 WORM capture (Hyperliquid, all perps)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={root}
ExecStart={nat2_bin} capture hl --all
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""

    # The cycle persists last-run times in SQLite (io/cycle.py), so a restart
    # never re-triggers the expensive registry sweep early.
    cycle = f"""\
[Unit]
Description=nat2 snapshot/observe cycle (gate-map evidence accrual)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={root}
ExecStart={nat2_bin} cycle --snapshot-every 6h --scan-every 1h
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
"""
    return {CAPTURE_UNIT: capture, CYCLE_UNIT: cycle}


def install() -> None:
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    if not NAT2_BIN.exists():
        sys.exit(f"refusing to install: {NAT2_BIN} does not exist")
    for name, text in render_units().items():
        (unit_dir / name).write_text(text)
        print(f"wrote {unit_dir / name}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", CAPTURE_UNIT, CYCLE_UNIT], check=True
    )
    subprocess.run(["loginctl", "enable-linger"], check=False)
    print("units enabled and started; linger requested")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "render":
        for name, text in render_units().items():
            print(f"# --- {name}\n{text}")
    else:
        install()
