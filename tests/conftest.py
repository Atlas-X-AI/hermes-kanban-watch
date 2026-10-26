"""Every test runs with disposable user and Hermes homes."""
import os
import sqlite3
import time
from pathlib import Path

import pytest

from helpers import create_board

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugin"


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "user"
    home.mkdir()
    hermes = home / ".hermes"
    hermes.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    for kind in ("CACHE", "CONFIG", "DATA", "STATE"):
        monkeypatch.setenv(f"XDG_{kind}_HOME", str(tmp_path / f"xdg-{kind.lower()}"))
    # A test must explicitly install its fake sender; never inherit an outbound route.
    monkeypatch.setenv("KANBAN_WATCH_DISCORD_SENDER", str(home / "no-discord-sender"))
    for name in tuple(os.environ):
        if name.startswith("HERMES_KANBAN_") or name == "KANBAN_WATCH_ROUTES":
            monkeypatch.delenv(name, raising=False)
    return home, hermes


@pytest.fixture
def board(isolated_home):
    _, hermes = isolated_home
    directory = hermes / "kanban" / "boards" / "demo"
    directory.mkdir(parents=True)
    db = create_board(directory / "kanban.db")
    now = int(time.time())
    with sqlite3.connect(db) as con:
        con.execute("insert into task_links values('t_run', 't_wait')")
        con.executemany("insert into tasks values(?,?,?,?,?,?,?,?,?,?)", [
            ("t_run", "Running card", "builder", "running", now - 60, now - 50, None, now, 1, 1),
            ("t_wait", "Queued card", "builder", "ready", now - 60, None, None, None, None, None),
            ("t_done", "Done card", "builder", "done", now - 60, now - 50, now - 5, None, None, 2),
        ])
        con.executemany("insert into task_runs values(?,?,?,?,?,?,?,?,?,?)", [
            (1, "t_run", "running", None, "worker-model", "worker-provider", None, now - 50, None, "Building"),
            (2, "t_done", "done", "completed", "review-model", "review-provider", None, now - 50, now - 5, "Complete"),
        ])
        con.execute("insert into task_events(task_id,kind,payload,created_at) values(?,?,?,?)",
                    ("t_wait", "created", '{"creator_task_id":"t_run"}', now))
    store = hermes / "profiles" / "builder"
    store.mkdir(parents=True)
    with sqlite3.connect(store / "state.db") as con:
        con.executescript("""
            create table sessions(id text primary key, source text, title text, started_at real);
            create table messages(id integer primary key, session_id text, role text, content text,
                tool_calls text, tool_name text, timestamp real, active int default 1);
            insert into sessions values('s1','kanban','work kanban task t_run',1);
            insert into sessions values('s2','kanban','work kanban task t_done',1);
            insert into messages(session_id,role,content,timestamp) values('s1','assistant','Building now',1);
            insert into messages(session_id,role,content,timestamp) values('s2','assistant','Finished now',1);
        """)
    return db


@pytest.fixture
def routes():
    return {
        "providers": {"openai": "gpt", "anthropic": "claude", "compatible": "gpt"},
        "classes": {"app-code": {"profile": "builder", "model": "gpt-5", "provider": "openai"}},
        "verifiers": {
            "gpt": {"profile": "reviewer", "model": "claude-sonnet-4-5", "provider": "anthropic"},
            "claude": {"profile": "builder", "model": "gpt-5", "provider": "openai"},
        },
    }
