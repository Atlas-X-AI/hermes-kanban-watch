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
    concurrent = os.environ.get('FAKE_CONCURRENT_FILE')
    if concurrent and not Path(concurrent).exists():
        Path(concurrent).write_text('concurrent writer')
    lane = '|'.join(args[args.index(flag) + 1] for flag in ('-p', '-m', '--provider'))
    response = json.loads(os.environ.get('FAKE_PROBES', '{}')).get(lane, os.environ.get('FAKE_PROBE', 'PONG'))
    if response == 'EXIT_1':
        sys.exit(1)
    print(response)
elif args[:2] == ['kanban', '--board'] and len(args) > 3:
    board, action = args[2:4]
    root = Path(os.environ['HERMES_HOME'])
    db = root / 'kanban.db' if board == 'default' else root / 'kanban/boards' / board / 'kanban.db'
    if not db.is_file():
        sys.exit(1)
    if os.environ.get('FAKE_FAIL') == action:
        print('suppressed subprocess error', file=sys.stderr)
        sys.exit(1)
    store = db.with_suffix('.cards.json')
    cards = json.loads(store.read_text()) if store.exists() else {}
    def value(flag):
        return args[args.index(flag) + 1] if flag in args else None
    if action == 'create':
        if len(args) < 5 or args[4].startswith('--') or '--title' in args:
            sys.exit(2)
        task_id = 't_' + format(len(cards) + 1, '08x')
        workspace = value('--workspace') or 'scratch'
        kind, _, path = workspace.partition(':')
        card = {'id': task_id, 'title': args[4], 'assignee': value('--assignee'),
                'body': value('--body'), 'workspace_kind': kind, 'workspace_path': path or None,
                'model_override': value('--model'), 'provider_override': value('--provider')}
        cards[task_id] = card
        store.write_text(json.dumps(cards))
        print(json.dumps(card))
    elif action == 'set-model':
        card = cards[args[4]]
        card.update(model_override=args[5], provider_override=value('--provider'))
        store.write_text(json.dumps(cards))
        print('Model updated')
    elif action == 'show':
        card = cards[args[4]]
        card.update(json.loads(os.environ.get('FAKE_SHOW', '{}')))
        print(json.dumps({'task': card, 'parents': [], 'children': [], 'runs': []}))
    else:
        sys.exit(2)
else:
    sys.exit(2)
''')
    fake.chmod(0o755)
    return fake
