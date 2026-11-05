"""Board-only watcher contracts. All databases and cursors are disposable."""
import importlib.util
import json
import sqlite3
import subprocess
import sys
import time

import pytest

from conftest import PLUGIN


@pytest.fixture
def reader(monkeypatch):
    monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
    spec = importlib.util.spec_from_file_location("board_watch", PLUGIN / "scripts/board_watch.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def command(*args, **kwargs):
    return subprocess.run([sys.executable, str(PLUGIN / "scripts/board_watch.py"), *args],
                          capture_output=True, text=True, timeout=5, **kwargs)


def event(board, task, kind, at, payload=None):
    with sqlite3.connect(board) as conn:
        conn.execute("insert into task_events(task_id,kind,created_at,payload) values(?,?,?,?)",
                     (task, kind, at, json.dumps(payload or {})))


def test_snapshot_actual_run_age_and_read_only(board, reader):
    before = board.read_bytes()
    data = reader.snapshot("demo", now=int(time.time()))
    cards = {c["id"]: c for c in data["cards"]}
    assert data["read_status"] == "OK" and len(cards) == 3
    assert cards["t_run"]["model_used"] == "worker-model"
    assert cards["t_run"]["provider_used"] == "worker-provider"
    assert cards["t_wait"]["model_used"] is None
    assert cards["t_run"]["age_s"] >= 60
    assert cards["t_run"]["run"]["runtime_s"] >= 50
    assert not data["all_terminal"] and not data["problems"]
    assert board.read_bytes() == before


def test_snapshot_does_not_read_any_profiles(board, reader, monkeypatch):
    connect = sqlite3.connect

    def guarded(path, *args, **kwargs):
        assert "/profiles/" not in str(path)
        assert "mode=ro" in str(path)
        return connect(path, *args, **kwargs)

    monkeypatch.setattr(reader.sqlite3, "connect", guarded)
    assert reader.snapshot("demo", ["t_run"])["cards"][0]["id"] == "t_run"


@pytest.mark.parametrize("status,terminal", [("done", True), ("archived", True),
    ("blocked", False), ("review", False), ("scheduled", False)])
def test_only_finished_states_are_terminal(board, reader, status, terminal):
    with sqlite3.connect(board) as conn:
        conn.execute("update tasks set status=? where id='t_wait'", (status,))
    result = reader.snapshot("demo", ["t_wait"])
    assert result["all_terminal"] is terminal
    assert any(p["kind"] == "blocked" for p in result["problems"]) is (status == "blocked")


def test_ready_age_ignores_heartbeat_and_cap_noise(board, reader):
    now = int(time.time())
    event(board, "t_wait", "promoted", now - 601)
    event(board, "t_wait", "capped", now - 2)
    event(board, "t_wait", "heartbeat", now)
    result = reader.snapshot("demo", ["t_wait"], now=now)
    assert [p["kind"] for p in result["problems"]] == ["ready_no_worker"]
    event(board, "t_wait", "unblocked", now)
    assert not reader.snapshot("demo", ["t_wait"], now=now)["problems"]


@pytest.mark.parametrize("alerted", [False, True])
def test_live_cap_is_reported_not_a_problem(board, reader, alerted):
    now = int(time.time()) + 1000  # All synthetic events follow the fixture's creation.
    with sqlite3.connect(board) as conn:
        conn.execute("insert into tasks values('t_uncapped','Waiting','builder','ready',?,null,null,null,null,null)",
                     (now - 900,))
        conn.execute("insert into tasks values('t_holder','Holder','builder','running',?,null,null,null,null,null)",
                     (now - 100,))
    event(board, "t_wait", "promoted", now - 900)
    event(board, "t_wait", "capped", now - 840,
          {"scope": "group:builders", "assignee": "builder", "running": 2, "cap": 2})
    if alerted:
        event(board, "t_wait", "capped_alerted", now - 1)
    before = board.read_bytes()
    data = reader.snapshot("demo", ["t_wait", "t_uncapped"], now=now)
    cards = {card["id"]: card for card in data["cards"]}
    assert cards["t_wait"]["capped"] == {
        "scope": "group:builders", "assignee": "builder", "running": 2, "cap": 2,
        "waiting_minutes": 14, "alerted": alerted, "label": "capped group:builders 2/2 14m"}
    assert not cards["t_uncapped"].get("capped")
    assert data["capped"] == 1 and not data["all_terminal"]
    assert [(p["card"], p["kind"]) for p in data["problems"]] == [("t_uncapped", "ready_no_worker")]
    assert "capped 1" in reader.summary(data) and "problems 1" in reader.summary(data)
    assert board.read_bytes() == before


@pytest.mark.parametrize("later", ["promoted", "claimed", "spawn_failed"])
def test_old_cap_does_not_hide_an_uncapped_ready_problem(board, reader, later):
    now = int(time.time()) + 1000
    event(board, "t_wait", "capped", now - 900, {"running": 1, "cap": 1})
    event(board, "t_wait", later, now - 800)
    data = reader.snapshot("demo", ["t_wait"], now=now)
    assert data["capped"] == 0 and not data["cards"][0].get("capped")
    assert "ready_no_worker" in [p["kind"] for p in data["problems"]]


def test_live_cap_waits_until_summary_not_immediate_problem(board, reader, tmp_path):
    clock = Clock()
    with sqlite3.connect(board) as conn:
        conn.execute("update task_events set created_at=0 where task_id='t_wait'")
    event(board, "t_wait", "promoted", 0)
    event(board, "t_wait", "capped", 1, {"running": 1, "cap": 1})
    wake = reader.wait("demo", ["t_wait"], every=900, state_file=tmp_path / "capped-cursor",
                       clock=clock.time, sleep=clock.sleep)
    assert wake["reason"] == "summary" and clock.sleeps == [60] * 15
    assert not wake["new_problems"] and wake["snapshot"]["capped"] == 1


def test_live_cap_does_not_hide_spawn_failures(board, reader):
    now = int(time.time()) + 1000
    event(board, "t_wait", "spawn_failed", now - 900, {"reason": "cannot start"})
    event(board, "t_wait", "capped", now - 1, {"running": 1, "cap": 1})
    data = reader.snapshot("demo", ["t_wait"], now=now)
    assert data["capped"] == 1
    assert [p["kind"] for p in data["problems"]] == ["spawn_failed"]


@pytest.mark.parametrize("holders", [0, 1])
def test_abandoned_cap_cannot_hide_a_stalled_ready_card(board, reader, holders):
    now = int(time.time()) + 30000
    with sqlite3.connect(board) as conn:
        conn.execute("update tasks set status=? where id='t_run'", ("running" if holders else "done",))
    event(board, "t_wait", "promoted", now - 22000)
    event(board, "t_wait", "capped", now - 21600,
          {"assignee": "builder", "running": 2, "cap": 2})
    before = board.read_bytes()
    data = reader.snapshot("demo", ["t_wait"], now=now)
    assert data["capped"] == 0
    assert not data["cards"][0].get("capped")
    assert [p["kind"] for p in data["problems"]] == ["ready_no_worker"]
    assert board.read_bytes() == before


def test_unrelated_running_card_is_not_a_cap_holder(board, reader):
    now = int(time.time()) + 1000
    with sqlite3.connect(board) as conn:
        conn.execute("update tasks set assignee='other' where id='t_run'")
    event(board, "t_wait", "promoted", now - 900)
    event(board, "t_wait", "capped", now - 800,
          {"assignee": "builder", "running": 1, "cap": 1})
    data = reader.snapshot("demo", ["t_wait"], now=now)
    assert data["capped"] == 0
    assert [p["kind"] for p in data["problems"]] == ["ready_no_worker"]


def test_ready_worker_evidence_and_current_run_pointer(board, reader):
    now = int(time.time())
    event(board, "t_wait", "promoted", now - 700)
    with sqlite3.connect(board) as conn:
        conn.execute("update tasks set worker_pid=42 where id='t_wait'")
        conn.execute("insert into task_runs values(3,'t_run','done',null,'old','old',null,1,2,'old')")
    assert not reader.snapshot("demo", ["t_wait"], now=now)["problems"]
    assert reader.snapshot("demo", ["t_run"])["cards"][0]["model_used"] == "worker-model"


@pytest.mark.parametrize("kind", ["respawn_guarded", "spawn_failed"])
def test_current_spawn_problem_and_recovery(board, reader, kind):
    now = int(time.time())
    event(board, "t_wait", kind, now, {"reason": "cannot start"})
    result = reader.snapshot("demo", ["t_wait"])
    assert [p["kind"] for p in result["problems"]] == [kind]
    event(board, "t_wait", "spawned", now)
    assert not reader.snapshot("demo", ["t_wait"])["problems"]


def test_parent_question_and_resumed_control(board, reader):
    now = int(time.time())
    event(board, "t_wait", "dependency_wait", now, {"reason": "Which branch?", "kind": "dependency"})
    result = reader.snapshot("demo", ["t_wait"])
    assert result["problems"][0]["kind"] == "parent_question"
    assert "Which branch?" in result["problems"][0]["text"]
    event(board, "t_wait", "claimed", now)
    assert not reader.snapshot("demo", ["t_wait"])["problems"]


def test_explicit_parent_comment_not_prose_or_answered(board, reader):
    now = int(time.time())
    with sqlite3.connect(board) as conn:
        conn.execute("create table task_comments(id integer primary key,task_id text,body text,created_at int)")
        conn.execute("insert into task_comments values(1,'t_run','No ASK PARENT yet',?)", (now,))
    assert not reader.snapshot("demo", ["t_run"])["problems"]
    with sqlite3.connect(board) as conn:
        conn.execute("insert into task_comments values(2,'t_run','ASK PARENT: Which target?',?)", (now,))
    assert reader.snapshot("demo", ["t_run"])["problems"][0]["kind"] == "parent_question"
    with sqlite3.connect(board) as conn:
        conn.execute("insert into task_comments values(3,'t_run','ANSWER: use main',?)", (now,))
    assert not reader.snapshot("demo", ["t_run"])["problems"]


def test_run_cap_not_card_age(board, reader):
    now = int(time.time())
    with sqlite3.connect(board) as conn:
        conn.execute("alter table tasks add column max_runtime_seconds int")
        conn.execute("alter table task_runs add column max_runtime_seconds int")
        conn.execute("update tasks set max_runtime_seconds=99999 where id='t_run'")
        conn.execute("update task_runs set max_runtime_seconds=10 where id=1")
    data = reader.snapshot("demo", ["t_run"], now=now)
    assert data["problems"][0]["kind"] == "max_runtime"
    assert data["cards"][0]["run"]["max_runtime_seconds"] == 10


@pytest.mark.parametrize("args", [("--board", "../escape"),
    ("--board", "absent"), ("--board", "demo", "--cards", "t_missing"),
    ("--board", "demo", "--cards", "t_run;bad")])
def test_cli_refuses_bad_scope_without_completion(board, args):
    out = command("snapshot", *args)
    assert out.returncode != 0
    assert '"all_terminal": true' not in out.stdout


def test_corrupt_board_unknown_and_no_creation(board):
    board.write_bytes(b"not sqlite")
    out = command("snapshot", "--board", "demo")
    assert out.returncode != 0 and json.loads(out.stdout)["read_status"] == "UNKNOWN"


def test_summary_is_short_plain_and_honest(board, reader):
    data = reader.snapshot("demo")
    text = reader.summary(data)
    assert len(text.splitlines()) <= 12
    assert "done 1" in text and "running 1" in text and "waiting 1" in text
    assert "problems 0" in text and "rough time left UNKNOWN" in text
    out = command("summary", input=json.dumps(data))
    assert out.returncode == 0 and out.stdout.strip() == text


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def test_wait_summary_deadline_is_not_reset_by_problem(board, reader, tmp_path):
    clock = Clock()
    state = tmp_path / "cursor.json"
    result = reader.wait("demo", ["t_wait"], every=900, state_file=state,
                         clock=clock.time, sleep=clock.sleep)
    assert result["reason"] == "summary" and clock.sleeps == [60] * 15
    event(board, "t_wait", "spawn_failed", 1900)
    result = reader.wait("demo", ["t_wait"], every=900, state_file=state,
                         clock=clock.time, sleep=clock.sleep)
    assert result["reason"] == "problem" and result["new_problems"]
    assert json.loads(state.read_text())["next_summary_at"] == 2800
    clock.sleeps.clear()
    result = reader.wait("demo", ["t_wait"], every=900, state_file=state,
                         clock=clock.time, sleep=clock.sleep)
    assert result["reason"] == "summary" and clock.sleeps == [60] * 15


def test_wait_initial_problem_then_final_without_sleep(board, reader, tmp_path):
    state = tmp_path / "state.json"
    with sqlite3.connect(board) as conn:
        conn.execute("update tasks set status='blocked' where id='t_wait'")
    assert reader.wait("demo", ["t_wait"], state_file=state)["reason"] == "problem"
    with sqlite3.connect(board) as conn:
        conn.execute("update tasks set status='done' where id='t_wait'")
    assert reader.wait("demo", ["t_wait"], state_file=state)["reason"] == "final"


def test_wait_short_interval_and_time_limit(board, reader, tmp_path):
    out = command("wait", "--board", "demo", "--cards", "t_wait", "--every", "0.02",
                  "--state-file", str(tmp_path / "cursor"))
    assert out.returncode == 0 and json.loads(out.stdout)["reason"] == "summary"
    clock = Clock()
    result = reader.wait("demo", ["t_wait"], every=900, state_file=tmp_path / "other",
                         until=1010, clock=clock.time, sleep=clock.sleep)
    assert result["reason"] == "max_hours" and not result["snapshot"]["all_terminal"]
    assert clock.sleeps == [10]


def budget_problems(data):
    return [p for p in data["problems"] if p["kind"].startswith("budget_")]


@pytest.mark.parametrize("kind, lead", [
    ("budget_warning", "Nearly out of turns: 135/150 turns"),
    ("budget_exhausted", "Ran out of turns: 150/150 turns"),
])
def test_budget_event_is_an_immediate_problem(board, reader, kind, lead):
    used = 135 if kind == "budget_warning" else 150
    event(board, "t_run", kind, int(time.time()),
          {"iterations": used, "max_iterations": 150, "new_commits": 2, "last_activity": "patch src/app.py"})
    problems = budget_problems(reader.snapshot("demo", now=int(time.time())))
    assert [p["kind"] for p in problems] == [kind]
    assert problems[0]["card"] == "t_run"
    assert problems[0]["text"].startswith(lead)
    assert "2 new commits this run" in problems[0]["text"] and "last: patch src/app.py" in problems[0]["text"]


def test_quiet_checkpoint_is_not_a_problem(board, reader):
    event(board, "t_run", "budget_checkpoint", int(time.time()),
          {"iterations": 75, "max_iterations": 150, "new_commits": 0})
    assert budget_problems(reader.snapshot("demo", now=int(time.time()))) == []


def test_unknown_commit_count_stays_unknown(board, reader):
    event(board, "t_run", "budget_warning", int(time.time()),
          {"iterations": 135, "max_iterations": 150, "new_commits": None})
    assert "commits unknown" in budget_problems(reader.snapshot("demo", now=int(time.time())))[0]["text"]


def test_only_the_newest_budget_event_is_reported(board, reader):
    now = int(time.time())
    event(board, "t_run", "budget_warning", now - 60, {"iterations": 135, "max_iterations": 150})
    event(board, "t_run", "budget_exhausted", now - 30, {"iterations": 150, "max_iterations": 150})
    assert [p["kind"] for p in budget_problems(reader.snapshot("demo", now=now))] == ["budget_exhausted"]


def test_a_respawn_does_not_hide_an_exhausted_run(board, reader):
    now = int(time.time())
    event(board, "t_run", "budget_exhausted", now - 30, {"iterations": 150, "max_iterations": 150})
    event(board, "t_run", "claimed", now - 4)
    event(board, "t_run", "spawned", now - 4)
    assert [p["kind"] for p in budget_problems(reader.snapshot("demo", now=now))] == ["budget_exhausted"]


def test_a_later_budget_event_wakes_the_watch_again(board, reader, tmp_path):
    clock = Clock()
    state = tmp_path / "state.json"
    watch = lambda: reader.wait("demo", ["t_run"], every=900, state_file=state,
                                clock=clock.time, sleep=clock.sleep)
    event(board, "t_run", "budget_warning", 900, {"iterations": 135, "max_iterations": 150})
    assert [p["kind"] for p in watch()["new_problems"]] == ["budget_warning"]
    # The retry's own warning: the same kind, so only the event id can tell it is new.
    event(board, "t_run", "budget_warning", 950, {"iterations": 135, "max_iterations": 150})
    second = watch()
    assert second["reason"] == "problem"
    assert [p["kind"] for p in second["new_problems"]] == ["budget_warning"]


def test_a_finished_card_stops_reporting_its_budget(board, reader):
    now = int(time.time())
    event(board, "t_run", "budget_warning", now - 60, {"iterations": 135, "max_iterations": 150})
    with sqlite3.connect(board) as conn:
        conn.execute("update tasks set status='done' where id='t_run'")
    assert budget_problems(reader.snapshot("demo", now=now)) == []
