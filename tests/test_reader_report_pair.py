"""Report and independence checks use disposable SQLite boards, not profile pins."""
import json
import sqlite3
import subprocess
import sys

import pytest

from conftest import PLUGIN
from helpers import create_board


def live(*args):
    return subprocess.run([sys.executable, str(PLUGIN / "scripts/kanban_live.py"), *args],
                          capture_output=True, text=True, timeout=10)


@pytest.fixture
def wave(isolated_home):
    _, hermes = isolated_home
    db = create_board(hermes / "kanban/boards/wave/kanban.db")
    with sqlite3.connect(db) as con:
        con.executescript("""
            alter table tasks add column model_override text;
            alter table tasks add column provider_override text;
        """)
        con.executemany("insert into tasks(id,title,status,assignee,created_at,model_override,provider_override)"
                        " values(?,?,?,?,?,?,?)", [
            ("t_worker", "Worker title", "done", "builder", 100, "claude-pinned", "anthropic"),
            ("t_verifier", "Verifier title", "review", "reviewer", 101, "gpt-pinned", "openai"),
            ("t_queue", "Queued title", "ready", None, 200, None, None),
        ])
        con.executemany("insert into task_runs(id,task_id,status,model_used,provider_used,fallback_from,"
                        "started_at,ended_at) values(?,?,?,?,?,?,?,?)", [
            (1, "t_worker", "failed", "claude-old", "anthropic", "gpt-original", 100, 110),
            (2, "t_worker", "done", "gpt-actual", "openai", None, 120, 145),
            (3, "t_verifier", "done", "claude-actual", "anthropic", None, 130, 170),
        ])
        con.executemany("insert into task_events(task_id,kind,run_id,created_at) values(?,?,?,?)", [
            ("t_worker", "crashed", 1, 140),
            ("t_worker", "completion_warnings", 2, 145),
            ("t_worker", "protocol_violation", None, 141),
        ])
    return db


def test_report_all_cards_and_total_time_including_previous_fallback(wave):
    before = wave.read_bytes()
    out = live("report", "--board", "wave")
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    cards = {c["id"]: c for c in data["cards"]}
    assert data["read_status"] == "OK" and data["now"] > 0
    assert cards["t_worker"] == {
        "id": "t_worker", "title": "Worker title", "status": "done", "assignee": "builder",
        "model_used": "gpt-actual", "provider_used": "openai", "fallback_from": None,
        "runs": 2, "retried": True, "duration_s": 25,
        "warnings": {"completion_warnings": 1, "protocol_violation": 1},
    }
    assert cards["t_queue"]["model_used"] is None and cards["t_queue"]["duration_s"] is None
    assert cards["t_queue"]["runs"] == 0 and cards["t_queue"]["warnings"] == {}
    assert data["totals"] == {
        "by_status": {"done": 1, "ready": 1, "review": 1}, "run_seconds": 75,
        "fell_back": 1, "retried": 1,
    }
    assert wave.read_bytes() == before


def test_report_task_since_filters_and_missing(wave):
    out = live("report", "--board", "wave", "--since", "101")
    data = json.loads(out.stdout)
    assert out.returncode == 0
    assert [c["id"] for c in data["cards"]] == ["t_verifier", "t_queue"]
    assert data["totals"]["run_seconds"] == 40
    out = live("report", "--board", "wave", "--task", "t_worker", "--task", "t_worker",
               "--task", "t_missing")
    data = json.loads(out.stdout)
    assert out.returncode == 4 and data["missing"] == ["t_missing"]
    assert len(data["cards"]) == 1 and data["totals"]["run_seconds"] == 35
    out = live("report", "--board", "wave", "--since", "999")
    assert out.returncode == 0 and json.loads(out.stdout)["totals"]["by_status"] == {}


def test_report_aligned_text_keeps_actual_run_and_warnings(wave):
    out = live("report", "--board", "wave", "--format", "text")
    assert out.returncode == 0, out.stderr
    lines = out.stdout.splitlines()
    assert lines[0].startswith("ID ")
    assert "gpt-actual" in out.stdout and "completion_warnings=1" in out.stdout
    assert "run_seconds=75" in out.stdout and "fell_back=1" in out.stdout and "retried=1" in out.stdout
    assert "claude-pinned" not in out.stdout and "|" not in out.stdout
    # Rows have the same column starts even when the assignee/model are unknown.
    assert lines[0].index("STATUS") == lines[1].index("done") == lines[3].index("ready")


def test_report_running_duration_and_unknown_timing(wave):
    with sqlite3.connect(wave) as con:
        con.execute("insert into task_runs(id,task_id,status,started_at) values(4,'t_queue','running',1)")
        con.execute("insert into task_runs(id,task_id,status) values(5,'t_verifier','done')")
    out = live("report", "--board", "wave")
    data = json.loads(out.stdout)
    cards = {c["id"]: c for c in data["cards"]}
    assert out.returncode == 0
    assert cards["t_queue"]["duration_s"] == data["now"] - 1
    assert cards["t_verifier"]["duration_s"] is None
    assert data["totals"]["run_seconds"] == data["now"] - 1 + 75


@pytest.mark.parametrize("provider,code,family", [("openai", 1, "gpt"),
                                                   ("anthropic", 0, "claude")])
