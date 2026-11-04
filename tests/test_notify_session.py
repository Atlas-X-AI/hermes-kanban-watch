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
print(os.environ.get('TMUX_PANES', '%42 ' + os.environ.get('PANE_FAKE_ROOT_PID', '4200')))
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
    other = int(os.environ["PANE_FAKE_AGENT_PID"]) + 10000  # Never a real pane: ambiguity only.
    monkeypatch.setenv("TMUX_PANES", "%43 " + str(other) + chr(10) + "%42 " + os.environ["PANE_FAKE_ROOT_PID"])
    result = notify(text, "desk", "--pane", "%42", "--pane-pid", os.environ["PANE_FAKE_ROOT_PID"])
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
    # Intermediate shells in the ps table, with /proc-visible endpoints: the
    # recorded agent stays this process; only its ps-table parent chain grows.
    agent = int(os.environ["PANE_FAKE_AGENT_PID"])
    root = int(os.environ["PANE_FAKE_ROOT_PID"])
    monkeypatch.setenv("PANE_PROCESSES", f"{root} 1 S Mon Nov 2 08:00:00 2026 zsh\n"
                       f"{agent + 1} {root} S Mon Nov 2 08:00:01 2026 bash\n"
                       f"{agent} {agent + 1} S Mon Nov 2 08:01:00 2026 claude")
    assert notify(text, "desk", "--agent-pid", str(agent),
                  "--agent-start", "Mon Nov 2 08:01:00 2026").returncode == 0


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


