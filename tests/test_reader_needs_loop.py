"""Persistent needs polling preserves its start boundary and bootstraps current waits."""
import importlib.util
import json
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from conftest import PLUGIN
from helpers import create_board


def live(*args):
    return subprocess.run([sys.executable, str(PLUGIN / "scripts/kanban_live.py"), *args],
                          capture_output=True, text=True, timeout=10)


@pytest.fixture
def waiting(isolated_home):
    _, hermes = isolated_home
    db = create_board(hermes / "kanban/boards/waits/kanban.db")
    old = int(time.time()) - 7200
    with sqlite3.connect(db) as con:
        con.execute("alter table tasks add column created_by text")
        con.executemany("insert into tasks(id,title,status,created_by,created_at) values(?,?,?,?,?)", [
            ("t_wait", "Needs parent answer", "blocked", "parent-session", old),
            ("t_resumed", "Already resumed", "running", "parent-session", old),
            ("t_other", "Another creator", "blocked", "other-session", old),
        ])
        con.executemany("insert into task_events(task_id,kind,payload,created_at) values(?,?,?,?)", [
            ("t_wait", "blocked", '{"reason":"Which interface should I implement?"}', old),
            ("t_resumed", "blocked", '{"reason":"Old question"}', old),
            ("t_resumed", "claimed", '{}', old + 1),
            ("t_other", "blocked", '{"reason":"Another question"}', old),
        ])
    return db


def test_needs_loop_initial_wait_backlog_and_legacy_control(waiting):
    args = ("needs", "--board", "waits", "--since", str(int(time.time())), "--created-by", "parent-session",
            "--max-seconds", "0", "--ready-stale", "0")
    before = waiting.read_bytes()
    legacy = live(*args)
    assert legacy.returncode == 3 and json.loads(legacy.stdout)["items"] == []
    loop = live(*args, "--loop")
    assert loop.returncode == 0, loop.stderr
    items = json.loads(loop.stdout)["items"]
    assert [(i["kind"], i["task"]) for i in items] == [("parent_question", "t_wait")]
    assert items[0]["text"] == "Which interface should I implement?"
    assert waiting.read_bytes() == before


def test_needs_loop_since_defaults_only_in_loop(waiting):
    out = live("needs", "--board", "waits", "--loop", "--created-by", "parent-session",
               "--ready-stale", "0", "--max-seconds", "0")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["items"][0]["task"] == "t_wait"
    assert live("needs", "--board", "waits", "--max-seconds", "0").returncode == 2


def test_needs_loop_only_bootstraps_newest_standing_question(waiting):
    with sqlite3.connect(waiting) as con:
        con.execute("insert into task_events(task_id,kind,payload,created_at) values(?,?,?,?)",
                    ("t_wait", "dependency_wait", '{"reason":"The replacement question"}', int(time.time()) - 60))
    out = live("needs", "--board", "waits", "--loop", "--created-by", "parent-session",
               "--ready-stale", "0", "--max-seconds", "0")
    assert out.returncode == 0, out.stderr
    items = json.loads(out.stdout)["items"]
    assert len(items) == 1 and items[0]["text"] == "The replacement question"


def reader_module():
    spec = importlib.util.spec_from_file_location("needs_loop_reader", PLUGIN / "scripts/kanban_live.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("explicit", [None, 50])
def test_needs_loop_retries_timeouts_without_moving_since(waiting, monkeypatch, capsys, explicit):
    reader = reader_module()
    clock = [100]
    calls = []
    sleeps = []

    def sleep(interval):
        sleeps.append(interval)
        clock[0] += 10

    def items(board, since, *args, **kwargs):
        calls.append((since, kwargs.get("include_waiting_before", False)))
        if len(calls) < 3:
            return []
        # This event landed after arming but before the most recent timeout.
        assert since <= 105
        return [{"key": "parent:t_new", "kind": "parent_question", "task": "t_new",
                 "at": 105, "status": "blocked", "text": "New question"}]

    monkeypatch.setattr(reader, "time", SimpleNamespace(time=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(reader, "needs_items", items)
    args = ["needs", "--board", "waits", "--loop", "--max-seconds", "0", "--interval", "1"]
    if explicit is not None:
        args.extend(["--since", str(explicit)])
    assert reader.main(args) == 0
    assert calls == [(explicit if explicit is not None else 100, True),
                     (explicit if explicit is not None else 100, False),
                     (explicit if explicit is not None else 100, False)]
    assert sleeps == [1, 1]
    assert json.loads(capsys.readouterr().out)["items"][0]["task"] == "t_new"


def test_needs_loop_does_not_retry_observation_errors(waiting, monkeypatch, capsys):
    reader = reader_module()
    calls = []

    def fail(*args, **kwargs):
        calls.append(True)
        raise sqlite3.OperationalError("fixture read failure")

    monkeypatch.setattr(reader, "needs_items", fail)
    assert reader.main(["needs", "--board", "waits", "--loop", "--max-seconds", "0"]) == 5
    assert len(calls) == 1 and "fixture read failure" in capsys.readouterr().out


@pytest.mark.parametrize("extra", [["--interval", "0"], ["--max-seconds", "-1"]])
def test_needs_loop_rejects_busy_poll_arguments(waiting, extra):
    out = live("needs", "--board", "waits", "--loop", *extra)
    assert out.returncode == 2
