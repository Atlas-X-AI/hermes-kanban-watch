"""Issue #9: pin drift on first sight, Discord channel
resolved per post from a session name, SUPERSEDED cards are not problems.

Synthetic boards, a fake loopback relay and a fake sender only: never a live board,
never a real relay, never a real Discord post.
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
from pathlib import Path
import sqlite3
import threading
import time

import pytest

from conftest import PLUGIN
from test_single_watcher import launch, launch_env  # noqa: F401  (fixture)
from test_watcher_discord import calls_at, discord_env, inbox_texts, tick  # noqa: F401  (fixture)


@pytest.fixture
def reader():
    spec = importlib.util.spec_from_file_location("board_watch", PLUGIN / "scripts/board_watch.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pin(board, task, model):
    with sqlite3.connect(board) as conn:
        if "model_override" not in {r[1] for r in conn.execute("pragma table_info(tasks)")}:
            conn.execute("alter table tasks add column model_override text")
        conn.execute("update tasks set model_override=? where id=?", (model, task))


def block(board, task, reason=None):
    with sqlite3.connect(board) as conn:
        if "block_reason" not in {r[1] for r in conn.execute("pragma table_info(tasks)")}:
            conn.execute("alter table tasks add column block_reason text")
        conn.execute("update tasks set status='blocked', block_reason=? where id=?", (reason, task))


def comment(board, task, body, at=None):
    with sqlite3.connect(board) as conn:
        conn.execute("create table if not exists task_comments"
                     "(id integer primary key, task_id text, body text, created_at int)")
        conn.execute("insert into task_comments(task_id,body,created_at) values(?,?,?)",
                     (task, body, int(time.time()) if at is None else at))


def event(board, task, kind, at):
    with sqlite3.connect(board) as conn:
        conn.execute("insert into task_events(task_id,kind,created_at,payload) values(?,?,?,'{}')",
                     (task, kind, at))


def kinds(data):
    return [p["kind"] for p in data["problems"]]


# (a) pin drift -------------------------------------------------------------------------

def test_pin_drift_is_a_problem_on_first_sight(board, reader, tmp_path):
    pin(board, "t_run", "pinned-model")
    data = reader.snapshot("demo", ["t_run"])
    assert kinds(data) == ["model_drift"]
    text = data["problems"][0]["text"]
    assert "pinned-model" in text and "worker-model" in text
    # The first wake after the start is the problem wake, not the 15-minute summary.
    wake = reader.wait("demo", ["t_run"], every=900, state_file=tmp_path / "cursor.json")
    assert wake["reason"] == "problem" and [p["kind"] for p in wake["new_problems"]] == ["model_drift"]
    assert not wake["summary_due"]
    assert "t_run model_drift:" in reader.summary(data)


def test_matching_model_or_unstamped_run_is_not_drift(board, reader):
    pin(board, "t_run", "worker-model")
    assert not reader.snapshot("demo", ["t_run"])["problems"]
    # Before the worker's first API call the run model is UNKNOWN, not drift.
    pin(board, "t_run", "pinned-model")
    with sqlite3.connect(board) as conn:
        conn.execute("update task_runs set model_used=null where id=1")
    assert not reader.snapshot("demo", ["t_run"])["problems"]
    with sqlite3.connect(board) as conn:
        conn.execute("update task_runs set model_used='other-model' where id=1")
    assert kinds(reader.snapshot("demo", ["t_run"])) == ["model_drift"]


def test_unpinned_card_with_recorded_fallback_is_drift(board, reader):
    assert not reader.snapshot("demo", ["t_run"])["problems"]  # no pin, no fallback: control
    with sqlite3.connect(board) as conn:
        conn.execute("update task_runs set fallback_from='seat-model' where id=1")
    data = reader.snapshot("demo", ["t_run"])
    assert kinds(data) == ["model_drift"] and "seat-model" in data["problems"][0]["text"]


def test_drift_reaches_the_starting_session_in_the_first_report(launch_env, board):
    _, inbox = launch_env
    pin(board, "t_run", "pinned-model")
    out = launch("--prepare-only", "--cards", "t_run", "--every", "900")
    assert out.returncode == 0, out.stderr
    first = tick(json.loads(out.stdout))
    assert first.returncode == 0 and "CONTINUE problem" in first.stdout
    rows = inbox_texts(inbox)
    assert len(rows) == 1 and rows[0].startswith("problem:")
    assert "model_drift" in rows[0] and "pinned-model" in rows[0] and "worker-model" in rows[0]


# (c) SUPERSEDED ------------------------------------------------------------------------

@pytest.mark.parametrize("reason", ["SUPERSEDED by t_run", "Superseded: replaced by t_run"])
def test_superseded_block_reason_is_not_a_problem(board, reader, reason):
    block(board, "t_wait", reason)
    data = reader.snapshot("demo", ["t_wait", "t_run"])
    assert not data["problems"] and data["superseded"] == ["t_wait"]
    text = reader.summary(data)
    assert "problems 0" in text and "superseded 1" in text and "waiting 0" in text


def test_superseded_comment_is_not_a_problem_until_work_resumes(board, reader):
    now = int(time.time())
    block(board, "t_wait", "Needs a decision")
    assert kinds(reader.snapshot("demo", ["t_wait"])) == ["blocked"]  # control
    comment(board, "t_wait", "Not superseded yet, still waiting", now - 5)
    assert kinds(reader.snapshot("demo", ["t_wait"])) == ["blocked"]  # prose is not the marker
    comment(board, "t_wait", "SUPERSEDED: P4b replaces this card", now - 4)
    assert not reader.snapshot("demo", ["t_wait"])["problems"]
    event(board, "t_wait", "claimed", now)  # resumed after the marker: watched again
    data = reader.snapshot("demo", ["t_wait"])
    assert data["superseded"] == [] and "blocked" in kinds(data)


# (b) session-name channel resolution -----------------------------------------------------

class FakeRelay:
    """Loopback stand-in for GET /discord/sessions/<name>/channel; replies are scripted."""

    def __init__(self, replies):
        self.replies, self.paths = list(replies), []
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                relay.paths.append(self.path)
                status, body = relay.replies.pop(0)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def relay(monkeypatch):
    made = []

    def start(*replies):
        fake = FakeRelay(replies)
        made.append(fake)
        monkeypatch.setenv("KANBAN_WATCH_DISCORD_RELAY", fake.url)
        return fake

    yield start
    for fake in made:
        fake.close()


def prepare_session(*extra):
    out = launch("--prepare-only", "--discord-session", "desk-room", *extra)
    assert out.returncode == 0, out.stderr
    receipt = json.loads(out.stdout)
    config = json.loads(Path(receipt["config"]).read_text())
    assert config["discord_session"] == "desk-room" and not config.get("discord_channel")
    return receipt, config


OLD, NEW = "111111111111111111", "222222222222222222"


def test_session_channel_is_resolved_for_every_post(discord_env, relay, monkeypatch):
    calls, inbox, _ = discord_env
    monkeypatch.setenv("FAKE_DISCORD_ANY_CHANNEL", "1")
    # The channel is re-created between the two posts: the second post follows it.
    fake = relay((200, {"channelId": OLD}), (200, {"channelId": NEW}))
    receipt, _ = prepare_session("--cards", "t_wait", "--every", "0.02")
    for _ in range(2):
        out = tick(receipt)
        assert out.returncode == 0 and "CONTINUE summary" in out.stdout, out.stderr
    assert fake.paths == ["/discord/sessions/desk-room/channel"] * 2
    assert [m["args"] for m in calls_at(calls)] == [["--channel", OLD], ["--channel", NEW]]
    assert inbox_texts(inbox) == []


@pytest.mark.parametrize("reply,kind", [
    ((404, {"error": "no channel"}), "http_404"),
    ((200, {"channelId": "not-a-channel"}), "bad_channel"),
    ((200, {"other": 1}), "bad_channel"),
    (None, "relay_unreachable"),
])
def test_resolver_failure_is_reported_not_dropped(discord_env, relay, monkeypatch, reply, kind):
    calls, inbox, _ = discord_env
    monkeypatch.setenv("FAKE_DISCORD_ANY_CHANNEL", "1")
    receipt, config = prepare_session("--cards", "t_wait", "--every", "0.02")
    if reply is not None:
        relay(reply)
    failed = tick(receipt)
    assert failed.returncode == 0 and "CONTINUE summary" in failed.stdout, failed.stderr
    assert not calls.exists()  # nothing posted anywhere, never a guessed channel
    rows = inbox_texts(inbox)
    assert len(rows) == 1 and rows[0].startswith("problem:")
    assert "desk-room" in rows[0] and kind in rows[0] and "not posted" in rows[0]
    assert "Kanban demo:" in rows[0]  # the unposted report itself reaches the session
    state = json.loads(Path(config["relay_state"]).read_text())
    assert "discord_failure" not in state and "pending" not in state
    # Nothing was sent, so the next post resolves again and goes through.
    relay((200, {"channelId": NEW}))
    again = tick(receipt)
    assert again.returncode == 0 and "CONTINUE summary" in again.stdout, again.stderr
    assert [m["args"] for m in calls_at(calls)] == [["--channel", NEW]]
    assert len(inbox_texts(inbox)) == 1


def test_non_loopback_relay_is_refused_and_reported(discord_env, monkeypatch):
    calls, inbox, _ = discord_env
    monkeypatch.setenv("KANBAN_WATCH_DISCORD_RELAY", "http://relay.example.com:3100")
    receipt, _ = prepare_session("--cards", "t_wait", "--every", "0.02")
    out = tick(receipt)
    assert out.returncode == 0 and not calls.exists()
    assert "relay_not_loopback" in inbox_texts(inbox)[0]


@pytest.mark.parametrize("args", [
    ("--prepare-only", "--discord-session", "desk-room", "--discord-channel", OLD),
    ("--prepare-only", "--discord-session", "../escape"),
    ("--prepare-only", "--discord-session", ""),
    ("--discord-session", "desk-room"),  # detached: same refusal as a fixed channel
])
def test_bad_session_channel_launch_is_refused(discord_env, args):
    calls, inbox, _ = discord_env
    out = launch("--cards", "t_wait", *args)
    assert out.returncode != 0
    assert not calls.exists() and inbox_texts(inbox) == []