def test_agent_exit_while_notify_waits_on_write_lock_queues_nothing(inbox, tmp_path, monkeypatch):
    """The bound agent is revalidated inside the inbox write lock. A second
    connection holds the write lock; the bound agent exits while notify waits
    on it: refuse, queue nothing. Control: the agent stays alive and the row is
    queued once the lock is released."""
    import time

    real_tmux = shutil.which("tmux", path=os.defpath)
    real_ps = shutil.which("ps", path=os.defpath)
    if real_tmux is None or real_ps is None or sys.platform != "linux":
        pytest.skip("Linux, tmux and ps required for the in-lock revalidation contract")
    db, text = inbox
    socket = tmp_path / "tmux.sock"
    base = [real_tmux, "-S", str(socket)]
    ready, stop = tmp_path / "ready", tmp_path / "stop"
    ps_calls = tmp_path / "ps-calls"
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
    holder = sqlite3.connect(db, timeout=5, isolation_level=None)
    control = red = None
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
                                         "import os, sys\nfrom pathlib import Path\n" +
                                         f"with Path({str(ps_calls)!r}).open('a') as log:\n" +
                                         "    log.write('ps ' + ' '.join(sys.argv[1:]) + '\\n')\n" +
                                         f"os.execv({real_ps!r}, [{real_ps!r}] + sys.argv[1:])\n")
        wrapper.chmod(0o755)
        (tmp_path / "bin/ps").chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
        monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
        from notify_session import resolve_agent, resolve_pane
        pane, pane_pid = resolve_pane("desk")
        agent_pid, agent_start = resolve_agent(pane_pid)
        binding = ["--pane", pane, "--pane-pid", str(pane_pid),
                   "--agent-pid", str(agent_pid), "--agent-start", agent_start]

        def wait_first_validation(previous):
            """Block until notify's first ps read has certainly been parsed."""
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                seen = len(ps_calls.read_text().splitlines()) if ps_calls.exists() else 0
                if seen > previous:
                    time.sleep(0.2)  # Parsing and the lock wait, still inside the 2 s timeout.
                    return seen
                time.sleep(0.01)
            pytest.fail("notify never ran its first ps validation")

        def spawn(key):
            return subprocess.Popen(
                [sys.executable, str(PLUGIN / "scripts/notify_session.py"), "--session", "desk",
                 "--text-file", str(text), "--key", key, *binding],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # Control: a live agent still gets its row once the lock is released.
        seen = len(ps_calls.read_text().splitlines())
        holder.execute("begin immediate")
        control = spawn("kanban-watch:d4:control")
        wait_first_validation(seen)
        time.sleep(0.3)  # Parsing and the lock wait, still inside the 2 s timeout.
        holder.execute("commit")
        _, err = control.communicate(timeout=15)
        assert control.returncode == 0, err
        with sqlite3.connect(db) as conn:
            assert conn.execute("select dedupe_key from messages").fetchall() == [
                ("kanban-watch:d4:control",)]

        # The agent exits while notify waits on the write lock: nothing queued.
        seen = len(ps_calls.read_text().splitlines())
        holder.execute("begin immediate")
        red = spawn("kanban-watch:d4:exit")
        wait_first_validation(seen)
        stop.touch()  # The child exits and stays an unreaped zombie.
        deadline = time.monotonic() + 5
        state = "?"
        while time.monotonic() < deadline:
            state = Path(f"/proc/{agent_pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            if state == "Z":
                break
            time.sleep(0.01)
        assert state == "Z", f"bound agent did not become a zombie (state {state})"
        holder.execute("commit")
        out, _ = red.communicate(timeout=15)
        assert red.returncode != 0, out
        with sqlite3.connect(db) as conn:
            assert conn.execute("select dedupe_key from messages").fetchall() == [
                ("kanban-watch:d4:control",)]
    finally:
        for process in (control, red):
            if process is not None and process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
        holder.close()
        subprocess.run([*base, "kill-server"], capture_output=True, timeout=5, check=True)


# --- Round 3: the /proc-only recheck inside the inbox write lock. ---

import threading  # noqa: E402
import time  # noqa: E402

REAL_TMUX_R3 = shutil.which("tmux", path=os.defpath)
REAL_PS_R3 = shutil.which("ps", path=os.defpath)
REAL_CONNECT_R3 = sqlite3.connect

# The pane process is a dispatcher: it forks prctl-named children on request and
# never reaps them unless asked, so an exited child is a real zombie under an
# unchanged pane root until the test asks for it to be reaped.
PANE_DISPATCHER_R3 = '''import ctypes, os, sys, time
from pathlib import Path
work = Path(sys.argv[1])
work.mkdir(parents=True, exist_ok=True)
deadline = time.monotonic() + 600
while time.monotonic() < deadline:
    rootname = work / "rootname"
    if rootname.exists():
        name = rootname.read_text().strip()
        rootname.unlink()
        assert ctypes.CDLL(None).prctl(15, name.encode(), 0, 0, 0) == 0
        (work / "pid0").write_text(str(os.getpid()))
        (work / "count").write_text("0")
        (work / "ready0").touch()
    request = work / "spawn"
    if request.exists():
        names = request.read_text().split("|")
        request.unlink()
        try:
            index = int((work / "count").read_text())
        except (FileNotFoundError, ValueError):
            index = 0
        for offset, name in enumerate(names):
            slot = index + offset
            if os.fork() == 0:
                assert ctypes.CDLL(None).prctl(15, name.encode(), 0, 0, 0) == 0
                (work / f"pid{slot}").write_text(str(os.getpid()))
                (work / f"ready{slot}").touch()
                end = time.monotonic() + 600
                while not (work / f"stop{slot}").exists() and time.monotonic() < end:
                    time.sleep(0.005)
                os._exit(0)
        (work / "count").write_text(str(index + len(names)))
    reap = work / "reap"
    if reap.exists():
        slot = reap.read_text().strip()
        reap.unlink()
        try:
            os.waitpid(int((work / f"pid{slot}").read_text()), os.WNOHANG)
        except (FileNotFoundError, ValueError, ChildProcessError):
            pass
    time.sleep(0.005)
'''


def _r3_state_of(pid):
    """The state letter from /proc/<pid>/stat parsed after the last ')', or 'gone'."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (FileNotFoundError, ProcessLookupError):
        return "gone"


def _r3_wait_state(pid, want, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _r3_state_of(pid) in want:
            return
        time.sleep(0.01)
    pytest.fail(f"pid {pid} never reached {want} (last state {_r3_state_of(pid)})")


def _r3_rows(inbox):
    with sqlite3.connect(inbox) as conn:
        return conn.execute("select id from messages").fetchall()


def _r3_lock_free(inbox, timeout=1.0):
    """A second connection's begin immediate succeeds at once (D8)."""
    probe = sqlite3.connect(inbox, timeout=timeout, isolation_level=None)
    try:
        probe.execute("begin immediate")
        probe.execute("rollback")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        probe.close()


def _r3_wait_ps(pslog, previous, timeout=30):
    """Block until one more logged ps call has returned."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        lines = len(pslog.read_text().splitlines()) if pslog.exists() else 0
        if lines > previous:
            return lines
        time.sleep(0.002)
    pytest.fail("notify never ran its pre-lock ps validation")


@pytest.fixture
def proc_window(inbox, tmp_path, monkeypatch):
    """Real tmux and a logged real ps on PATH, one disposable 'desk' session whose
    pane root forks prctl-named children on request. start(names) returns the
    first agent's (slot, pid); stop(slot) leaves a zombie; reap(slot) reaps it."""
    if REAL_TMUX_R3 is None or REAL_PS_R3 is None or sys.platform != "linux":
        pytest.skip("Linux, tmux and ps required for the in-lock /proc recheck contract")
    db, text = inbox
    socket = tmp_path / "tmux.sock"
    base = [REAL_TMUX_R3, "-S", str(socket)]
    work = tmp_path / "pane"
    work.mkdir()
    pslog = tmp_path / "ps-log"
    dispatcher = tmp_path / "pane_dispatcher.py"
    dispatcher.write_text(PANE_DISPATCHER_R3)
    created = subprocess.run([*base, "-f", "/dev/null", "new-session", "-d", "-s", "desk",
                              " ".join(shlex.quote(part) for part in
                                       [sys.executable, str(dispatcher), str(work)])],
                             capture_output=True, text=True, timeout=5)
    assert created.returncode == 0, created.stderr

    def start(names, root_name=None):
        if root_name is not None:
            (work / "rootname.tmp").write_text(root_name)
            (work / "rootname.tmp").rename(work / "rootname")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not (work / "ready0").exists():
                time.sleep(0.01)
            assert (work / "ready0").exists(), "dispatcher never renamed itself"
            return 0, int((work / "pid0").read_text())
        (work / "spawn.new").write_text("|".join(names))
        (work / "spawn.new").rename(work / "spawn")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not (work / "count").exists():
            time.sleep(0.01)
        count = int((work / "count").read_text())
        assert count >= 1, "dispatcher never spawned the synthetic agents"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not all((work / f"ready{i}").exists()
                                                      for i in range(count)):
            time.sleep(0.01)
        assert all((work / f"ready{i}").exists() for i in range(count)), "agents not ready"
        return 0, int((work / "pid0").read_text())

    def stop(slot):
        (work / f"stop{slot}").touch()
        _r3_wait_state(int((work / f"pid{slot}").read_text()), {"Z", "gone"})

    def reap(slot):
        (work / "reap").write_text(str(slot))
        deadline = time.monotonic() + 5
        pid = int((work / f"pid{slot}").read_text())
        while time.monotonic() < deadline and _r3_state_of(pid) != "gone":
            time.sleep(0.01)
        assert _r3_state_of(pid) == "gone", f"pid {pid} was never reaped"

    try:
        bindir = tmp_path / "bin"
        (bindir / "tmux").write_text(f"#!{sys.executable}\nimport os, sys\n"
                                     f"os.execv({REAL_TMUX_R3!r}, {base!r} + sys.argv[1:])\n")
        (bindir / "ps").write_text(f"#!{sys.executable}\n" +
                                   "import subprocess, sys, time\n"
                                   f"r = subprocess.run([{REAL_PS_R3!r}] + sys.argv[1:], capture_output=True)\n"
                                   "sys.stdout.buffer.write(r.stdout)\n"
                                   "sys.stderr.buffer.write(r.stderr)\n"
                                   f"with open({str(pslog)!r}, 'a') as log:\n"
                                   "    log.write('%.6f\\n' % time.time())\n"
                                   "sys.exit(r.returncode)\n")
        for wrapper in bindir.iterdir():
            wrapper.chmod(0o755)
        monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
        monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
        yield {"inbox": db, "text": text, "base": base, "work": work,
               "pslog": pslog, "start": start, "stop": stop, "reap": reap}
    finally:
        subprocess.run([*base, "kill-server"], capture_output=True, timeout=5, check=True)


def _r3_trace(ns, monkeypatch):
    """Trace notify's SQL: the waiting event fires on begin immediate, and the
    event list records when the insert starts. Connections made by the test
    itself use _r3_untraced_connect."""
    events, waiting = [], threading.Event()

    class Traced(sqlite3.Connection):
        def execute(self, sql, *args):
            if sql.strip().lower() == "begin immediate":
                waiting.set()
            events.append((sql.split()[0].lower() + "-start", time.time()))
            result = super().execute(sql, *args)
            events.append((sql.split()[0].lower() + "-end", time.time()))
            return result

    def connect(*args, **kwargs):
        kwargs["factory"] = Traced
        return REAL_CONNECT_R3(*args, **kwargs)

    monkeypatch.setattr(ns.sqlite3, "connect", connect)
    return events, waiting


def test_slow_tmux_does_not_block_other_inbox_writers(proc_window, monkeypatch, tmp_path):
    """D7: with tmux and ps slowed to 3 s, a second inbox writer that starts just
    after notify's begin immediate, with a 1 s busy timeout, must succeed: inside
    the write lock only /proc is read, never a subprocess."""
    slot, agent = proc_window["start"](["claude"])
    inbox = proc_window["inbox"]
    slow = tmp_path / "sbin"
    slow.mkdir()
    flag = slow / "slow-on"
    (slow / "tmux").write_text(f"#!{sys.executable}\nimport os, sys, time\n"
                               f"if os.path.exists({str(flag)!r}):\n    time.sleep(3)\n"
                               f"os.execv({REAL_TMUX_R3!r}, [{REAL_TMUX_R3!r}, '-S', "
                               f"{str(proc_window['base'][2])!r}] + sys.argv[1:])\n")
    (slow / "ps").write_text(f"#!{sys.executable}\nimport os, subprocess, sys, time\n"
                             f"if os.path.exists({str(flag)!r}):\n    time.sleep(3)\n"
                             f"r = subprocess.run([{REAL_PS_R3!r}] + sys.argv[1:], capture_output=True)\n"
                             "sys.stdout.buffer.write(r.stdout)\n"
                             "sys.stderr.buffer.write(r.stderr)\n"
                             f"with open({str(proc_window['pslog'])!r}, 'a') as log:\n"
                             "    log.write('%.6f\\n' % time.time())\n"
                             "sys.exit(r.returncode)\n")
    for wrapper in slow.iterdir():
        wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(slow) + os.pathsep + os.environ["PATH"])
    import notify_session as ns
    events, waiting = _r3_trace(ns, monkeypatch)
    pane, pane_pid = ns.resolve_pane("desk")
    agent_pid, agent_start = ns.resolve_agent(pane_pid)
    assert agent_pid == agent
    flag.touch()
    outcome = {}

    def call():
        try:
            outcome["result"] = ns.notify("desk", "probe: slow tmux", "kanban-watch:rr3:slow",
                                          inbox, pane=pane, pane_pid=pane_pid,
                                          agent_pid=agent_pid, agent_start=agent_start)
        except Exception as exc:  # noqa: BLE001 - any outcome is recorded
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    assert waiting.wait(30), "notify never took the write lock"
    other = REAL_CONNECT_R3(inbox, timeout=1.0, isolation_level=None)
    writer, waited = None, -1.0
    try:
        t0 = time.monotonic()
        other.execute("begin immediate")
        other.execute("insert into messages(sender,target,message_type,payload) "
                      "values('other-writer','x','t','{}')")
        other.execute("commit")
        writer, waited = "ok", round(time.monotonic() - t0, 3)
    except sqlite3.OperationalError as exc:
        writer = f"OperationalError: {exc}"
    finally:
        other.close()
    worker.join(45)
    assert not worker.is_alive()
    assert "result" in outcome, outcome  # Control: the agent is alive, notify still queues.
    assert writer == "ok", f"second inbox writer locked out after {waited}s: {outcome}"
    assert waited < 0.5, f"second writer waited {waited}s: the lock is held too long"


def test_invisible_agent_after_validation_refuses_without_subprocess(proc_window, monkeypatch, tmp_path):
    """D10: the agent is reaped after the pre-lock ps and before notify's /proc
    read. The send must refuse, and no tmux or ps may run under the inbox write
    lock: tmux is slowed to 3 s, so a second writer with a 1 s busy timeout
    succeeds only if the lock is never held across a subprocess."""
    slot, agent = proc_window["start"](["claude"])
    inbox = proc_window["inbox"]
    slow = tmp_path / "sbin"
    slow.mkdir()
    flag = slow / "slow-on"
    (slow / "tmux").write_text(f"#!{sys.executable}\nimport os, sys, time\n"
                               f"if os.path.exists({str(flag)!r}):\n    time.sleep(3)\n"
                               f"os.execv({REAL_TMUX_R3!r}, [{REAL_TMUX_R3!r}, '-S', "
                               f"{str(proc_window['base'][2])!r}] + sys.argv[1:])\n")
    (slow / "ps").write_text(f"#!{sys.executable}\nimport os, subprocess, sys, time\n"
                             f"if os.path.exists({str(flag)!r}):\n    time.sleep(3)\n"
                             f"r = subprocess.run([{REAL_PS_R3!r}] + sys.argv[1:], capture_output=True)\n"
                             "sys.stdout.buffer.write(r.stdout)\n"
                             "sys.stderr.buffer.write(r.stderr)\n"
                             f"with open({str(proc_window['pslog'])!r}, 'a') as log:\n"
                             "    log.write('%.6f' % time.time() + chr(10))\n"
                             "sys.exit(r.returncode)\n")
    for wrapper in slow.iterdir():
        wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", str(slow) + os.pathsep + os.environ["PATH"])
    monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
    import notify_session as ns
    pane, pane_pid = ns.resolve_pane("desk")
    agent_pid, agent_start = ns.resolve_agent(pane_pid)
    assert agent_pid == agent
    seen = len(proc_window["pslog"].read_text().splitlines())
    reaped = []
    real_run = ns.subprocess.run

    def run_then_reap(argv, *args, **kwargs):
        result = real_run(argv, *args, **kwargs)
        if argv[0] == "ps" and not reaped:
            reaped.append(1)
            proc_window["stop"](slot)  # Exit and stay a zombie ...
            proc_window["reap"](slot)  # ... then reaped: /proc no longer has it.
            flag.touch()  # Any tmux or ps from here on is slowed to 3 s.
        return result

    monkeypatch.setattr(ns.subprocess, "run", run_then_reap)
    outcome = {}

    def call():
        try:
            outcome["result"] = ns.notify("desk", "probe: invisible agent", "kanban-watch:rr4:invisible",
                                          inbox, pane=pane, pane_pid=pane_pid,
                                          agent_pid=agent_pid, agent_start=agent_start)
        except Exception as exc:  # noqa: BLE001 - any refusal is recorded
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not reaped:
        time.sleep(0.002)
    assert reaped, "the pre-lock ps never returned"
    time.sleep(0.15)  # notify has reached (and, under a fallback, holds) the write lock.
    other = REAL_CONNECT_R3(inbox, timeout=1.0, isolation_level=None)
    writer, waited = None, -1.0
    t0 = time.monotonic()
    try:
        other.execute("begin immediate")
        other.execute("insert into messages(sender,target,message_type,payload) "
                      "values('other-writer','x','t','{}')")
        other.execute("commit")
        writer, waited = "ok", round(time.monotonic() - t0, 3)
    except sqlite3.OperationalError as exc:
        writer, waited = f"OperationalError: {exc}", round(time.monotonic() - t0, 3)
    finally:
        other.close()
    worker.join(40)
    assert not worker.is_alive()
    assert writer == "ok", f"second inbox writer locked out after {waited}s: {outcome}"
    assert waited < 0.5, f"second writer waited {waited}s: the lock is held too long"
    assert "error" in outcome and "not visible in /proc" in outcome["error"], outcome
    with sqlite3.connect(inbox) as conn:
        queued = conn.execute("select count(*) from messages where sender='kanban-watcher'").fetchone()[0]
    assert queued == 0, outcome
    # The pre-lock validation is the only ps call: none ran inside the lock.
    assert len(proc_window["pslog"].read_text().splitlines()) == seen + 1, \
        "a ps call ran inside the write lock"


def test_agent_exit_between_validation_and_lock_refuses(proc_window):
    """D6: the agent exits after the pre-lock validation and before the lock is
    taken (a second connection holds the lock while it exits): refused, nothing
    queued, the write lock is free again after the refusal, and the in-lock
    recheck ran no subprocess (exactly one ps call: the pre-lock validation)."""
    slot, agent = proc_window["start"](["claude"])
    inbox = proc_window["inbox"]
    import notify_session as ns
    pane, pane_pid = ns.resolve_pane("desk")
    agent_pid, agent_start = ns.resolve_agent(pane_pid)
    assert agent_pid == agent
    holder = REAL_CONNECT_R3(inbox, timeout=5, isolation_level=None)
    holder.execute("begin immediate")
    outcome = {}
    seen = len(proc_window["pslog"].read_text().splitlines())

    def call():
        try:
            outcome["result"] = ns.notify("desk", "probe: exit window", "kanban-watch:rr3:exit",
                                          inbox, pane=pane, pane_pid=pane_pid,
                                          agent_pid=agent_pid, agent_start=agent_start)
        except Exception as exc:  # noqa: BLE001 - any refusal is recorded
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    _r3_wait_ps(proc_window["pslog"], seen)  # The pre-lock ps has returned.
    time.sleep(0.15)  # The parse finishes; notify is blocked on the write lock.
    proc_window["stop"](slot)  # The child exits and stays an unreaped zombie.
    _r3_wait_state(agent, {"Z"})
    holder.execute("commit")
    holder.close()
    worker.join(15)
    assert not worker.is_alive()
    assert "result" not in outcome, outcome
    assert "nothing queued" in outcome["error"], outcome
    assert not _r3_rows(inbox), outcome
    assert _r3_lock_free(inbox), "the inbox write lock is not free after the refusal"
    assert not Path(str(inbox) + "-journal").exists(), "a journal was left after the refusal"
    # No subprocess inside the lock: the pre-lock validation is the only ps call.
    assert len(proc_window["pslog"].read_text().splitlines()) == seen + 1, \
        "a ps call ran inside the write lock"


def test_replaced_agent_with_same_name_and_new_start_time_refuses(proc_window, monkeypatch):
    """D6: while notify waits on the write lock, the recorded agent exits and a
    same-named replacement with a new pid and start time appears: refused inside
    the lock by the /proc identity, with no ps call inside the lock."""
    slot, agent = proc_window["start"](["claude"])
    inbox = proc_window["inbox"]
    import notify_session as ns
    pane, pane_pid = ns.resolve_pane("desk")
    agent_pid, agent_start = ns.resolve_agent(pane_pid)
    assert agent_pid == agent
    holder = REAL_CONNECT_R3(inbox, timeout=5, isolation_level=None)
    holder.execute("begin immediate")
    outcome = {}
    seen = len(proc_window["pslog"].read_text().splitlines())

    def call():
        try:
            outcome["result"] = ns.notify("desk", "probe: replaced agent", "kanban-watch:rr3:replaced",
                                          inbox, pane=pane, pane_pid=pane_pid,
                                          agent_pid=agent_pid, agent_start=agent_start)
        except Exception as exc:  # noqa: BLE001 - any refusal is recorded
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    _r3_wait_ps(proc_window["pslog"], seen)  # The pre-lock ps saw the original agent.
    time.sleep(0.15)  # The parse finishes; notify is blocked on the write lock.
    proc_window["stop"](slot)  # The recorded agent exits and stays a zombie.
    proc_window["start"](["claude"])  # A same-named replacement under the same pane.
    holder.execute("commit")
    holder.close()
    worker.join(15)
    assert not worker.is_alive()
    assert "result" not in outcome, outcome
    assert "nothing queued" in outcome["error"], outcome
    assert not _r3_rows(inbox), outcome
    assert _r3_lock_free(inbox)
    assert len(proc_window["pslog"].read_text().splitlines()) == seen + 1, \
        "a ps call ran inside the write lock"


def test_proc_recheck_refusals_are_pinned(proc_window, monkeypatch):
    """The /proc recheck refuses on each axis: the agent gone, a zombie, a
    changed agent or pane start time, and a broken parent chain."""
    import notify_session as ns
    slot, agent = proc_window["start"](["claude"])
    pane, pane_pid = ns.resolve_pane("desk")
    agent_pid, agent_start = ns.resolve_agent(pane_pid)
    agent_identity = ns._proc_identity(agent_pid)
    pane_identity = ns._proc_identity(pane_pid)
    assert agent_identity and pane_identity and agent_identity[0] not in {"Z", "X"}
    # Control: the real identities pass.
    ns.revalidate_in_lock(pane_pid, pane_identity[2], agent_pid, agent_identity[2])
    # Forged agent start time (the same pid reused by a new process).
    with pytest.raises(ValueError, match="nothing queued"):
        ns.revalidate_in_lock(pane_pid, pane_identity[2], agent_pid, agent_identity[2] - 1)
    # Forged pane start time (the pane process was replaced).
    with pytest.raises(ValueError, match="nothing queued"):
        ns.revalidate_in_lock(pane_pid, pane_identity[2] - 1, agent_pid, agent_identity[2])
    # The parent chain no longer reaches the recorded pane process.
    with pytest.raises(ValueError, match="nothing queued"):
        ns.revalidate_in_lock(os.getpid(), ns._proc_identity(os.getpid())[2],
                              agent_pid, agent_identity[2])
    # The agent exited: a real zombie under the unchanged pane root.
    proc_window["stop"](slot)
    _r3_wait_state(agent_pid, {"Z"})
    with pytest.raises(ValueError, match="nothing queued"):
        ns.revalidate_in_lock(pane_pid, pane_identity[2], agent_pid, agent_identity[2])
    # The agent was reaped: /proc no longer has it at all.
    proc_window["reap"](slot)
    with pytest.raises(ValueError, match="nothing queued"):
        ns.revalidate_in_lock(pane_pid, pane_identity[2], agent_pid, agent_identity[2])


FAKE_TABLE_R3 = ("4200 1 S Mon Nov 2 08:00:00 2026 zsh\n"
                 "4201 4200 S Mon Nov 2 08:01:00 2026 claude")
FAKE_TABLE_SWAPPED_R3 = ("4200 1 S Mon Nov 2 08:00:00 2026 zsh\n"
                         "4201 4200 S Mon Nov 2 08:02:00 2026 claude")


def test_agent_is_pane_root_inprocess_queues(proc_window, monkeypatch):
    """D9: when the pane's own root process is the claude agent (claude run as
    the pane command), the in-lock recheck accepts it. An agent whose PID is the
    pane PID is in the pane."""
    slot, agent = proc_window["start"](["claude"], root_name="claude")
    inbox = proc_window["inbox"]
    import notify_session as ns
    pane, pane_pid = ns.resolve_pane("desk")
    agent_pid, agent_start = ns.resolve_agent(pane_pid)
    assert agent_pid == pane_pid == agent, (pane_pid, agent_pid, agent)
    outcome = {}
    try:
        outcome["result"] = ns.notify("desk", "probe: pane root", "kanban-watch:rr4:self",
                                      inbox, pane=pane, pane_pid=pane_pid,
                                      agent_pid=agent_pid, agent_start=agent_start)
    except Exception as exc:  # noqa: BLE001 - any refusal is recorded
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    assert "result" in outcome, outcome
    assert len(_r3_rows(inbox)) == 1


def test_agent_is_pane_root_tick_path_queues(proc_window, board, tmp_path):
    """D9, full tick path: the pane command is claude, so prepare records the
    pane root itself as the agent and the tick delivers to the inbox."""
    slot, agent = proc_window["start"](["claude"], root_name="claude")
    prepare = subprocess.run([sys.executable, str(PLUGIN / "scripts/watcher_launch.py"),
                              "--prepare-only", "--board", "demo", "--session", "desk",
                              "--cards", "t_done"], capture_output=True, text=True, timeout=30)
    assert prepare.returncode == 0, prepare.stderr
    receipt = json.loads(prepare.stdout)
    config = json.loads(Path(receipt["config"]).read_text())
    assert config["pane_pid"] == config["agent_pid"] == agent, config
    result = subprocess.run([sys.executable, str(PLUGIN / "scripts/watcher_tick.py"),
                             "--config", receipt["config"]], capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stderr
    assert len(_r3_rows(proc_window["inbox"])) == 1, result.stdout + result.stderr


def test_replaced_and_outside_pane_agents_still_refuse(proc_window):
    """D9 controls, on the /proc path: a replaced agent and an agent outside the
    pane still refuse inside the lock."""
    slot, agent = proc_window["start"](["claude"])
    inbox = proc_window["inbox"]
    import notify_session as ns
    pane, pane_pid = ns.resolve_pane("desk")
    agent_pid, agent_start = ns.resolve_agent(pane_pid)
    assert agent_pid == agent
    # A replaced agent: the recorded one exits, a same-named one appears under the pane.
    proc_window["stop"](slot)  # zombie under the unchanged pane root
    _r3_wait_state(agent_pid, {"Z"})
    with pytest.raises(ValueError, match="nothing queued"):
        ns.notify("desk", "probe: replaced", "kanban-watch:rr4:replaced", inbox,
                  pane=pane, pane_pid=pane_pid, agent_pid=agent_pid, agent_start=agent_start)
    # An agent outside the pane: the recorded identity is a live process whose
    # parent chain never reaches the pane root. The in-lock walk still refuses.
    outside = os.getpid()
    with pytest.raises(ValueError, match="no longer runs under the pane"):
        ns.revalidate_in_lock(pane_pid, ns._proc_identity(pane_pid)[2],
                              outside, ns._proc_identity(outside)[2])
    assert not _r3_rows(inbox)


def _r3_fake_swap_refusal(inbox, tmp_path, monkeypatch, table_after):
    """In-process notify against the fake ps table, with a patched /proc reader
    that speaks the same fake PIDs; while notify is blocked on the write lock,
    the table changes to table_after. Returns the outcome dict. No host PID is
    assumed absent: the fake identities live only in the reader."""
    monkeypatch.setenv("PANE_PROCESSES", FAKE_TABLE_R3)
    logging_ps = tmp_path / "bin/ps"
    logging_ps.write_text(f"#!{sys.executable}\n" +
                          "import os, sys, time\n"
                          "print(os.environ.get('PANE_PROCESSES'))\n"
                          f"with open({str(tmp_path / 'ps-calls-r3')!r}, 'a') as log:\n"
                          "    log.write('%.6f' % time.time() + chr(10))\n")
    logging_ps.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "bin") + os.pathsep + os.environ["PATH"])
    monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
    import notify_session as ns
    real_identity = ns._proc_identity
    fake = {4200: ("S", 1, 111), 4201: ("S", 4200, 222)}

    def fake_identity(pid):
        # Fake PIDs speak the table; every other PID (this process included,
        # for the has-/proc probe) reads the host /proc as before.
        return fake.get(int(pid), real_identity(pid))

    def fake_start(pid):
        identity = fake_identity(pid)
        return None if identity is None else identity[2]

    monkeypatch.setattr(ns, "_proc_identity", fake_identity)
    monkeypatch.setattr(ns, "_proc_start", fake_start)
    # The fake tmux speaks the same fake pane PID; the host /proc is bypassed
    # for the pair under test, so no host PID is assumed absent.
    monkeypatch.setenv("TMUX_PANES", "%42 4200")
    inbox = inbox[0]  # The fixture yields (db, text); only the database matters here.
    outcome = {}

    def call():
        try:
            outcome["result"] = ns.notify("desk", "probe: table swap", "kanban-watch:rr3:swap",
                                          inbox, pane="%42", pane_pid=4200,
                                          agent_pid=4201, agent_start="Mon Nov 2 08:01:00 2026")
        except Exception as exc:  # noqa: BLE001 - any refusal is recorded
            outcome["error"] = f"{type(exc).__name__}: {exc}"

    holder = REAL_CONNECT_R3(inbox, timeout=5, isolation_level=None)
    holder.execute("begin immediate")
    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    _r3_wait_ps(tmp_path / "ps-calls-r3", 0)  # The pre-lock ps has returned.
    time.sleep(0.15)  # The parse finishes; notify is blocked on the write lock.
    monkeypatch.setenv("PANE_PROCESSES", table_after)
    # The same-PID reuse shape: the agent PID survives with a new start time.
    fake[4201] = ("S", 4200, 333)
    holder.execute("commit")
    holder.close()
    worker.join(15)
    assert not worker.is_alive()
    return outcome


