"""Real inbox SQL, fake tmux plus one owned-server contract test, no live delivery."""
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys

import pytest

from conftest import PLUGIN, ROOT


@pytest.fixture
def inbox(isolated_home, tmp_path, monkeypatch):
    home, _ = isolated_home
    db = home / ".claude/state/messages.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.executescript((ROOT / "tests/fixtures/inbox_schema.sql").read_text())
    bindir = tmp_path / "bin"
    bindir.mkdir()
    tmux = bindir / "tmux"
    tmux.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
with open(os.environ['TMUX_CALLS'], 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:] != ['list-panes', '-s', '-t', '=desk:', '-F', '#{pane_id} #{pane_pid}']:
    sys.exit(1)
print(os.environ.get('TMUX_PANES', '%42 4200'))
''')
    tmux.chmod(0o755)
    from pane_processes import install_ps
    install_ps(bindir)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("TMUX_CALLS", str(tmp_path / "tmux-calls"))
    text = tmp_path / "text.txt"
    text.write_text("Kanban demo: done 1 / running 2\nrough time left UNKNOWN\n")
    return db, text


def notify(text, session="desk", *extra):
    return subprocess.run([sys.executable, str(PLUGIN / "scripts/notify_session.py"),
                           "--session", session, "--text-file", str(text), *extra],
                          capture_output=True, text=True, timeout=5)


def test_exact_row_shape_and_readback(inbox):
    db, text = inbox
    result = notify(text)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("select * from messages").fetchall()
    assert len(rows) == 1 and receipt["id"] == rows[0]["id"] and receipt["notified"] == 0
    row = rows[0]
    assert row["target"] == "desk" and row["notify_target"] == "=desk:.%42"
    assert row["sender"] == "kanban-watcher" and row["message_type"] == "project_channel_message"
    assert json.loads(row["payload"]) == {
        "text": text.read_text(), "source": "kanban-watch",
        "project_channel": {"type": "project_channel_message", "render": text.read_text()}}
    assert row["dedupe_key"].startswith("kanban-watch:")


@pytest.mark.parametrize("session", ["des", "missing", "desk;bad", "desk\n", ""])
def test_missing_or_invalid_session_never_writes(inbox, session):
    db, text = inbox
    before = db.read_bytes()
    out = notify(text, session)
    assert out.returncode != 0
    assert db.read_bytes() == before


@pytest.mark.parametrize("panes", ["", "%42 4200\n%43 4300", "not-a-pane"])
def test_ambiguous_or_invalid_pane_refused(inbox, monkeypatch, panes):
    db, text = inbox
    monkeypatch.setenv("TMUX_PANES", panes)
    before = db.read_bytes()
    assert notify(text).returncode != 0
    assert db.read_bytes() == before


def test_explicit_pane_in_shared_session(inbox, monkeypatch):
    db, text = inbox
    monkeypatch.setenv("TMUX_PANES", "%43 4300\n%42 4200")
    result = notify(text, "desk", "--pane", "%42", "--pane-pid", "4200")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["notify_target"] == "=desk:.%42"
    with sqlite3.connect(db) as conn:
        assert conn.execute("select notify_target from messages").fetchall() == [("=desk:.%42",)]


@pytest.mark.parametrize("extra", [
    ("--pane", "%999"), ("--pane", ""), ("--pane", "%42\n"),
    ("--pane", "%42", "--pane-pid", "9999"),
    ("--pane", "%42", "--pane-pid", "0"), ("--pane-pid", "4200"),
])
def test_invalid_explicit_identity_does_not_open_inbox(inbox, monkeypatch, extra):
    db, text = inbox
    monkeypatch.setenv("TMUX_PANES", "%42 4200\n%43 4300")
    before = db.read_bytes()
    result = notify(text, "desk", *extra)
    assert result.returncode != 0
    assert db.read_bytes() == before


def test_hold_state_is_read_back_not_assumed(inbox):
    db, text = inbox
    with sqlite3.connect(db) as conn:
        conn.execute("insert into molle_input_holds values('desk')")
    out = notify(text)
    assert out.returncode == 0
    assert json.loads(out.stdout)["notified"] == 2


def test_missing_database_not_created(inbox):
    db, text = inbox
    db.unlink()
    assert notify(text).returncode != 0
    assert not db.exists()


def test_empty_text_does_not_queue(inbox):
    db, text = inbox
    before = db.read_bytes()
    text.write_text(" \n")
    assert notify(text).returncode != 0
    assert db.read_bytes() == before


def test_dedupe_replays_do_not_insert_twice(inbox):
    db, text = inbox
    first = notify(text, "desk", "--key", "kanban-watch:test:1")
    second = notify(text, "desk", "--key", "kanban-watch:test:1")
    assert first.returncode == second.returncode == 0
    assert json.loads(first.stdout)["id"] == json.loads(second.stdout)["id"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("select count(*) from messages").fetchone()[0] == 1


@pytest.mark.parametrize("processes", [
    "4200 1 S Mon Nov 2 08:00:00 2026 zsh\n4201 4200 Z Mon Nov 2 08:01:00 2026 claude",
    "4200 1 S Mon Nov 2 08:00:00 2026 zsh\n4201 4200 X Mon Nov 2 08:01:00 2026 claude",
    "4200 1 S Mon Nov 2 08:00:00 2026 zsh\n4201 4200 S Mon Nov 2 08:01:00 2026 node",
    "4201 4202 S Mon Nov 2 08:01:00 2026 claude\n4202 4201 S Mon Nov 2 08:01:00 2026 zsh",
    "unreadable process output",
])
def test_dead_unknown_or_unreadable_agents_never_open_inbox(inbox, monkeypatch, processes):
    db, text = inbox
    monkeypatch.setenv("PANE_PROCESSES", processes)
    before = db.read_bytes()
    assert notify(text).returncode != 0
    assert db.read_bytes() == before


def test_agent_ancestry_can_include_intermediate_shells(inbox, monkeypatch):
    _, text = inbox
    monkeypatch.setenv("PANE_PROCESSES", "4200 1 S Mon Nov 2 08:00:00 2026 zsh\n"
                       "4202 4200 S Mon Nov 2 08:00:01 2026 bash\n"
                       "4201 4202 S Mon Nov 2 08:01:00 2026 claude")
    assert notify(text, "desk", "--agent-pid", "4201", "--agent-start", "Mon Nov 2 08:01:00 2026").returncode == 0


def test_real_tmux_exact_session_contract_in_owned_server(inbox, tmp_path, monkeypatch):
    """Real ps ancestry and exact tmux targets, on a separate temporary server."""
    import time

    real_tmux = shutil.which("tmux", path=os.defpath)
    real_ps = shutil.which("ps", path=os.defpath)
    if real_tmux is None or real_ps is None or sys.platform != "linux":
        pytest.skip("Linux, tmux and ps required for the owned-process contract")
    db, text = inbox
    socket = tmp_path / "tmux.sock"
    base = [real_tmux, "-S", str(socket)]
    ready, stop = tmp_path / "ready", tmp_path / "stop"
    agent = tmp_path / "agent.py"
    agent.write_text("import ctypes, time\nfrom pathlib import Path\n"
                     "assert ctypes.CDLL(None).prctl(15, b'claude', 0, 0, 0) == 0\n" +
                     f"Path({str(ready)!r}).touch()\n" +
                     "deadline = time.monotonic() + 60\n" +
                     f"while not Path({str(stop)!r}).exists() and time.monotonic() < deadline:\n"
                     "    time.sleep(0.01)\n")
    shell_command = f"{shlex.quote(sys.executable)} {shlex.quote(str(agent))}; sleep 60"
    created = subprocess.run([*base, "-f", "/dev/null", "new-session", "-d", "-s", "desk", shell_command],
                             capture_output=True, text=True, timeout=5)
    assert created.returncode == 0, created.stderr
    try:
        wrapper = tmp_path / "bin/tmux"
        wrapper.write_text(f"#!{sys.executable}\n" +
                           f"import os, sys\nos.execv({real_tmux!r}, {base!r} + sys.argv[1:])\n")
        (tmp_path / "bin/ps").write_text(f"#!{sys.executable}\n" +
                                       f"import os, sys\nos.execv({real_ps!r}, [{real_ps!r}] + sys.argv[1:])\n")
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "owned agent did not become ready"
        monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
        from notify_session import resolve_agent, resolve_pane
        parent_pane, pane_pid = resolve_pane("desk")
        agent_pid, agent_start = resolve_agent(pane_pid)
        assert agent_pid != pane_pid  # This is the agent inside the surviving shell.
        binding = ["--pane", parent_pane, "--pane-pid", str(pane_pid),
                   "--agent-pid", str(agent_pid), "--agent-start", agent_start]
        out = notify(text, "desk", *binding)
        assert out.returncode == 0, out.stderr
        receipt = json.loads(out.stdout)
        pane = subprocess.run([*base, "display-message", "-p", "-t", receipt["notify_target"], "#{pane_id}"],
                              capture_output=True, text=True, timeout=5)
        assert pane.returncode == 0 and pane.stdout.strip() == parent_pane
        assert receipt["notify_target"] == "=desk:." + parent_pane
        other = subprocess.run([*base, "new-window", "-t", "desk:", "sleep 60"],
                               capture_output=True, text=True, timeout=5)
        assert other.returncode == 0, other.stderr
        shared = notify(text, "desk", *binding)
        assert shared.returncode == 0, shared.stderr
        assert json.loads(shared.stdout)["notify_target"] == receipt["notify_target"]
        before = db.read_bytes()
        assert notify(text).returncode != 0  # No active-window fallback in a shared session.
        assert db.read_bytes() == before
        assert notify(text, "des").returncode != 0
        assert db.read_bytes() == before
        stop.touch()  # Only the owned temporary agent observes this file.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                resolve_agent(pane_pid, agent_pid, agent_start)
            except ValueError:
                break
            time.sleep(0.01)
        else:
            pytest.fail("owned agent did not exit")
        assert resolve_pane("desk", parent_pane)[1] == pane_pid
        assert notify(text, "desk", *binding).returncode != 0
        assert db.read_bytes() == before
    finally:
        subprocess.run([*base, "kill-server"], capture_output=True, timeout=5, check=True)


def test_zombie_agent_under_unchanged_pane_root_is_never_live(inbox, tmp_path, monkeypatch):
    """Real procps zombie: comm prints as 'claude <defunct', so a command-last
    ps layout misreads the state column and a zombie passes as live."""
    import time

    real_tmux = shutil.which("tmux", path=os.defpath)
    real_ps = shutil.which("ps", path=os.defpath)
    if real_tmux is None or real_ps is None or sys.platform != "linux":
        pytest.skip("Linux, tmux and ps required for the real-zombie contract")
    db, text = inbox
    socket = tmp_path / "tmux.sock"
    base = [real_tmux, "-S", str(socket)]
    ready, stop = tmp_path / "ready", tmp_path / "stop"
    # The pane root forks one child named 'claude' and never reaps it: when the
    # child exits it stays a real zombie under the unchanged pane root.
    root = tmp_path / "pane_root.py"
    root.write_text("import ctypes, os, time\nfrom pathlib import Path\n"
                    "child = os.fork()\n"
                    "if child == 0:\n"
                    "    assert ctypes.CDLL(None).prctl(15, b'claude', 0, 0, 0) == 0\n"
                    f"    Path({str(ready)!r}).touch()\n"
                    "    deadline = time.monotonic() + 120\n"
                    f"    while not Path({str(stop)!r}).exists() and time.monotonic() < deadline:\n"
                    "        time.sleep(0.01)\n"
                    "    os._exit(0)\n"
                    "time.sleep(120)\n")
    created = subprocess.run([*base, "-f", "/dev/null", "new-session", "-d", "-s", "desk",
                              f"{shlex.quote(sys.executable)} {shlex.quote(str(root))}"],
                             capture_output=True, text=True, timeout=5)
    assert created.returncode == 0, created.stderr
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "synthetic agent did not become ready"
        wrapper = tmp_path / "bin/tmux"
        wrapper.parent.mkdir(exist_ok=True)
        wrapper.write_text(f"#!{sys.executable}\n" +
                           f"import os, sys\nos.execv({real_tmux!r}, {base!r} + sys.argv[1:])\n")
        (tmp_path / "bin/ps").write_text(f"#!{sys.executable}\n" +
                                         f"import os, sys\nos.execv({real_ps!r}, [{real_ps!r}] + sys.argv[1:])\n")
        monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
        monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
        from notify_session import resolve_agent, resolve_pane
        pane, pane_pid = resolve_pane("desk")
        agent_pid, agent_start = resolve_agent(pane_pid)
        before = db.read_bytes()
        stop.touch()  # The child exits and is left unreaped: a real zombie.
        deadline = time.monotonic() + 5
        state = "?"
        while time.monotonic() < deadline:
            state = Path(f"/proc/{agent_pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if state == "Z":
                break
            time.sleep(0.01)
        assert state == "Z", f"bound agent did not become a zombie (state {state})"
        binding = ["--pane", pane, "--pane-pid", str(pane_pid),
                   "--agent-pid", str(agent_pid), "--agent-start", agent_start]
        result = notify(text, "desk", *binding)
        assert result.returncode != 0
        assert db.read_bytes() == before
        with pytest.raises(ValueError):
            resolve_agent(pane_pid, agent_pid, agent_start)
    finally:
        subprocess.run([*base, "kill-server"], capture_output=True, timeout=5, check=True)
