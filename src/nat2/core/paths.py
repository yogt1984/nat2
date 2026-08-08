"""Where nat2 keeps its data.

Once `nat2` is on PATH it gets run from anywhere, and relative paths turn that
into a silent hazard: `nat2 wallets status` from another directory used to
report an empty registry rather than an error, because it had helpfully looked
at `./data` and found nothing. An empty answer that looks like a real one is
the worst failure mode this system can have.

Resolution order:

    $NAT2_HOME                  explicit wins
    nearest ancestor with a .nat2 marker or a nat2 pyproject.toml
    ~/.config/nat2/home         recorded by install.sh
    the current directory       last resort, and reported as such

Walking up comes before the recorded default on purpose: working inside a
checkout should use that checkout, so a second clone does not quietly write
into the first one's store.

Every command prints the resolved home when it matters, so "which store am I
looking at" is never a guess.
"""

from __future__ import annotations

import os
from pathlib import Path

# Deliberately narrow. An earlier version treated any `data/` directory as a
# nat2 home, which promptly matched a stray `/tmp/data` left by an unrelated
# command. A marker that can be created by accident is not a marker.
MARKER_FILE = ".nat2"
CONFIG_FILE = Path.home() / ".config" / "nat2" / "home"


def _is_project(candidate: Path) -> bool:
    if (candidate / MARKER_FILE).is_file():
        return True
    pyproject = candidate / "pyproject.toml"
    if pyproject.is_file():
        try:
            return 'name = "nat2"' in pyproject.read_text()
        except OSError:
            return False
    return False


def recorded_home(config: Path | None = None) -> Path | None:
    """The install location, written by install.sh so `nat2` works anywhere."""
    config = CONFIG_FILE if config is None else config
    try:
        value = config.read_text().strip()
    except OSError:
        return None
    if not value:
        return None
    path = Path(value).expanduser()
    return path.resolve() if path.is_dir() else None


def home(start: Path | None = None, env: dict | None = None,
         config: Path | None = None) -> Path:
    return resolved(start, env, config)[0]


def resolved(start: Path | None = None, env: dict | None = None,
             config: Path | None = None) -> tuple[Path, str]:
    """The home directory and how it was chosen, for reporting."""
    env = os.environ if env is None else env
    override = env.get("NAT2_HOME")
    if override:
        return Path(override).expanduser().resolve(), "NAT2_HOME"
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if _is_project(candidate):
            return candidate, "project root"
    installed = recorded_home(config)
    if installed is not None:
        return installed, "installed default"
    return here, "current directory (no project found)"
