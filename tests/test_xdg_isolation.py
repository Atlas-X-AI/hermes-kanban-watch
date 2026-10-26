"""Inherited XDG roots must never escape the disposable test home."""
import os
from pathlib import Path

from conftest import isolated_home


XDG_ROOTS = ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")


def test_preset_xdg_roots_are_replaced(tmp_path, monkeypatch):
    inherited = tmp_path / "inherited"
    for name in XDG_ROOTS:
        monkeypatch.setenv(name, str(inherited / name))
    nested = tmp_path / "next-test"
    nested.mkdir()
    home, hermes = isolated_home.__wrapped__(nested, monkeypatch)
    assert Path(os.environ["HOME"]) == home
    assert Path(os.environ["HERMES_HOME"]) == hermes
    for name in XDG_ROOTS:
        root = Path(os.environ[name])
        assert root.is_relative_to(nested)
        assert not root.is_relative_to(inherited)
    assert len({os.environ[name] for name in XDG_ROOTS}) == 4
    assert not inherited.exists()


def test_every_test_has_private_xdg_roots(tmp_path):
    for name in XDG_ROOTS:
        assert Path(os.environ[name]).is_relative_to(tmp_path)
