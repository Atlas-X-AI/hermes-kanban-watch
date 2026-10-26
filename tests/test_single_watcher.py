"""Launch and wake proofs with fake processes and disposable SQL, never a live watcher."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from conftest import PLUGIN


@pytest.fixture
def launch_env(board, isolated_home, tmp_path, monkeypatch):
    home, _ = isolated_home
    inbox = home / ".claude/state/messages.db"
    inbox.parent.mkdir(parents=True)
    with sqlite3.connect(inbox) as conn:
        conn.executescript((PLUGIN.parent / "tests/fixtures/inbox_schema.sql").read_text())
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, content in {
        "tmux": "import sys\nassert sys.argv[1:] == ['list-panes','-t','=desk:','-F','#{pane_id}']\nprint('%42')\n",
        "hermes": "import sys\nraise SystemExit('fake hermes must not be run by launcher tests')\n",
        "systemd-run": '''import json, os, sys
from pathlib import Path
log = Path(os.environ['UNIT_CALLS'])
with log.open('a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
unit = sys.argv[sys.argv.index('--unit') + 1]
marker = log.parent / unit
try:
    marker.open('x').close()
except FileExistsError:
    print('unit already exists', file=sys.stderr)
    sys.exit(1)
''',
    }.items():
        path = bindir / name
        path.write_text(f"#!{sys.executable}\n" + content)
        path.chmod(0o755)
    log = tmp_path / "unit-calls"
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("UNIT_CALLS", str(log))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_parent")
    return log, inbox


def launch(*extra):
    return subprocess.run(["bash", str(PLUGIN / "scripts/start_watcher.sh"),
                           "--board", "demo", "--session", "desk", *extra],
                          capture_output=True, text=True, timeout=5)


def test_single_watcher_unit_and_budget(launch_env):
    log, _ = launch_env
    out = launch("--cards", "t_run", "t_wait", "--every", "900")
    assert out.returncode == 0, out.stderr
    receipt = json.loads(out.stdout)
    args = json.loads(log.read_text().splitlines()[0])
    assert args[:4] == ["--user", "--unit", "kanban-watcher-demo", "--collect"]
    assert "RuntimeMaxSec=43290" in args
    assert args[args.index('--profile') + 1] == "kanban-watcher"
    assert args[args.index('--max-turns') + 1] == "1600"
    assert args[args.index('-t') + 1] == "terminal"
    assert "HERMES_KANBAN_TASK" not in " ".join(args)
    brief = Path(args[args.index('--query-file') + 1]).read_text()
    assert "watcher_tick.py" in brief and "Never" in brief
    assert "Workflow(" not in brief
    config = json.loads(Path(receipt["config"]).read_text())
    assert config["cards"] == ["t_run", "t_wait"] and config["every"] == 900
    assert config["board_home"] == os.environ["HERMES_HOME"]
    assert receipt["unit"] == "kanban-watcher-demo"
    assert launch().returncode != 0  # The shared board lease refuses a duplicate before systemd.


@pytest.mark.parametrize("extra", [["--board", "../bad"], ["--every", "0"],
    ["--max-hours", "0"], ["--max-hours", "nan"], ["--cards", "t_absent"]])
def test_bad_launch_never_starts(launch_env, extra):
    log, _ = launch_env
    assert launch(*extra).returncode != 0
    assert not log.exists()


def test_tick_problem_and_final_exit_without_extra_delivery(launch_env, tmp_path):
    _, inbox = launch_env
    out = launch("--cards", "t_wait", "--every", "0.02")
    assert out.returncode == 0, out.stderr
    config = Path(json.loads(out.stdout)["config"])
    command = [sys.executable, str(PLUGIN / "scripts/watcher_tick.py"), "--config", str(config)]
    first = subprocess.run(command, capture_output=True, text=True, timeout=5)
    assert first.returncode == 0 and "CONTINUE summary" in first.stdout
    db = Path(os.environ["HERMES_HOME"]) / "kanban/boards/demo/kanban.db"
    with sqlite3.connect(db) as conn:
        conn.execute("update tasks set status='blocked' where id='t_wait'")
    problem = subprocess.run(command, capture_output=True, text=True, timeout=5)
    assert problem.returncode == 0 and "CONTINUE problem" in problem.stdout
    with sqlite3.connect(db) as conn:
        conn.execute("update tasks set status='done' where id='t_wait'")
    before = db.read_bytes()
    final = subprocess.run(command, capture_output=True, text=True, timeout=5)
    assert final.returncode == 0 and "STOP final" in final.stdout
    again = subprocess.run(command, capture_output=True, text=True, timeout=5)
    assert again.returncode == 0 and "STOP" in again.stdout
    with sqlite3.connect(inbox) as conn:
        rows = conn.execute("select payload from messages").fetchall()
    assert len(rows) == 3 and "FINAL" in json.loads(rows[-1][0])["text"]
    assert db.read_bytes() == before


def test_tick_max_hours_is_not_completion(launch_env):
    _, inbox = launch_env
    out = launch("--cards", "t_run")
    config = Path(json.loads(out.stdout)["config"])
    data = json.loads(config.read_text())
    data["until"] = 1
    config.write_text(json.dumps(data))
    tick = subprocess.run([sys.executable, str(PLUGIN / "scripts/watcher_tick.py"), "--config", str(config)],
                          capture_output=True, text=True, timeout=5)
    assert tick.returncode == 0 and "STOP max_hours" in tick.stdout
    with sqlite3.connect(inbox) as conn:
        text = json.loads(conn.execute("select payload from messages").fetchone()[0])["text"]
    assert "max_hours" in text and "FINAL" not in text


def test_tick_read_failure_sends_unknown_and_stops(launch_env):
    _, inbox = launch_env
    out = launch("--cards", "t_run")
    config = json.loads(out.stdout)["config"]
    db = Path(os.environ["HERMES_HOME"]) / "kanban/boards/demo/kanban.db"
    db.write_bytes(b"broken")
    tick = subprocess.run([sys.executable, str(PLUGIN / "scripts/watcher_tick.py"), "--config", config],
                          capture_output=True, text=True, timeout=5)
    assert tick.returncode != 0 and "STOP UNKNOWN" in tick.stdout
    with sqlite3.connect(inbox) as conn:
        assert "UNKNOWN" in json.loads(conn.execute("select payload from messages").fetchone()[0])["text"]


def test_native_prepare_uses_same_reader_without_inbox(launch_env):
    log, inbox = launch_env
    out = launch("--prepare-only", "--delivery", "native", "--cards", "t_wait", "--every", "0.02")
    assert out.returncode == 0, out.stderr
    receipt = json.loads(out.stdout)
    assert receipt["native_parent_delivery"] == "UNKNOWN" and not log.exists()
    tick = subprocess.run([sys.executable, str(PLUGIN / "scripts/watcher_tick.py"),
                           "--config", receipt["config"]], capture_output=True, text=True, timeout=5)
    assert tick.returncode == 0 and "running 0" in tick.stdout and "CONTINUE summary" in tick.stdout
    with sqlite3.connect(inbox) as conn:
        assert conn.execute("select count(*) from messages").fetchone()[0] == 0
    assert launch().returncode != 0  # Native and bridge launches share a board lease.


def test_delivery_failure_keeps_pending_for_same_config_retry(launch_env, tmp_path):
    _, inbox = launch_env
    receipt = json.loads(launch("--cards", "t_done").stdout)
    tmux = tmp_path / "bin/tmux"
    good = tmux.read_text()
    tmux.write_text(f"#!{sys.executable}\nraise SystemExit(1)\n")
    command = [sys.executable, str(PLUGIN / "scripts/watcher_tick.py"), "--config", receipt["config"]]
    failure = subprocess.run(command, capture_output=True, text=True, timeout=5)
    assert failure.returncode != 0 and "delivery UNKNOWN" in failure.stderr
    config = json.loads(Path(receipt["config"]).read_text())
    assert json.loads(Path(config["relay_state"]).read_text())["pending"]["reason"] == "final"
    with sqlite3.connect(inbox) as conn:
        assert conn.execute("select count(*) from messages").fetchone()[0] == 0
    tmux.write_text(good)
    success = subprocess.run(command, capture_output=True, text=True, timeout=5)
    assert success.returncode == 0 and "STOP final" in success.stdout
    assert "FINAL: Kanban" in success.stdout
    with sqlite3.connect(inbox) as conn:
        assert conn.execute("select count(*) from messages").fetchone()[0] == 1


def test_claude_brief_uses_background_completion_not_foreground_timeout(launch_env):
    receipt = json.loads(launch("--prepare-only").stdout)
    brief = Path(receipt["brief"]).read_text()
    claude = brief.split("Claude Code:", 1)[1].split("Hermes:", 1)[0]
    assert "run_in_background: true" in claude
    assert "task-completion notification" in claude
    assert "last lines" in claude
    assert "Never poll" in claude and "sleep" in claude
    assert "960" not in claude and "foreground" not in claude
    hermes = brief.split("Hermes:", 1)[1]
    assert "foreground" in hermes and "960" in hermes
    config = json.loads(Path(receipt["config"]).read_text())
    assert config["every"] == 900


def test_superseded_lease_never_delivers(launch_env):
    _, inbox = launch_env
    receipt = json.loads(launch("--cards", "t_done").stdout)
    config = json.loads(Path(receipt["config"]).read_text())
    Path(config["lease"]).write_text(json.dumps({"launch_id": "another", "until": config["until"]}))
    out = subprocess.run([sys.executable, str(PLUGIN / "scripts/watcher_tick.py"),
                          "--config", receipt["config"]], capture_output=True, text=True, timeout=5)
    assert out.returncode != 0 and "superseded" in out.stderr
    with sqlite3.connect(inbox) as conn:
        assert conn.execute("select count(*) from messages").fetchone()[0] == 0
