"""Read-only live guards, using disposable boards and a fake proc tree only.

Set KANBAN_LIVE_TEST_REF=origin/main to run the same CLI controls on an exported
baseline reader, without altering the worktree or touching a live board.
"""
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from conftest import PLUGIN, ROOT
from helpers import create_board


@pytest.fixture
def reader(tmp_path):
    ref = os.environ.get("KANBAN_LIVE_TEST_REF")
    if not ref:
        return PLUGIN / "scripts/kanban_live.py"
    plugin = tmp_path / "baseline/plugin"
    for relative in ("scripts/kanban_live.py", "scripts/wave_status.py", ".claude-plugin/plugin.json"):
        export = subprocess.run(["git", "show", f"{ref}:plugin/{relative}"], cwd=ROOT,
                                capture_output=True, check=True)
        target = plugin / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(export.stdout)
    return plugin / "scripts/kanban_live.py"


@pytest.fixture
def guards(isolated_home, tmp_path, monkeypatch):
    _, hermes = isolated_home
    db = create_board(hermes / "kanban/boards/guards/kanban.db")
    with sqlite3.connect(db) as con:
        con.execute("alter table tasks add column block_reason text")
        con.execute("alter table tasks add column created_by text")
        con.execute("insert into tasks(id,title,status,created_at,created_by)"
                    " values('t_x','Guarded card','todo',100,'parent')")
    proc = tmp_path / "proc"
    proc.mkdir()
    monkeypatch.setenv("KANBAN_LIVE_PROC_ROOT", str(proc))
    return db, proc