def test_verify_pair_actual_family_same_and_cross_controls(wave, provider, code, family):
    # Both pins say the opposite of what their runs actually used.
    with sqlite3.connect(wave) as con:
        con.execute("update task_runs set provider_used=?,model_used=? where id=3",
                    (provider, "gpt-review" if family == "gpt" else "claude-review"))
    before = wave.read_bytes()
    out = live("verify-pair", "--board", "wave", "--worker", "t_worker", "--verifier", "t_verifier")
    assert out.returncode == code, out.stderr
    data = json.loads(out.stdout)
    assert data["worker"]["model_used"] == "gpt-actual" and data["worker"]["family"] == "gpt"
    assert data["verifier"]["family"] == family
    assert data["different_families"] is (code == 0)
    assert wave.read_bytes() == before


def test_verify_pair_last_finished_run_not_older_or_unfinished(wave):
    with sqlite3.connect(wave) as con:
        con.execute("insert into task_runs(id,task_id,status,model_used,provider_used,started_at)"
                    " values(4,'t_worker','running','claude-new','anthropic',180)")
    out = live("verify-pair", "--board", "wave", "--worker", "t_worker", "--verifier", "t_verifier")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["worker"]["run_id"] == 2


@pytest.mark.parametrize("worker", ["t_queue", "t_absent"])
def test_verify_pair_no_finished_run(wave, worker):
    out = live("verify-pair", "--board", "wave", "--worker", worker, "--verifier", "t_verifier")
    assert out.returncode == 3, out.stderr
    assert json.loads(out.stdout)["different_families"] is None


def test_verify_pair_unknown_actual_provider_is_not_independence(wave):
    with sqlite3.connect(wave) as con:
        con.execute("update task_runs set provider_used='unknown-host' where id=2")
    out = live("verify-pair", "--board", "wave", "--worker", "t_worker", "--verifier", "t_verifier")
    assert out.returncode == 1
    data = json.loads(out.stdout)
    assert data["different_families"] is None and "error" in data


def test_verify_pair_different_hosts_can_be_the_same_family(wave, tmp_path):
    families = tmp_path / "families.json"
    families.write_text(json.dumps({"providers": {"openai": "gpt", "other-host": "gpt"}}))
    with sqlite3.connect(wave) as con:
        con.execute("update task_runs set provider_used='other-host',model_used='gpt-review' where id=3")
    out = live("verify-pair", "--board", "wave", "--worker", "t_worker", "--verifier", "t_verifier",
               "--families", str(families))
    assert out.returncode == 1
    assert json.loads(out.stdout)["different_families"] is False


@pytest.mark.parametrize("field", ["model_used", "provider_used"])
def test_verify_pair_missing_actual_metadata_does_not_fall_back_to_pin(wave, field):
    with sqlite3.connect(wave) as con:
        con.execute(f"update task_runs set {field}=NULL where id=2")
    out = live("verify-pair", "--board", "wave", "--worker", "t_worker", "--verifier", "t_verifier")
    assert out.returncode == 1
    data = json.loads(out.stdout)
    assert data["different_families"] is None and "error" in data


def test_verify_pair_family_routes_precedence(wave, isolated_home, tmp_path, monkeypatch):
    home, _ = isolated_home
    local = home / ".config/hermes-kanban-watch/routes.yaml"
    local.parent.mkdir(parents=True)
    # Only the providers mapping is needed to classify actual runs.
    local.write_text("providers:\n  openai: shared\n  anthropic: shared\n")
    args = ("verify-pair", "--board", "wave", "--worker", "t_worker", "--verifier", "t_verifier")
    assert live(*args).returncode == 1
    env_file = tmp_path / "env.json"
    env_file.write_text(json.dumps({"providers": {"openai": "one", "anthropic": "two"}}))
    monkeypatch.setenv("KANBAN_WATCH_ROUTES", str(env_file))
    assert live(*args).returncode == 0
    assert live(*args, "--families", str(local)).returncode == 1
    assert live(*args, "--families", str(tmp_path / "missing")).returncode == 1
    monkeypatch.setenv("KANBAN_WATCH_ROUTES", "")
    assert live(*args).returncode == 1


@pytest.mark.parametrize("payload", ["{}", '{"providers": []}', '{"providers":{"openai":42}}',
                                     "providers:\n  openai: one\n  openai: two\n"])
def test_verify_pair_invalid_families_fail_closed(wave, tmp_path, payload):
    routes = tmp_path / "families.yaml"
    routes.write_text(payload)
    out = live("verify-pair", "--board", "wave", "--worker", "t_worker", "--verifier", "t_verifier",
               "--families", str(routes))
    assert out.returncode == 1 and "Traceback" not in out.stderr


@pytest.mark.parametrize("command,extra", [("report", []), ("verify-pair", ["--worker", "t_worker", "--verifier", "t_verifier"])])
@pytest.mark.parametrize("corrupt", [False, True])
def test_new_readers_unreadable_board(wave, corrupt, command, extra):
    if corrupt:
        wave.write_bytes(b"not sqlite")
    else:
        wave.unlink()
    out = live(command, "--board", "wave", *extra)
    assert out.returncode == 5
    data = json.loads(out.stdout)
    assert data["read_status"] == "UNKNOWN" and "error" in data and "cards" not in data
