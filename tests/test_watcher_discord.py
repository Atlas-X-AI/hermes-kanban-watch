"""Direct delivery proofs: fake sender, temporary board and temporary inbox only."""
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

import pytest

from conftest import PLUGIN
from test_single_watcher import launch, launch_env


CHANNEL = "123456789012345678"
MARKERS = "🟢🟣🟡🟠🔴"


@pytest.fixture
def discord_env(launch_env, tmp_path, monkeypatch):
    sender = tmp_path / "bin/send-hayden.sh"
    sender.write_text(f"#!{sys.executable}\n" + '''import json, os, re, subprocess, sys
from pathlib import Path
draft = Path(sys.argv[1])
text = draft.read_text()
lines = text.splitlines()
assert 3 <= len(lines) <= 8
assert lines[-1] == '-# Source: desk'
assert all(line[0] in '🟢🟣🟡🟠🔴' for line in lines[:-1])
assert all(sum(line.count(mark) for mark in '🟢🟣🟡🟠🔴') == 1 for line in lines[:-1])
assert not re.search(r'/|[a-f0-9]{40}|feat/|t_wait|t_run|t_done|@everyone', text)
# Optional real format-lint, its own --check mode, never the real sender.
lint_dir = os.environ.get('KANBAN_WATCH_FORMAT_LINT_DIR')
if lint_dir:
    result = subprocess.run([sys.executable, '-m', 'astra_context_layer', 'format-lint',
                             '--check', '--text-file', str(draft)], cwd=lint_dir,
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
if sys.argv[2:] == ['--lint-only']:
    print('lint PASS')
    sys.exit(0)
assert sys.argv[2:] == ['--channel', '123456789012345678']
with Path(os.environ['DISCORD_CALLS']).open('a') as f:
    f.write(json.dumps({'text': text, 'args': sys.argv[2:]}) + '\\n')
mode = os.environ.get('FAKE_DISCORD_MODE', 'ok')
if mode == 'missing-ok':
    print('PASS but missing final receipt')
    sys.exit(0)
if mode == 'source-identity-refused':
    print('source identity refused: PRIVATE-SENDER-DIAGNOSTIC', file=sys.stderr)
    sys.exit(1)
if mode != 'ok':
    print('UNTRUSTED sender output must not become a delivery claim')
    print('ok 987654321098765432 reactions=0/0')  # Even a success-looking line cannot override a non-zero exit.
    sys.exit(int(mode))
print('PASS fake send, no network')
print('ok 987654321098765432 reactions=0/0')
''')
    sender.chmod(0o755)
    calls = tmp_path / "discord-calls"
    monkeypatch.setenv("KANBAN_WATCH_DISCORD_SENDER", str(sender))
    monkeypatch.setenv("DISCORD_CALLS", str(calls))
    return calls, launch_env[1], sender


def prepare(*extra):
    out = launch("--prepare-only", "--discord-channel", CHANNEL, *extra)
    assert out.returncode == 0, out.stderr
    receipt = json.loads(out.stdout)
    config = json.loads(Path(receipt["config"]).read_text())
    assert config["discord_channel"] == CHANNEL
    return receipt, config


def tick(receipt):
    return subprocess.run([sys.executable, str(PLUGIN / "scripts/watcher_tick.py"),
                           "--config", receipt["config"]],
                          capture_output=True, text=True, timeout=20)


def inbox_texts(inbox):
    with sqlite3.connect(inbox) as conn:
        return [json.loads(row[0])["text"] for row in conn.execute("select payload from messages order by id")]


def calls_at(calls):
    return [json.loads(line) for line in calls.read_text().splitlines()]


def test_summary_problem_final_sent_directly_without_summary_inbox(discord_env, board):
    calls, inbox, sender = discord_env
    receipt, config = prepare("--cards", "t_wait", "--every", "0.02")
    assert "does not relay" in Path(receipt["brief"]).read_text()
    before = board.read_bytes()
    summary = tick(receipt)
    assert summary.returncode == 0 and "CONTINUE summary" in summary.stdout
    assert inbox_texts(inbox) == []
    assert board.read_bytes() == before
    with sqlite3.connect(board) as conn:
        conn.execute("update tasks set status='blocked', title=? where id='t_wait'",
                     ("@everyone /private/path feat/branch " + "a" * 40,))
    problem = tick(receipt)
    assert problem.returncode == 0 and "CONTINUE problem" in problem.stdout
    assert len(inbox_texts(inbox)) == 1 and "problem:" in inbox_texts(inbox)[0]
    with sqlite3.connect(board) as conn:
        conn.execute("update tasks set status='done' where id='t_wait'")
    before = board.read_bytes()
    final = tick(receipt)
    assert final.returncode == 0 and "STOP final" in final.stdout
    rows = inbox_texts(inbox)
    assert len(rows) == 2 and "FINAL:" in rows[-1]
    messages = calls_at(calls)
    assert len(messages) == 3
    assert "🟢 Done: 0" in messages[0]["text"]
    assert "🟡 Waiting: 1" in messages[0]["text"]
    # A problem wake that is also a due summary posts plain progress: problems are the
    # starting session's to handle (operator ruling, 2026-10-26).
    assert "Kanban progress update" in messages[1]["text"]
    assert all("roblem" not in m["text"] and "investigate" not in m["text"] for m in messages)
    assert "FINAL" in messages[-1]["text"] and "🟢 Done: 1" in messages[-1]["text"]
    draft = Path(config["relay_state"]).with_name("discord-draft.md")
    lint = subprocess.run([str(sender), str(draft), "--lint-only"],
                          capture_output=True, text=True, timeout=15)
    assert lint.returncode == 0 and "lint PASS" in lint.stdout
    invalid = draft.with_name("invalid-draft.md")
    invalid.write_text("No marker\n-# Source: desk")
    assert subprocess.run([str(sender), str(invalid), "--lint-only"],
                          capture_output=True, timeout=15).returncode != 0
    assert tick(receipt).returncode == 0
    assert len(calls_at(calls)) == 3 and len(inbox_texts(inbox)) == 2
    assert board.read_bytes() == before
    assert json.loads(Path(config["lease"]).read_text())["stopped"]


