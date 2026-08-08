"""Finding the data home from anywhere.

Once `nat2` is on PATH it gets run from arbitrary directories. The failure that
matters is not a crash -- it is `nat2 wallets status` cheerfully reporting an
empty registry because it looked at `./data` in someone's home directory. An
empty answer indistinguishable from a real one is worse than an error.
"""

from __future__ import annotations

from pathlib import Path

from nat2.core.paths import home, resolved

PYPROJECT = '[project]\nname = "nat2"\nversion = "0.1.0"\n'
# Tests must not consult the developer's own ~/.config/nat2/home, or they pass
# or fail depending on whether this machine has nat2 installed.
NO_CONFIG = Path("/nonexistent/nat2/home")


def _project(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    return tmp_path


def test_env_override_wins(tmp_path):
    project = _project(tmp_path / "proj")
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert home(start=project, env={"NAT2_HOME": str(other)}) == other.resolve()
    assert resolved(start=project, env={"NAT2_HOME": str(other)})[1] == "NAT2_HOME"


def test_env_override_expands_a_tilde(tmp_path):
    result = home(start=tmp_path, env={"NAT2_HOME": "~/somewhere"})
    assert "~" not in str(result) and result.is_absolute()


def test_project_root_is_found_from_the_root_itself(tmp_path):
    project = _project(tmp_path)
    assert home(start=project, env={}) == project.resolve()
    assert resolved(start=project, env={})[1] == "project root"


def test_project_root_is_found_from_deep_inside(tmp_path):
    project = _project(tmp_path)
    deep = project / "src" / "nat2" / "features"
    deep.mkdir(parents=True)
    assert home(start=deep, env={}) == project.resolve()


def test_a_marker_file_identifies_a_deployment_without_source(tmp_path):
    # An installed deployment need not carry pyproject.toml.
    (tmp_path / ".nat2").touch()
    assert home(start=tmp_path, env={}) == tmp_path.resolve()


def test_a_bare_data_directory_is_not_a_marker(tmp_path):
    # An earlier version matched any data/ dir and promptly picked up a stray
    # /tmp/data left by an unrelated command. A marker that can be created by
    # accident is not a marker.
    (tmp_path / "data").mkdir()
    assert resolved(start=tmp_path, env={}, config=NO_CONFIG)[1].startswith(
        "current directory"
    )


def test_a_foreign_pyproject_is_not_our_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "something-else"\n')
    nested = tmp_path / "sub"
    nested.mkdir()
    # No data/ and not our pyproject, so this is the fallback, and it says so.
    assert resolved(start=nested, env={}, config=NO_CONFIG)[1].startswith("current directory")


def test_fallback_is_reported_rather_than_silently_assumed(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    path, how = resolved(start=empty, env={}, config=NO_CONFIG)
    assert path == empty.resolve()
    assert "no project found" in how


def test_an_unreadable_pyproject_does_not_crash_the_search(tmp_path):
    from nat2.core import paths

    (tmp_path / "pyproject.toml").write_text(PYPROJECT)
    original = paths.Path.read_text

    def boom(self, *args, **kwargs):
        if self.name == "pyproject.toml":
            raise OSError("permission denied")
        return original(self, *args, **kwargs)

    paths.Path.read_text = boom
    try:
        # Falls through rather than propagating; a project further up may
        # still match, so only the crash-freedom is asserted here.
        assert isinstance(resolved(start=tmp_path, env={}, config=NO_CONFIG)[1], str)
    finally:
        paths.Path.read_text = original


def test_home_is_always_absolute(tmp_path):
    assert home(start=tmp_path, env={}).is_absolute()


def test_installed_default_is_used_when_no_project_is_found(tmp_path):
    # The reason `nat2` works from anywhere: install.sh records where the store
    # lives, so an arbitrary directory does not become a new empty one.
    store = tmp_path / "store"
    store.mkdir()
    config = tmp_path / "config"
    config.write_text(f"{store}\n")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    path, how = resolved(start=elsewhere, env={}, config=config)
    assert path == store.resolve()
    assert how == "installed default"


def test_a_checkout_beats_the_installed_default(tmp_path):
    # A second clone must not quietly write into the first one's store.
    store = tmp_path / "store"
    store.mkdir()
    config = tmp_path / "config"
    config.write_text(str(store))
    checkout = _project(tmp_path / "other-clone")

    assert home(start=checkout, env={}, config=config) == checkout.resolve()


def test_env_beats_everything(tmp_path):
    store = tmp_path / "store"
    store.mkdir()
    config = tmp_path / "config"
    config.write_text(str(store))
    forced = tmp_path / "forced"
    forced.mkdir()
    checkout = _project(tmp_path / "clone")

    assert home(start=checkout, env={"NAT2_HOME": str(forced)}, config=config) == forced.resolve()


def test_a_stale_recorded_home_is_ignored(tmp_path):
    # The recorded path was deleted or moved; fall through rather than pointing
    # every command at a directory that is not there.
    config = tmp_path / "config"
    config.write_text(str(tmp_path / "deleted"))
    here = tmp_path / "here"
    here.mkdir()
    assert resolved(start=here, env={}, config=config)[1].startswith("current directory")


def test_a_missing_or_empty_config_is_not_an_error(tmp_path):
    from nat2.core.paths import recorded_home

    assert recorded_home(tmp_path / "nope") is None
    empty = tmp_path / "empty"
    empty.write_text("  \n")
    assert recorded_home(empty) is None
