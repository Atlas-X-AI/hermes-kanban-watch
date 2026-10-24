"""Follow progress from disposable boards, logs and session stores only.

KANBAN_LIVE_TEST_REF exports a baseline reader into tmp_path for the known-red gate.
The workflow checks cover its prompt contract, not a live model's compliance.
"""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from conftest import PLUGIN, ROOT
from test_workflow import module_copy


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
def progress_card(board, isolated_home):
    _, hermes = isolated_home
    store = hermes / "profiles/builder/state.db"
    with sqlite3.connect(board) as con:
        con.execute("update task_runs set provider_used='openai-codex' where id=1")
    with sqlite3.connect(store) as con:
        con.execute("delete from messages where session_id='s1'")
    log = board.parent / "logs/t_run.log"
    log.parent.mkdir()
    log.write_text("worker started\n")
    return board, store, log


def follow(reader, fixture, *extra, board_name="demo", compact=True):
    db, store, log = fixture
    before = {path: path.read_bytes() for path in (db, store, log) if path.exists()}
    result = subprocess.run([sys.executable, str(reader), "follow", "--board", board_name,
                             "--task", "t_run", "--cursor-key", "progress-test",
                             "--max-seconds", "0", "--quiet-warn", "0",
                             *(["--compact"] if compact else ["--verbose"]), *extra],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 3, (result.stdout, result.stderr)
    assert all(path.read_bytes() == content for path, content in before.items()), "follow wrote input data"
    return result.stdout


@pytest.mark.parametrize("compact", [False, True])
def test_known_red_codex_growing_log(reader, progress_card, compact):
    first = follow(reader, progress_card, compact=compact)
    log = progress_card[2]
    for _ in range(2):
        log.write_text(log.read_text() + "tool completed\n")
        again = follow(reader, progress_card, compact=compact)
        assert "progress advanced" in again
        assert "worker messages unavailable" not in again
        assert "no new worker messages" not in again
    assert "worker messages unavailable; using card log" in first
    assert "no new worker messages" not in first


def test_flat_log_and_heartbeat_are_flaggable(reader, progress_card):
    assert "progress baseline" in follow(reader, progress_card)
    for _ in range(2):
        out = follow(reader, progress_card)
        assert "progress flat" in out
        assert "no new worker messages" not in out  # unavailable is not repeated quiet output


def test_codex_heartbeat_with_flat_log_is_flagged(reader, progress_card):
    """A heartbeat is never progress: a wedged worker keeps heartbeating."""
    follow(reader, progress_card)
    db, _, _ = progress_card
    for _ in range(2):
        with sqlite3.connect(db) as con:
            con.execute("update tasks set last_heartbeat_at=last_heartbeat_at+1 where id='t_run'")
        out = follow(reader, progress_card)
        assert "progress flat" in out and "progress advanced" not in out


def test_mtime_change_counts_even_without_size_growth(reader, progress_card):
    follow(reader, progress_card)
    log = progress_card[2]
    stamp = log.stat().st_mtime_ns + 1_000_000_000
    os.utime(log, ns=(stamp, stamp))
    assert "progress advanced" in follow(reader, progress_card)


def test_default_board_log_location(reader, progress_card, isolated_home):
    _, hermes = isolated_home
    db, store, old_log = progress_card
    default_db = hermes / "kanban.db"
    default_db.write_bytes(db.read_bytes())
    log = hermes / "kanban/logs/t_run.log"
    log.parent.mkdir(parents=True)
    log.write_text("default board worker\n")
    fixture = default_db, store, log
    follow(reader, fixture, board_name="default")
    log.write_text(log.read_text() + "another tool\n")
    assert "progress advanced" in follow(reader, fixture, board_name="default")
    # The named-board log did not change; the reader must not have used it.
    assert old_log.read_text() == "worker started\n"


def test_anthropic_messages_and_quiet_behaviour(reader, progress_card):
    db, store, _ = progress_card
    with sqlite3.connect(db) as con:
        con.execute("update task_runs set provider_used='anthropic' where id=1")
    with sqlite3.connect(store) as con:
        con.execute("insert into messages(session_id,role,content,timestamp)"
                    " values('s1','assistant','Building via tools',1)")
    first = follow(reader, progress_card)
    assert "Building via tools" in first and "worker messages unavailable" not in first
    quiet = follow(reader, progress_card)
    assert "Building via tools" not in quiet
    assert "no new worker messages" in quiet and "progress flat" in quiet
    with sqlite3.connect(store) as con:
        con.execute("insert into messages(session_id,role,content,timestamp)"
                    " values('s1','assistant','Next step complete',2)")
    active = follow(reader, progress_card)
    assert "Next step complete" in active and "worker messages unavailable" not in active


def test_anthropic_message_silence_with_heartbeat_is_quiet(reader, progress_card):
    """origin/main's rule: two quiet calls flag a running card whatever the heartbeat does."""
    db, store, _ = progress_card
    with sqlite3.connect(db) as con:
        con.execute("update task_runs set provider_used='anthropic' where id=1")
    with sqlite3.connect(store) as con:
        con.execute("insert into messages(session_id,role,content,timestamp)"
                    " values('s1','assistant','Building via tools',1)")
    follow(reader, progress_card)
    for _ in range(2):
        with sqlite3.connect(db) as con:
            con.execute("update tasks set last_heartbeat_at=last_heartbeat_at+1 where id='t_run'")
        quiet = follow(reader, progress_card)
        assert "no new worker messages" in quiet
        assert "progress advanced" not in quiet


def test_progress_resets_on_new_run_and_cursor_key(reader, progress_card):
    follow(reader, progress_card)
    assert "progress flat" in follow(reader, progress_card)
    assert "progress baseline" in follow(reader, progress_card, "--cursor-key", "other")
    with sqlite3.connect(progress_card[0]) as con:
        con.execute("insert into task_runs(id,task_id,status,started_at,provider_used)"
                    " values(3,'t_run','running',200,'openai-codex')")
        con.execute("update tasks set current_run_id=3,started_at=200,last_heartbeat_at=200 where id='t_run'")
    assert "progress baseline" in follow(reader, progress_card)


def test_missing_log_is_unknown_not_flat(reader, progress_card):
    progress_card[2].unlink()
    follow(reader, progress_card)
    assert "progress unknown" in follow(reader, progress_card)


def test_missing_heartbeat_does_not_change_log_decision(reader, progress_card):
    """The heartbeat is not a progress input, so its absence is neither unknown nor progress."""
    with sqlite3.connect(progress_card[0]) as con:
        con.execute("update tasks set last_heartbeat_at=null where id='t_run'")
    follow(reader, progress_card)
    assert "progress flat" in follow(reader, progress_card)


@pytest.mark.parametrize("cap", [128, 200, 1500])
def test_progress_control_survives_output_cap(reader, progress_card, cap):
    first = follow(reader, progress_card, "--max-bytes", str(cap))
    assert "worker messages unavailable; using card log" in first
    assert "still running" in first and len(first.encode()) <= cap
    out = follow(reader, progress_card, "--max-bytes", str(cap))
    assert len(out.encode()) <= cap
    assert "progress flat" in out and "still running" in out


def test_progress_sample_records_run_log_and_heartbeat(reader, progress_card, isolated_home):
    follow(reader, progress_card)
    home, _ = isolated_home
    state = json.loads((home / ".cache/hermes-kanban-watch/live/demo-t_run-progress-test.progress").read_text())
    log = progress_card[2].stat()
    assert state["run_id"] == 1
    assert state["log_size"] == log.st_size and state["log_mtime_ns"] == log.st_mtime_ns
    with sqlite3.connect(progress_card[0]) as con:
        heartbeat = con.execute("select last_heartbeat_at from tasks where id='t_run'").fetchone()[0]
    assert state["heartbeat"] == heartbeat


def test_corrupt_progress_cache_rebaselines(reader, progress_card, isolated_home):
    follow(reader, progress_card)
    home, _ = isolated_home
    cache = home / ".cache/hermes-kanban-watch/live/demo-t_run-progress-test.progress"
    cache.write_text("not JSON")
    assert "progress baseline" in follow(reader, progress_card)


@pytest.mark.parametrize("signal", ["log", "heartbeat"])
def test_progress_during_follow_call(reader, progress_card, monkeypatch, capsys, signal):
    spec = importlib.util.spec_from_file_location("follow_progress_reader", reader)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clock = [100000]

    def tick(seconds):
        clock[0] += seconds
        if signal == "log":
            log = progress_card[2]
            log.write_text(log.read_text() + "tool completed during follow\n")
        else:
            with sqlite3.connect(progress_card[0]) as con:
                con.execute("update tasks set last_heartbeat_at=last_heartbeat_at+1 where id='t_run'")

    monkeypatch.setattr(module, "time", SimpleNamespace(time=lambda: clock[0], sleep=tick,
                                                      strftime=lambda *args: "00:00:00", localtime=lambda _: None))
    assert module.main(["follow", "--board", "demo", "--task", "t_run", "--compact",
                        "--max-seconds", "2", "--interval", "1", "--quiet-warn", "0"]) == 3
    out = capsys.readouterr().out
    # Only log growth is progress; a heartbeat during the call is not.
    assert ("progress advanced" in out) == (signal == "log")
    assert "no new worker messages" not in out


def test_previous_run_heartbeat_is_not_progress(reader, progress_card):
    with sqlite3.connect(progress_card[0]) as con:
        con.execute("update tasks set last_heartbeat_at=1 where id='t_run'")
    follow(reader, progress_card)
    out = follow(reader, progress_card)
    assert "progress flat" in out and "progress advanced" not in out


@pytest.mark.parametrize("scenario", ["progress-growing", "progress-flat", "progress-heartbeat", "progress-anthropic",
                                      "progress-anthropic-quiet"])
def test_workflow_progress_prompt_contract(tmp_path, scenario):
    module_copy(tmp_path)
    harness = tmp_path / "watch.mjs"
    harness.write_text((ROOT / "tests/watch_scenarios.mjs").read_text())
    out = subprocess.run(["node", str(harness), scenario, "null"],
                         capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)["result"]
    assert result["read_status"] == "OK"
    assert result["cards"][0]["odd"] == ("possibly wedged" if scenario in (
        "progress-flat", "progress-heartbeat", "progress-anthropic-quiet") else "")