def test_problem_between_summaries_reaches_only_the_starting_session(discord_env, board):
    calls, inbox, _ = discord_env
    with sqlite3.connect(board) as conn:
        conn.execute("update tasks set status='blocked' where id='t_wait'")
    receipt, _ = prepare("--cards", "t_wait", "--every", "900")
    problem = tick(receipt)
    assert problem.returncode == 0 and "CONTINUE problem" in problem.stdout
    assert not calls.exists()
    rows = inbox_texts(inbox)
    assert len(rows) == 1 and rows[0].startswith("problem:")
    with sqlite3.connect(board) as conn:
        conn.execute("update tasks set status='done' where id='t_wait'")
    final = tick(receipt)
    assert final.returncode == 0 and "STOP final" in final.stdout
    messages = calls_at(calls)
    assert len(messages) == 1 and "FINAL" in messages[0]["text"]
    assert "roblem" not in messages[0]["text"]


@pytest.mark.parametrize("mode", ["1", "2", "missing-ok", "source-identity-refused"])
def test_failed_discord_delivery_is_unknown_pending_and_alerts_parent(discord_env, monkeypatch, mode):
    calls, inbox, _ = discord_env
    receipt, config = prepare("--cards", "t_done")
    monkeypatch.setenv("FAKE_DISCORD_MODE", mode)
    failed = tick(receipt)
    assert failed.returncode == 5 and "delivery UNKNOWN" in failed.stderr
    relay_path = Path(config["relay_state"])
    relay = json.loads(relay_path.read_text())
    expected = {"kind": "receipt_missing", "exit_code": 0} if mode == "missing-ok" else {
        "kind": "sender_exit", "exit_code": 1 if mode == "source-identity-refused" else int(mode)}
    assert relay["discord_failure"] == expected
    assert expected["kind"] in failed.stderr
    assert "UNTRUSTED" not in failed.stdout + failed.stderr + relay_path.read_text()
    assert "PRIVATE-SENDER-DIAGNOSTIC" not in failed.stdout + failed.stderr + relay_path.read_text()
    assert relay["pending"]["reason"] == "final"
    assert "discord_receipt" not in relay
    assert not json.loads(Path(config["lease"]).read_text())["stopped"]
    rows = inbox_texts(inbox)
    assert len(rows) == 1 and "Discord delivery UNKNOWN" in rows[0]
    assert expected["kind"] in rows[0]
    before = relay_path.read_bytes()
    monkeypatch.setenv("FAKE_DISCORD_MODE", "ok")
    again = tick(receipt)
    assert again.returncode == 5 and "reconciliation required" in again.stderr
    assert len(calls_at(calls)) == 1
    assert inbox_texts(inbox) == rows
    assert relay_path.read_bytes() == before


def test_successful_discord_send_not_repeated_after_inbox_failure(discord_env, monkeypatch, tmp_path):
    calls, inbox, _ = discord_env
    receipt, config = prepare("--cards", "t_done")
    tmux = tmp_path / "bin/tmux"
    good = tmux.read_text()
    tmux.write_text(f"#!{sys.executable}\nraise SystemExit(1)\n")
    failed = tick(receipt)
    assert failed.returncode == 5 and "delivery UNKNOWN" in failed.stderr
    relay = json.loads(Path(config["relay_state"]).read_text())
    assert relay["discord_receipt"]["message_id"] == "987654321098765432"
    assert len(calls_at(calls)) == 1 and inbox_texts(inbox) == []
    tmux.write_text(good)
    assert tick(receipt).returncode == 0
    assert len(calls_at(calls)) == 1 and len(inbox_texts(inbox)) == 1


