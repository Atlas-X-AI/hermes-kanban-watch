import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import PLUGIN
from helpers import fake_hermes


@pytest.fixture
def setup_doctor(tmp_path, isolated_home, monkeypatch, routes):
    home, hermes = isolated_home
    bindir = tmp_path / "bin"
    fake_hermes(bindir)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CALLS", str(log))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.defpath)
    for profile in ("builder", "reviewer"):
        directory = hermes / "profiles" / profile
        directory.mkdir(parents=True)
        (directory / "config.yaml").write_text("unrelated: do-not-report\nmodel:\n  provider: openai\n  default: gpt-5\n  api_key: do-not-report\n")
        for filename in (".env", "auth.json"):
            (directory / filename).write_text("CANARY_MUST_NOT_BE_OPENED_123456789")
    (hermes / "kanban.db").write_bytes(b"fake board")
    routefile = tmp_path / "routes.yaml"
    routefile.write_text(json.dumps(routes))
    return home, hermes, bindir, log, routefile


def doctor(routefile, *args, audit=False):
    script = PLUGIN / "scripts/doctor.py"
    if audit:
        runner = '''import runpy, sys
from pathlib import Path
script = sys.argv.pop(1)
sys.path.insert(0, str(Path(script).parent))
def guard(event, args):
    if event == 'open' and isinstance(args[0], (str, bytes)):
        if Path(args[0]).name in ('.env', 'auth.json'):
            print('CREDENTIAL_OPEN_ATTEMPT', file=sys.stderr)
            raise RuntimeError('credential file opened')
sys.addaudithook(guard)
runpy.run_path(script, run_name='__main__')
'''
        command = [sys.executable, "-c", runner, str(script)]
    else:
        command = [sys.executable, str(script)]
    return subprocess.run([*command, "--routes", str(routefile), *args],
                          capture_output=True, text=True, timeout=20)