def process(proc, pid, *argv):
    directory = proc / str(pid)
    directory.mkdir()
    (directory / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")


def park(db, status="todo", summary="Which interface should I implement?"):
    with sqlite3.connect(db) as con:
        con.execute("update tasks set status=?,current_run_id=1 where id='t_x'", (status,))
        con.execute("insert into task_runs(id,task_id,status,outcome,started_at,ended_at,summary)"
                    " values(1,'t_x','blocked','blocked',110,120,?)", (summary,))
        con.execute("insert into task_events(task_id,kind,payload,created_at,run_id)"
                    " values('t_x','dependency_wait','{}',120,1)")


def invoke(reader, db, command, *extra, code=0):
    before = db.read_bytes()
    result = subprocess.run([sys.executable, str(reader), command, "--board", "guards", *extra],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == code, (result.returncode, result.stdout, result.stderr)
    assert db.read_bytes() == before, "watcher changed the board"
    return json.loads(result.stdout)


def card(reader, db, command):
    data = invoke(reader, db, command, "--task", "t_x")
    return data if command == "snapshot" else data["cards"][0]


@pytest.mark.parametrize("command", ["snapshot", "report", "cards"])
def test_known_red_duplicate_workers(reader, guards, command):
    db, proc = guards
    process(proc, 401, "hermes", "-p", "builder", "chat", "-q", "work kanban task t_x")
    process(proc, 90, "hermes", "chat", "-q", "work", "kanban", "task", "t_x")
    before = {p: p.read_bytes() for p in proc.glob("*/cmdline")}
    assert card(reader, db, command).get("warnings", {}).get("duplicate_workers") == [90, 401]
    assert {p: p.read_bytes() for p in before} == before


@pytest.mark.parametrize("command", ["snapshot", "report", "cards"])
@pytest.mark.parametrize("count", [0, 1])
def test_single_or_no_worker_control(reader, guards, command, count):
    db, proc = guards
    if count:
        process(proc, 12, "hermes", "chat", "-q", "work kanban task t_x")
    process(proc, 13, "hermes", "chat", "-q", "work kanban task t_x_other")
    process(proc, 14, "python", "a script mentioning work kanban task t_x in prose")
    process(proc, 15, "hermes", "chat", "-q", "work kanban task t_y")
    process(proc, 16)  # kernel/empty cmdline is not a worker
    (proc / "self").mkdir()
    assert "duplicate_workers" not in card(reader, db, command).get("warnings", {})


@pytest.mark.parametrize("command", ["snapshot", "report", "cards"])
@pytest.mark.parametrize("failure", ["missing-root", "file-root", "unreadable-cmdline"])
def test_proc_read_failure_is_unknown(reader, guards, command, failure, monkeypatch):
    db, proc = guards
    if failure == "missing-root":
        monkeypatch.setenv("KANBAN_LIVE_PROC_ROOT", str(proc / "missing"))
    elif failure == "file-root":
        root = proc / "not-a-directory"
        root.write_text("not proc")
        monkeypatch.setenv("KANBAN_LIVE_PROC_ROOT", str(root))
    else:
        (proc / "42/cmdline").mkdir(parents=True)  # deterministic read error, even as root
    assert card(reader, db, command)["warnings"]["duplicate_workers"] == "UNKNOWN"


def test_disappeared_process_is_not_an_unreadable_proc(reader, guards):
    db, proc = guards
    (proc / "42").mkdir()  # vanished between enumerating PID and opening cmdline
    assert "duplicate_workers" not in card(reader, db, "snapshot")["warnings"]


@pytest.mark.parametrize("status", ["todo", "ready"])
def test_known_red_parked_on_parent(reader, guards, status):
    db, _ = guards
    summary = "Question for the parent: " + "Which interface? " * 40
    park(db, status, summary)
    result = card(reader, db, "snapshot")
    assert result.get("waiting_on") == "parent"
    assert result["waiting_text"] == summary  # not the 400-character run preview
    assert result["block_reason"] is None


@pytest.mark.parametrize("loop", [False, True])
@pytest.mark.parametrize("creator", [False, True])
def test_needs_reports_parent_question(reader, guards, loop, creator):
    db, _ = guards
    park(db)
    extra = ["--since", "0", "--max-seconds", "0", "--ready-stale", "0"]
    if loop:
        extra += ["--loop"]
    if creator:
        extra += ["--created-by", "parent"]
    data = invoke(reader, db, "needs", *extra)
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert (item["kind"], item["task"], item["text"]) == (
        "parent_question", "t_x", "Which interface should I implement?")
    assert invoke(reader, db, "needs", "--since", "0", "--max-seconds", "0",
                  "--ready-stale", "0", code=3)["items"] == []  # same cursor, no repeat


@pytest.mark.parametrize("status", ["todo", "ready"])
@pytest.mark.parametrize("reason,summary,expected", [
    pytest.param("Reason text", None, "Reason text", id="reason-null-summary"),
    pytest.param("Reason text", "", "Reason text", id="reason-empty-summary"),
    pytest.param("Reason text", "Run summary", "Reason text", id="reason-over-summary"),
    pytest.param("R" * 2001, "Run summary", "R" * 2000, id="reason-clipped"),
    pytest.param(None, "Run summary", "Run summary", id="missing-reason-fallback"),
    pytest.param("", "S" * 2001, "S" * 2000, id="empty-reason-fallback-clipped"),
    pytest.param(None, None, "", id="missing-reason-and-summary"),
    pytest.param("", "", "", id="empty-reason-and-summary"),
])
def test_needs_prefers_event_reason_then_clipped_summary(reader, guards, status, reason, summary, expected):
    db, _ = guards
    park(db, status, summary)
    with sqlite3.connect(db) as con:
        con.execute("update task_events set payload=?", (json.dumps({"reason": reason}),))
    data = invoke(reader, db, "needs", "--since", "0", "--created-by", "parent",
                  "--ready-stale", "0", "--max-seconds", "0")
    assert len(data["items"]) == 1
    item = data["items"][0]
    assert (item["kind"], item["task"], item["text"]) == ("parent_question", "t_x", expected)
    assert item["key"] == "parent:t_x" and item["at"] == 120


@pytest.mark.parametrize("state", ["plain-todo", "later-event", "later-run", "same-second-run",
                                   "running", "blocked", "review", "done", "scheduled", "triage"])
def test_non_parent_wait_controls(reader, guards, state):
    db, _ = guards
    if state != "plain-todo":
        park(db)
        with sqlite3.connect(db) as con:
            if state == "later-event":
                con.execute("insert into task_events(task_id,kind,created_at) values('t_x','promoted',121)")
            elif state in ("later-run", "same-second-run"):
                # Pointer deliberately stale: any later run invalidates the old wait.
                con.execute("insert into task_runs(id,task_id,status,started_at) values(2,'t_x','running',?)",
                            (121 if state == "later-run" else 120,))
            else:
                con.execute("update tasks set status=? where id='t_x'", (state,))
    result = card(reader, db, "snapshot")
    assert "waiting_on" not in result and "waiting_text" not in result
    out = invoke(reader, db, "needs", "--since", "0", "--max-seconds", "0", "--ready-stale", "0", code=3)
    assert out["items"] == []


def test_needs_keeps_since_and_creator_boundaries(reader, guards):
    db, _ = guards
    park(db)
    common = ["--max-seconds", "0", "--ready-stale", "0"]
    assert invoke(reader, db, "needs", "--since", "121", *common, code=3)["items"] == []
    assert invoke(reader, db, "needs", "--since", "0", "--created-by", "other", *common, code=3)["items"] == []
    out = invoke(reader, db, "needs", "--since", "121", "--loop", *common)
    assert out["items"][0]["text"] == "Which interface should I implement?"


def load_reader(reader):
    spec = importlib.util.spec_from_file_location("live_guards_reader", reader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_permission_denied_proc_is_unknown_and_never_signalled(reader, guards, monkeypatch):
    db, proc = guards
    process(proc, 42, "hermes", "chat", "-q", "work kanban task t_x")
    module = load_reader(reader)
    original = Path.read_bytes

    def denied(path):
        if path == proc / "42/cmdline":
            raise PermissionError("fake proc permission denied")
        return original(path)

    def no_signal(*args):
        pytest.fail("a read-only watcher must never signal a process")

    monkeypatch.setattr(Path, "read_bytes", denied)
    monkeypatch.setattr(os, "kill", no_signal)
    assert module.worker_processes() is None


def test_duplicate_scan_once_per_command(reader, guards, monkeypatch, capsys):
    db, proc = guards
    process(proc, 42, "hermes", "chat", "-q", "work kanban task t_x")
    module = load_reader(reader)
    original = module.worker_processes
    scans = []

    def counted():
        scans.append(True)
        return original()

    monkeypatch.setattr(module, "worker_processes", counted)
    with sqlite3.connect(db) as con:
        con.execute("insert into tasks(id,title,status,created_at) values('t_y','Other card','todo',100)")
    for command in ("snapshot", "cards", "report"):
        scans.clear()
        assert module.main([command, "--board", "guards", "--task", "t_x", "--task", "t_y"]) == 0
        assert len(scans) == 1
    capsys.readouterr()


@pytest.mark.parametrize("command", ["snapshot", "report"])
def test_process_warnings_preserve_run_warning_counts(reader, guards, command):
    db, proc = guards
    park(db)
    with sqlite3.connect(db) as con:
        con.execute("insert into task_events(task_id,kind,created_at,run_id)"
                    " values('t_x','protocol_violation',120,1)")
    # Repeated query arguments in one PID must not double-count that process.
    process(proc, 10, "hermes", "chat", "-q", "work kanban task t_x", "work kanban task t_x")
    process(proc, 20, "hermes", "chat", "-q", "work kanban task t_x")
    assert card(reader, db, command)["warnings"] == {
        "protocol_violation": 1, "duplicate_workers": [10, 20]}


def test_report_text_includes_duplicate_workers(reader, guards):
    db, proc = guards
    process(proc, 20, "hermes", "chat", "-q", "work kanban task t_x")
    process(proc, 10, "hermes", "chat", "-q", "work kanban task t_x")
    before = db.read_bytes()
    out = subprocess.run([sys.executable, str(reader), "report", "--board", "guards", "--format", "text"],
                         capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    assert "duplicate_workers=[10, 20]" in out.stdout and db.read_bytes() == before


def test_latest_event_must_be_dependency_wait(reader, guards):
    db, _ = guards
    park(db)
    with sqlite3.connect(db) as con:
        con.execute("insert into task_events(task_id,kind,created_at,run_id)"
                    " values('t_x','heartbeat',121,1)")
    assert "waiting_on" not in card(reader, db, "snapshot")
    assert invoke(reader, db, "needs", "--since", "0", "--ready-stale", "0", "--max-seconds", "0",
                  code=3)["items"] == []


@pytest.mark.parametrize("status", ["running", "blocked", "review", "done", "scheduled", "triage"])
def test_legacy_creator_questions_for_other_states_unchanged(reader, guards, status):
    db, _ = guards
    park(db, status)
    with sqlite3.connect(db) as con:
        con.execute("update task_events set payload=?", (json.dumps({"reason": "Legacy reason"}),))
    out = invoke(reader, db, "needs", "--since", "0", "--created-by", "parent",
                 "--ready-stale", "0", "--max-seconds", "0")
    assert [(i["kind"], i["task"], i["text"]) for i in out["items"]] == [
        ("parent_question", "t_x", "Legacy reason")]


def test_older_board_without_event_run_ids(reader, guards):
    db, _ = guards
    park(db)
    with sqlite3.connect(db) as con:
        con.execute("alter table task_events drop column run_id")
    assert card(reader, db, "snapshot")["waiting_text"] == "Which interface should I implement?"
    with sqlite3.connect(db) as con:
        con.execute("insert into task_runs(id,task_id,status,started_at) values(2,'t_x','running',121)")
    assert "waiting_on" not in card(reader, db, "snapshot")


def test_proc_directory_permission_denied_is_unknown(reader, guards, monkeypatch):
    _, proc = guards
    module = load_reader(reader)
    original = Path.iterdir

    def denied(path):
        if path == proc:
            raise PermissionError("fake proc root permission denied")
        return original(path)

    monkeypatch.setattr(Path, "iterdir", denied)
    assert module.worker_processes() is None
