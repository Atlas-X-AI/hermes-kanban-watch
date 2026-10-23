import json
import sqlite3
import subprocess
import sys
import time

import pytest

from conftest import PLUGIN


def live(*args):
    return subprocess.run([sys.executable, str(PLUGIN / "scripts/kanban_live.py"), *args],
                          capture_output=True, text=True, timeout=10)


def test_cards_graph_and_read_only(board):
    before = board.read_bytes()
    out = live("cards", "--board", "demo")
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["read_status"] == "OK" and data["now"] > 0 and data["count"] == 2
    cards = {c["id"]: c for c in data["cards"]}
    assert cards["t_wait"]["parents"] == ["t_run"]
    assert cards["t_wait"]["creator"] == "t_run"
    assert cards["t_run"]["model"] == "worker-model"
    assert board.read_bytes() == before


def test_snapshot_current_run_warnings_and_missing(board):
    with sqlite3.connect(board) as con:
        con.executemany("insert into task_events(task_id,kind,run_id,created_at) values(?,?,?,?)", [
            ("t_run", "model_fallback", 1, 1), ("t_run", "model_fallback", 1, 2),
            ("t_run", "crashed", 99, time.time()),
        ])
    out = live("snapshot", "--board", "demo", "--task", "t_run", "--task", "t_missing")
    assert out.returncode == 4
    card, missing = map(json.loads, out.stdout.splitlines())
    assert card["run"]["model_used"] == "worker-model"
    assert card["warnings"] == {"model_fallback": 2}
    assert missing["status"] == "NOT_FOUND" and missing["read_status"] == "OK"


@pytest.mark.parametrize("task,code,started", [("t_run", 0, ["t_run"]), ("t_wait", 3, []), ("t_missing", 4, [])])
def test_wait_any_exit_codes(board, task, code, started):
    out = live("wait-any", "--board", "demo", "--task", task, "--max-seconds", "0")
    assert out.returncode == code, out.stderr
    data = json.loads(out.stdout)
    assert data["read_status"] == "OK" and data["started"] == started
    assert bool(data.get("timeout")) == (code == 3)
    assert data["missing"] == ([task] if code == 4 else [])


def test_wait_any_start_survives_missing_and_not_starting(board):
    out = live("wait-any", "--board", "demo", "--task", "t_run", "--task", "t_absent", "--max-seconds", "0")
    assert out.returncode == 0
    assert json.loads(out.stdout)["missing"] == ["t_absent"]
    # The latest non-heartbeat event, not card creation, sets the idle age.
    # Give it a known age rather than depending on crossing a wall-clock second.
    with sqlite3.connect(board) as con:
        con.execute("update task_events set created_at=? where task_id='t_wait'", (int(time.time()) - 60,))
    out = live("wait-any", "--board", "demo", "--task", "t_wait", "--ready-stale", "1", "--max-seconds", "0")
    assert out.returncode == 6
    assert json.loads(out.stdout)["not_starting"] == ["t_wait"]


def test_cards_stale_filter_explicit_override(board):
    with sqlite3.connect(board) as con:
        con.execute("update tasks set created_at=1 where id='t_wait'")
        con.execute("delete from task_events")
    out = json.loads(live("cards", "--board", "demo").stdout)
    assert out["stale_skipped"] == ["t_wait"]
    explicit = json.loads(live("cards", "--board", "demo", "--task", "t_wait").stdout)
    assert explicit["cards"][0]["id"] == "t_wait" and not explicit["stale_skipped"]


def test_events_children_resumes_and_final(board):
    out = live("events", "--board", "demo", "--watch", "t_run=running", "--since", "0", "--max-seconds", "0")
    assert out.returncode == 0
    data = json.loads(out.stdout)
    assert data["count_new"] == 1 and data["new"][0]["id"] == "t_wait"
    out = live("events", "--board", "demo", "--watch", "t_run=blocked", "--known", "t_wait", "--since", "0", "--max-seconds", "0")
    assert out.returncode == 0
    assert json.loads(out.stdout)["changed"][0]["status"] == "running"
    out = live("events", "--board", "demo", "--watch", "t_done=running", "--since", "0", "--max-seconds", "0")
    assert out.returncode == 2 and json.loads(out.stdout)["all_finished"]


@pytest.mark.parametrize("compact", [False, True])
def test_follow_real_session_and_cursor(board, compact):
    mode = ["--compact"] if compact else []
    args = ["follow", "--board", "demo", "--task", "t_run", "--max-seconds", "0", *mode]
    first = live(*args)
    assert first.returncode == 3 and "Building now" in first.stdout
    again = live(*args)
    assert again.returncode == 3 and "Building now" not in again.stdout
    final = live("follow", "--board", "demo", "--task", "t_done", "--max-seconds", "0", *mode)
    assert final.returncode == 0 and "FINAL t_done [done]" in final.stdout
    assert "Finished now" in final.stdout


@pytest.mark.parametrize("command,extra", [("cards", []), ("snapshot", ["--task", "t_run"]),
    ("wait-any", ["--task", "t_run", "--max-seconds", "0"]),
    ("events", ["--watch", "t_run=running", "--since", "0", "--max-seconds", "0"])])
def test_unreadable_board_unknown(board, command, extra):
    board.write_bytes(b"not a database")
    out = live(command, "--board", "demo", *extra)
    assert out.returncode == 5
    data = json.loads(out.stdout)
    assert data["read_status"] == "UNKNOWN" and "error" in data
    assert "cards" not in data and "now" not in data


def test_optional_wave_snapshot(board):
    out = subprocess.run([sys.executable, str(PLUGIN / "scripts/wave_status.py"),
                          "--db", str(board), "--task", "t_run", "--task", "t_done"],
                         capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["counts"] == {"done": 1, "running": 1}
    assert not data["all_done"] and data["delivery"] == "NOT_ASSESSED"


@pytest.mark.parametrize("body,expected", [
    ("ASK OPERATOR: Which target should I use?", True),
    ("No ASK OPERATOR yet; continuing work.", False),
])
def test_needs_explicit_human_ask_and_control(board, body, expected):
    with sqlite3.connect(board) as con:
        con.execute("create table task_comments(id integer primary key, task_id text, body text, created_at int)")
        con.execute("insert into task_comments values(1, 't_run', ?, ?)", (body, int(time.time())))
    before = board.read_bytes()
    out = live("needs", "--board", "demo", "--since", "0", "--max-seconds", "0", "--ready-stale", "0")
    assert out.returncode == (0 if expected else 3), out.stderr
    data = json.loads(out.stdout)
    assert [(item["kind"], item["task"], item["text"]) for item in data["items"]] == (
        [("ask_operator", "t_run", body)] if expected else [])
    assert board.read_bytes() == before