def test_identity_swap_inside_the_lock_refuses(inbox, tmp_path, monkeypatch):
    """D6 (the same-PID reuse shape, /proc-pinned): the agent PID survives with
    a new start time while notify waits on the write lock; the send is refused
    by the in-lock start-time comparison."""
    outcome = _r3_fake_swap_refusal(inbox, tmp_path, monkeypatch, FAKE_TABLE_SWAPPED_R3)
    database = inbox[0]
    assert "result" not in outcome, outcome
    assert "Recorded process identity changed" in outcome["error"], outcome
    assert not _r3_rows(database)
    assert _r3_lock_free(database)


def test_inprocess_refusal_leaves_the_inbox_lock_free_at_once(inbox, tmp_path, monkeypatch):
    """D8: after an in-process refusal a second connection's begin immediate
    succeeds at once: the refusal rolled back and released the write lock."""
    outcome = _r3_fake_swap_refusal(inbox, tmp_path, monkeypatch, FAKE_TABLE_SWAPPED_R3)
    database = inbox[0]
    assert "result" not in outcome, outcome
    probe = REAL_CONNECT_R3(database, timeout=0, isolation_level=None)
    try:
        t0 = time.monotonic()
        probe.execute("begin immediate")
        elapsed = time.monotonic() - t0
        probe.execute("rollback")
    except sqlite3.OperationalError as exc:
        pytest.fail(f"the write lock is still held after the refusal: {exc}")
    finally:
        probe.close()
    assert elapsed < 0.1, f"begin immediate took {elapsed:.3f}s"
    assert not Path(str(database) + "-journal").exists(), "a journal was left after the refusal"


