"""Opt-in compatibility check using the installed Hermes CLI, never live user data."""
import json
import os
import shlex
import shutil
import subprocess
import sys

import pytest

from conftest import PLUGIN, ROOT

# Capture before isolated_home removes inherited HERMES_KANBAN_* routing variables.
REAL_HERMES = shutil.which("hermes")
pytestmark = pytest.mark.skipif(
    os.environ.get("HERMES_KANBAN_WATCH_REAL") != "1" or REAL_HERMES is None,
    reason="requires hermes on PATH and HERMES_KANBAN_WATCH_REAL=1",
)


def test_real_hermes_documented_commands_reader_and_doctor(isolated_home, tmp_path, routes):
    home, hermes = isolated_home
    # Do not inherit real profile selection, board routing, XDG paths or credentials.
    env = {
        "PATH": os.environ["PATH"], "HOME": str(home), "HERMES_HOME": str(hermes),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local/share"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }

    def run(*command):
        result = subprocess.run(command, env=env, cwd=tmp_path, capture_output=True,
                                text=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        return result.stdout

    version = run(REAL_HERMES, "--version")
    assert "Hermes Agent" in version
    print(version.splitlines()[0])
    commands = [
        "hermes kanban boards create demo",
        'hermes kanban --board demo create "Build the feature" --assignee builder',
    ]
    for document in (ROOT / "README.md", PLUGIN / "README.md"):
        text = document.read_text()
        assert "\n".join(commands) in text
    for command in commands:
        run(REAL_HERMES, *shlex.split(command)[1:])
    db = hermes / "kanban/boards/demo/kanban.db"
    assert db.is_file()
    before = db.read_bytes()
    reader = str(PLUGIN / "scripts/kanban_live.py")
    cards = json.loads(run(sys.executable, reader, "cards", "--board", "demo"))
    assert cards["read_status"] == "OK" and cards["now"] > 0 and cards["count"] == 1
    card = cards["cards"][0]
    assert card["title"] == "Build the feature" and card["assignee"] == "builder"
    snapshot = json.loads(run(sys.executable, reader, "snapshot", "--board", "demo",
                              "--task", card["id"]))
    assert snapshot["read_status"] == "OK" and snapshot["id"] == card["id"]
    assert snapshot["status"] == card["status"]
    assert db.read_bytes() == before

    # Synthetic configuration only: no profile CLI, credentials or model calls.
    for name in ("builder", "reviewer"):
        profile = hermes / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("model:\n  provider: openai\n  default: gpt-5\n")
    routefile = tmp_path / "routes.json"
    routefile.write_text(json.dumps(routes))
    fixed_db = hermes / "kanban/boards/doctor-check/kanban.db"
    assert not fixed_db.exists()
    doctor = str(PLUGIN / "scripts/doctor.py")
    fixed = run(sys.executable, doctor, "--routes", str(routefile), "--board", "doctor-check", "--fix")
    assert "PASS fix board:" in fixed and "PASS board:" in fixed
    assert "0 FAIL" in fixed and fixed_db.is_file()
    shim = home / ".local/bin/kanban-live"
    assert shim.is_file() and os.access(shim, os.X_OK)
    assert (home / ".config/hermes-kanban-watch/routes.yaml").is_file()
    empty = json.loads(run(sys.executable, reader, "cards", "--board", "doctor-check"))
    assert empty["read_status"] == "OK" and empty["count"] == 0