def tree_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_doctor_read_only_and_canaries_never_opened(setup_doctor):
    home, _, bindir, _, routefile = setup_doctor
    shim = bindir / "kanban-live"
    shim.write_text("#!/bin/sh\nexit 0\n")
    shim.chmod(0o755)
    before = tree_bytes(home)
    out = doctor(routefile, audit=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "PASS hermes" in out.stdout and "v0.21.2" in out.stdout
    assert "provider=openai model=gpt-5" in out.stdout
    assert "CANARY_MUST_NOT_BE_OPENED" not in out.stdout + out.stderr
    assert "CREDENTIAL_OPEN_ATTEMPT" not in out.stdout + out.stderr
    assert "do-not-report" not in out.stdout
    assert tree_bytes(home) == before
    for command in ("cards", "snapshot", "wait-start", "wait-any", "follow", "events", "active", "scout", "needs"):
        assert f"Bash(kanban-live {command}:*)" in out.stdout


@pytest.mark.parametrize("board", ["default", "new-board"])
def test_doctor_fix_and_no_overwrite(setup_doctor, board):
    home, hermes, _, log, routefile = setup_doctor
    db = hermes / "kanban.db" if board == "default" else hermes / "kanban/boards" / board / "kanban.db"
    if db.exists():
        db.unlink()
    command = (["kanban", "--board", "default", "init"] if board == "default" else
               ["kanban", "boards", "create", board])
    out = doctor(routefile, "--fix", "--board", board)
    assert out.returncode == 0, out.stdout + out.stderr
    shim = home / ".local/bin/kanban-live"
    config = home / ".config/hermes-kanban-watch/routes.yaml"
    assert shim.stat().st_mode & 0o111
    assert "kanban_live.py" in shim.read_text()
    assert json.loads(subprocess.run([str(shim), "cards", "--board", "absent"],
                                    capture_output=True, text=True, timeout=10).stdout)["read_status"] == "UNKNOWN"
    assert "classes" in config.read_text()
    assert db.read_bytes() == b"fake board"
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert command in calls
    shim.write_text("preserve-shim")
    config.write_text("preserve-routes")
    out = doctor(routefile, "--fix", "--board", board)
    assert "existing" in out.stdout.lower()
    assert shim.read_text() == "preserve-shim" and config.read_text() == "preserve-routes"
    assert [json.loads(line) for line in log.read_text().splitlines()].count(command) == 1


def test_fake_hermes_named_board_requires_boards_create(setup_doctor):
    _, hermes, bindir, _, _ = setup_doctor
    db = hermes / "kanban/boards/new-board/kanban.db"
    command = [str(bindir / "hermes"), "kanban", "--board", "new-board", "init"]
    out = subprocess.run(command, capture_output=True, text=True, timeout=10)
    assert out.returncode == 1 and "Board does not exist" in out.stderr
    assert not db.exists()
    out = subprocess.run([str(bindir / "hermes"), "kanban", "boards", "create", "new-board"],
                         capture_output=True, text=True, timeout=10)
    assert out.returncode == 0 and db.is_file()
    assert subprocess.run(command, capture_output=True, timeout=10).returncode == 0


def test_doctor_missing_board_live_and_profile(setup_doctor):
    _, _, _, _, routefile = setup_doctor
    routes = json.loads(routefile.read_text())
    routes["classes"]["app-code"]["profile"] = "missing"
    routefile.write_text(json.dumps(routes))
    out = doctor(routefile, "--board", "absent")
    assert out.returncode == 1
    assert "FAIL board" in out.stdout and "FAIL kanban-live" in out.stdout and "FAIL class app-code" in out.stdout


@pytest.mark.parametrize("response,passes", [("PONG", True), ("PONG extra", False), ("", False)])
def test_doctor_probe_exact_and_deduplicated(setup_doctor, monkeypatch, response, passes):
    _, _, _, log, routefile = setup_doctor
    routes = json.loads(routefile.read_text())
    routes["classes"]["duplicate"] = routes["classes"]["app-code"]
    routefile.write_text(json.dumps(routes))
    monkeypatch.setenv("FAKE_PROBE", response)
    out = doctor(routefile, "--probe")
    probes = [json.loads(line) for line in log.read_text().splitlines() if '"chat"' in line]
    assert len(probes) == 2
    assert ["-p", "builder", "chat", "-Q", "-q", "Reply with the single word PONG and stop.",
            "-m", "gpt-5", "--provider", "openai"] in probes
    assert ("PASS probe" in out.stdout) == passes
    assert ("FAIL probe" in out.stdout) != passes


def test_doctor_suppresses_key_like_values(setup_doctor):
    _, hermes, _, _, routefile = setup_doctor
    secret = "sk-THIS-IS-A-PLANTED-KEY-LIKE-VALUE-0123456789"
    (hermes / "profiles/builder/config.yaml").write_text(f"model:\n  provider: {secret}\n  default: {secret}\n")
    out = doctor(routefile)
    assert secret not in out.stdout + out.stderr
    assert "UNKNOWN profile builder" in out.stdout


def test_doctor_no_bytecode_writes_without_environment_opt_out(setup_doctor, tmp_path, monkeypatch):
    import shutil
    _, _, _, _, routefile = setup_doctor
    install = tmp_path / "install"
    scripts = install / "scripts"
    scripts.mkdir(parents=True)
    for name in ("doctor.py", "route.py"):
        shutil.copyfile(PLUGIN / "scripts" / name, scripts / name)
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    before = tree_bytes(install)
    out = subprocess.run([sys.executable, str(scripts / "doctor.py"), "--routes", str(routefile)],
                         capture_output=True, text=True, timeout=10)
    assert "PASS profile builder" in out.stdout
    assert tree_bytes(install) == before


def test_doctor_config_symlink_does_not_open_credential(setup_doctor):
    _, hermes, _, _, routefile = setup_doctor
    profile = hermes / "profiles/builder"
    # Replace only the disposable fixture's config file.
    (profile / "config.yaml").unlink()
    (profile / "config.yaml").symlink_to(profile / ".env")
    out = doctor(routefile, audit=True)
    assert "UNKNOWN profile builder" in out.stdout
    assert "CREDENTIAL_OPEN_ATTEMPT" not in out.stdout + out.stderr
    assert "CANARY_MUST_NOT_BE_OPENED" not in out.stdout + out.stderr