def test_missing_sender_is_unknown_and_never_uses_real_sender(discord_env, monkeypatch, tmp_path):
    calls, inbox, _ = discord_env
    receipt, _ = prepare("--cards", "t_done")
    monkeypatch.setenv("KANBAN_WATCH_DISCORD_SENDER", str(tmp_path / "missing-sender"))
    failed = tick(receipt)
    assert failed.returncode == 5 and "delivery UNKNOWN" in failed.stderr
    assert not calls.exists()
    assert "Discord delivery UNKNOWN" in inbox_texts(inbox)[0]


@pytest.mark.parametrize("reason", ["UNKNOWN", "max_hours"])
def test_unknown_read_and_time_limit_are_not_done(discord_env, board, reason):
    calls, inbox, _ = discord_env
    receipt, config = prepare("--cards", "t_run")
    if reason == "UNKNOWN":
        board.write_bytes(b"broken")
    else:
        config["until"] = 1
        Path(receipt["config"]).write_text(json.dumps(config))
    out = tick(receipt)
    assert out.returncode == (5 if reason == "UNKNOWN" else 0)
    assert f"STOP {reason}" in out.stdout
    # A read failure or time limit is the starting session's to handle: inbox only, never Discord.
    assert not calls.exists()
    rows = inbox_texts(inbox)
    assert len(rows) == 1 and "FINAL" not in rows[0]


@pytest.mark.parametrize("channel", ["", "../bad", "1;exit", "@everyone"])
def test_invalid_channel_never_starts(launch_env, channel):
    log, inbox = launch_env
    out = launch("--discord-channel", channel)
    assert out.returncode != 0
    assert not log.exists() and inbox_texts(inbox) == []


def test_discord_requires_starting_session(discord_env):
    out = subprocess.run(["bash", str(PLUGIN / "scripts/start_watcher.sh"), "--prepare-only",
                          "--delivery", "native", "--board", "demo", "--discord-channel", CHANNEL],
                         capture_output=True, text=True, timeout=5)
    assert out.returncode != 0 and "--session" in out.stderr


def test_detached_discord_refused_before_state_or_service(discord_env):
    calls, inbox, _ = discord_env
    state_root = Path(os.environ["XDG_STATE_HOME"]) / "hermes-kanban-watch"
    out = launch("--discord-channel", CHANNEL, "--cards", "t_done")
    assert out.returncode == 1
    assert "detached" in out.stderr and "parent relay" in out.stderr
    assert not state_root.exists()
    assert not Path(os.environ["UNIT_CALLS"]).exists()
    assert not calls.exists() and inbox_texts(inbox) == []
    # The rejected direct launch did not reserve the board; parent relay still works.
    parent = launch("--cards", "t_done")
    assert parent.returncode == 0, parent.stderr
    assert tick(json.loads(parent.stdout)).returncode == 0
    assert len(inbox_texts(inbox)) == 1 and not calls.exists()


def test_unknown_recorded_before_parent_notification_failure(discord_env, monkeypatch, tmp_path):
    calls, _, _ = discord_env
    receipt, config = prepare("--cards", "t_done")
    tmux = tmp_path / "bin/tmux"
    good = tmux.read_text()
    tmux.write_text(f"#!{sys.executable}\nraise SystemExit(1)\n")
    monkeypatch.setenv("FAKE_DISCORD_MODE", "source-identity-refused")
    assert tick(receipt).returncode == 5
    relay = json.loads(Path(config["relay_state"]).read_text())
    assert relay["discord_failure"] == {"kind": "sender_exit", "exit_code": 1}
    assert relay["pending"]["reason"] == "final"
    tmux.write_text(good)
    monkeypatch.setenv("FAKE_DISCORD_MODE", "ok")
    assert tick(receipt).returncode == 5
    assert len(calls_at(calls)) == 1


@pytest.mark.parametrize("error,kind", [
    (subprocess.TimeoutExpired(["PRIVATE-SENDER-DIAGNOSTIC"], 45,
                               output="PRIVATE-SENDER-DIAGNOSTIC", stderr="PRIVATE-SENDER-DIAGNOSTIC"),
     "sender_timeout"),
    (PermissionError("PRIVATE-SENDER-DIAGNOSTIC"), "sender_unavailable"),
])
def test_sender_process_errors_are_safe_and_not_retried(discord_env, monkeypatch, error, kind):
    # Run the real relay logic in-process so timeouts need no 45-second wait.
    monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
    import watcher_tick
    _, _, _ = discord_env
    receipt, config = prepare("--cards", "t_done")
    attempts = []

    def fail(*args, **kwargs):
        attempts.append(1)
        raise error

    monkeypatch.setattr(watcher_tick.subprocess, "run", fail)
    monkeypatch.setattr(watcher_tick, "notify", lambda *args: {"queued": True})
    for _ in range(2):
        with pytest.raises(ValueError) as caught:
            watcher_tick.tick(receipt["config"])
        assert "PRIVATE-SENDER-DIAGNOSTIC" not in str(caught.value)
    relay = json.loads(Path(config["relay_state"]).read_text())
    assert relay["discord_failure"] == {"kind": kind, "exit_code": None}
    assert relay["pending"]["reason"] == "final" and "discord_receipt" not in relay
    assert len(attempts) == 1
