"""Real inbox SQL, fake tmux plus one owned-server contract test, no live delivery."""
import json
import os
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
if sys.argv[1:] != ['list-panes', '-t', '=desk:', '-F', '#{pane_id}']:
    sys.exit(1)
print(os.environ.get('TMUX_PANES', '%42'))
''')
    tmux.chmod(0o755)
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


@pytest.mark.parametrize("panes", ["", "%42\n%43", "not-a-pane"])
def test_ambiguous_or_invalid_pane_refused(inbox, monkeypatch, panes):
    db, text = inbox
    monkeypatch.setenv("TMUX_PANES", panes)
    before = db.read_bytes()
    assert notify(text).returncode != 0
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


def test_real_tmux_exact_session_contract_in_owned_server(inbox, tmp_path):
    """Outside the fake: prove exact/pane targets on a separate temporary server."""
    real_tmux = shutil.which("tmux", path=os.defpath)
    if real_tmux is None:
        pytest.skip("tmux not installed")
    db, text = inbox
    socket = tmp_path / "tmux.sock"
    base = [real_tmux, "-S", str(socket)]
    created = subprocess.run([*base, "-f", "/dev/null", "new-session", "-d", "-s", "desk", "sleep 60"],
                             capture_output=True, text=True, timeout=5)
    assert created.returncode == 0, created.stderr
    try:
        wrapper = tmp_path / "bin/tmux"
        wrapper.write_text(f"#!{sys.executable}\n" +
                           f"import os, sys\nos.execv({real_tmux!r}, {base!r} + sys.argv[1:])\n")
        out = notify(text)
        assert out.returncode == 0, out.stderr
        receipt = json.loads(out.stdout)
        pane = subprocess.run([*base, "display-message", "-p", "-t", receipt["notify_target"], "#{pane_id}"],
                              capture_output=True, text=True, timeout=5)
        assert pane.returncode == 0 and pane.stdout.strip().startswith("%")
        assert receipt["notify_target"] == "=desk:." + pane.stdout.strip()
        before = db.read_bytes()
        assert notify(text, "des").returncode != 0
        assert db.read_bytes() == before
    finally:
        subprocess.run([*base, "kill-server"], capture_output=True, timeout=5, check=True)