def test_measured_window_from_proc_read_to_insert(proc_window, monkeypatch, tmp_path):
    """The remaining window, from the in-lock /proc read to the insert, timed
    over 15 real sends. Printout: 'rr3-window-ms min/median/max'."""
    import statistics

    proc_window["start"](["claude"])
    inbox = proc_window["inbox"]
    import notify_session as ns
    events, _waiting = _r3_trace(ns, monkeypatch)
    real_revalidate = ns.revalidate_in_lock
    marks = []

    def timed(pane_pid, pane_start, agent_pid, agent_start):
        result = real_revalidate(pane_pid, pane_start, agent_pid, agent_start)
        marks.append(time.time())  # The /proc reads have just finished.
        return result

    monkeypatch.setattr(ns, "revalidate_in_lock", timed)
    pane, pane_pid = ns.resolve_pane("desk")
    agent_pid, agent_start = ns.resolve_agent(pane_pid)
    samples = []
    for i in range(15):
        events.clear()
        ns.notify("desk", f"probe: window {i}", f"kanban-watch:rr3:timing:{i}", inbox,
                  pane=pane, pane_pid=pane_pid, agent_pid=agent_pid, agent_start=agent_start)
        insert_start = [t for kind, t in events if kind == "insert-start"]
        assert insert_start and marks, "the traced insert never ran"
        samples.append((insert_start[0] - marks[-1]) * 1000)
    summary = {key: round(fn(samples), 3) for key, fn in
               (("min", min), ("median", statistics.median), ("max", max))}
    print(f"rr3-window-ms min/median/max: {summary['min']}/{summary['median']}/{summary['max']}")
    assert 0 <= summary["min"] <= summary["median"] <= summary["max"] < 500, summary
    assert len(_r3_rows(inbox)) == 15
