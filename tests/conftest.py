"""No test may write into the real store.

The suite has been appending to the live record for a month. `record_action`
binds `path: Path = ACTIONS` at def time (deploy/gapwatch.py:147), so the three
`cmd_check()` calls in tests/test_gapwatch.py wrote a real `gapwatch:open` line
to data/actions.jsonl on every run -- and `record_action` swallows OSError, so
nothing ever reported it. 64 planted lines from 32 runs, all stamped
`t_ingest 1755000000000000000`, sit in the canonical action log today. A second,
quieter leak: the same test never patched TAPE_DIR, so `conditions()` globbed the
real 17k-file tape and its verdict depended on whether this laptop's capture
happened to be healthy.

That store is about to become the canonical history on the Hetzner box, so the
guarantee has to be structural rather than remembered. Two mechanisms, because
one is not enough:

*   `_no_repo_writes` redirects, per test, every module-level Path under
    `<repo>/data` that a deploy script exposes -- and rebinds any function whose
    signature captured one of those paths as a default, since patching the
    constant alone is inert for those.
*   `_no_test_record_reaches_the_store` is the backstop: it reads whatever was
    appended to the canonical files during the session and fails the run if any
    of it is a test's work, whoever wrote it. A future leak from a test nobody
    has written yet is caught by that, not by this docstring.

    It keys on the timestamp rather than on the file size, because on a capture
    box the live daemons append to the same files while the suite runs -- a size
    check would cry wolf every time nat2-cycle ticked. A test writes with a frozen
    clock (`NOW = 1_755_000_000`, a year in the past); a daemon writes with the
    real one. That difference is the signal.

Deliberately NOT patched: paths that point at code (deploy/statuspage.py's own
SRC, which tests/test_statuspage.py opens to count its lines) and ROOT itself,
which is only ever stat-ed for free disk. The rule is the data directory, not
the repo.
"""

from __future__ import annotations

import functools
import inspect
import json
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"

# Paths that escape the data directory but still reach it. gapwatch writes the
# ledger by shelling out to the CLI (gapwatch.py:138-144), so redirecting the
# store is not enough -- the binary itself has to become one that is not there,
# which is the case `ledger_incident` already handles by returning False.
ESCAPE_HATCHES = {"NAT2": "bin/nat2-not-installed-under-test"}

# Append-only and canonical: anything a test adds here is a forgery in the record.
# The ns timestamp lives under a different key in each.
WITNESSES = {"data/actions.jsonl": "t_ingest", "data/ledger.jsonl": "ts"}
# A daemon writing while the suite runs is fine; a record backdated further than
# this is a frozen test clock, not a late write.
BACKDATE_SLACK_NS = 24 * 3600 * 1_000_000_000


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _with_defaults(func, substitutions: dict[str, Path]):
    """`func` with some of its default arguments moved to a scratch directory.

    A plain `functools.partial(func, manifest=tmp)` would break
    `newest_ingest(_manifest(tmp_path))` -- the caller passes it positionally and
    gets "multiple values for argument". Binding the call first tells us whether
    the caller supplied the parameter at all, by either route.
    """
    signature = inspect.signature(func)

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        supplied = signature.bind_partial(*args, **kwargs).arguments
        for name, value in substitutions.items():
            if name not in supplied:
                kwargs[name] = value
        return func(*args, **kwargs)

    wrapper.__wrapped_defaults__ = substitutions   # for the guard test to assert on
    return wrapper


def _shield(module, scratch: Path, monkeypatch) -> dict[str, Path]:
    """Point every store path this module knows about at `scratch`. Returns them."""
    redirected: dict[str, Path] = {}
    by_old: dict[Path, Path] = {}

    for name, value in list(vars(module).items()):
        if not isinstance(value, Path):
            continue
        if name in ESCAPE_HATCHES:
            new = scratch / ESCAPE_HATCHES[name]
        elif _under(value, DATA):
            new = scratch / value.resolve().relative_to(REPO.resolve())
        else:
            continue                        # code paths stay real, on purpose
        new.parent.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(module, name, new, raising=False)
        redirected[name] = new
        by_old[value] = new

    # Constants captured as default arguments are read at def time, so the
    # setattr above does nothing for them; the function itself has to be replaced.
    for name, func in list(vars(module).items()):
        if not inspect.isfunction(func) or func.__module__ != module.__name__:
            continue
        substitutions = {
            parameter.name: by_old[parameter.default]
            for parameter in inspect.signature(func).parameters.values()
            if isinstance(parameter.default, Path) and parameter.default in by_old
        }
        if substitutions:
            monkeypatch.setattr(module, name, _with_defaults(func, substitutions))

    return redirected


@pytest.fixture(autouse=True)
def _no_repo_writes(request, monkeypatch, tmp_path):
    """Redirect the store for any deploy module this test module loaded.

    The deploy scripts are loaded by path rather than imported (the no-cross-imports
    rule), and `spec.loader.exec_module` does not register them in `sys.modules` --
    verified -- so they are reachable only as an attribute of the test module that
    loaded them. `request.module` is how a fixture gets there, and it means no test
    file needs editing to be protected.
    """
    monkeypatch.setenv("NAT2_HOME", str(tmp_path))
    # notify() reads the topic at call time; without this a test could page the
    # real phone, and the topic is public (deploy/systemd_units.py:43).
    monkeypatch.delenv("NAT2_NTFY_TOPIC", raising=False)

    scratch = tmp_path / "shielded"
    for value in vars(request.module).values():
        if inspect.ismodule(value) and getattr(value, "__file__", None) \
                and _under(Path(value.__file__), REPO / "deploy"):
            _shield(value, scratch, monkeypatch)


def appended_records(path: Path, offset: int, key: str) -> list[tuple[int, str]]:
    """(timestamp_ns, line) for every complete record written past `offset`."""
    out: list[tuple[int, str]] = []
    with path.open("rb") as fh:
        fh.seek(offset)
        for raw in fh.read().splitlines():
            line = raw.decode("utf-8", "replace")
            try:
                stamp = json.loads(line)[key]
            except (json.JSONDecodeError, KeyError, TypeError):
                continue                # a torn tail, not a forged record
            if isinstance(stamp, int):
                out.append((stamp, line))
    return out


@pytest.fixture(scope="session", autouse=True)
def _no_test_record_reaches_the_store():
    """The backstop: whatever a test writes into the canonical files, this finds it.

    Not a size check -- the live capture daemons append to these same files while
    the suite runs. What separates a test's line from a daemon's is the clock it
    was written with, so that is what this reads.
    """
    started_ns = time.time_ns()
    before = {name: (REPO / name).stat().st_size
              for name in WITNESSES if (REPO / name).exists()}
    yield

    forged: list[str] = []
    for name, offset in before.items():
        path, floor = REPO / name, started_ns - BACKDATE_SLACK_NS
        for stamp, line in appended_records(path, offset, WITNESSES[name]):
            if stamp < floor:
                forged.append(f"{name}: {line[:160]}")

    assert not forged, (
        f"{len(forged)} record(s) written with a test clock reached the canonical store:\n"
        + "\n".join(forged[:5])
        + "\n-- the blast shield in tests/conftest.py did not cover this path"
    )
