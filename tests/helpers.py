"""Dependency-free disposable fixtures shared by pytest and install smoke."""
import sqlite3
import sys


BOARD_SCHEMA = """
    create table tasks(id text primary key, title text, assignee text, status text,
        created_at int, started_at int, completed_at int, last_heartbeat_at int,
        worker_pid int, current_run_id int);
    create table task_runs(id integer primary key, task_id text, status text,
        outcome text, model_used text, provider_used text, fallback_from text,
        started_at int, ended_at int, summary text);
    create table task_events(id integer primary key, task_id text, kind text,
        payload text, created_at int, run_id int);
    create table task_links(parent_id text, child_id text);
"""


def create_board(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.executescript(BOARD_SCHEMA)
    return path


def fake_hermes(bindir):
    bindir.mkdir(parents=True, exist_ok=True)
    fake = bindir / "hermes"
    fake.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['FAKE_CALLS'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
if args == ['--version']:
    print('Hermes Agent v0.21.2')
elif (args[:3] == ['kanban', 'boards', 'create'] and len(args) == 4) or 'init' in args:
    creating = args[:3] == ['kanban', 'boards', 'create']
    board = args[args.index('--board') + 1] if '--board' in args else 'default'
    if creating:
        board = args[3]
    root = Path(os.environ['HERMES_HOME'])
    db = root / 'kanban.db' if board == 'default' else root / 'kanban/boards' / board / 'kanban.db'
    if not creating and board != 'default' and not db.is_file():
        print('Board does not exist', file=sys.stderr)
        sys.exit(1)
    db.parent.mkdir(parents=True, exist_ok=True)
    if not db.exists():
        db.write_bytes(b'fake board')
elif 'chat' in args:
    print(os.environ.get('FAKE_PROBE', 'PONG'))
else:
    sys.exit(2)
''')
    fake.chmod(0o755)
    return fake
