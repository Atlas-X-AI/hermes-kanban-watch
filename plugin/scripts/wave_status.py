#!/usr/bin/env python3
"""Read-only snapshot of explicitly named wave members. Never dispatch or mutate.

Exit 0: snapshot read successfully (inspect all_done separately).
Exit 2: missing task, unreadable/locked database, invalid input or unknown state.
Exit 3: --wait timed out before the members were quiescent (the last snapshot is printed).

--wait SECONDS re-reads the same explicit members every --interval seconds until all are
quiescent (or, with --stop-on-blocked, until any member is blocked), then prints that
snapshot. It replaces hand-written sqlite polling loops; it still never mutates anything.
Board completion does not establish integration, installation or user acceptance.

Capped cards: a member whose status is ready and whose
latest task_events row (ignoring capped_alerted) is kind "capped" gets a "capped" object, e.g.
label "capped builder 2/2 14m" (scope, running/cap, minutes since the cap episode began), plus
"alerted" when a capped_alerted followed. The report carries "capped" (count) and a one-line
"summary". Boards without a task_events table read exactly as before, with capped 0.
"""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time

KNOWN = {'triage', 'todo', 'ready', 'running', 'review', 'blocked', 'done', 'archived'}
QUIESCENT = {'review', 'blocked', 'done', 'archived'}


def cap_label(con, task_id, now=None):
    """Current cap facts, including a display label, or None. Read-only, legacy-safe.

    Only ready cards whose latest event (apart from capped_alerted) is capped
    qualify. Both wave snapshots and live wait rows use this same rule.
    """
    columns = {row[1] for row in con.execute('PRAGMA table_info(task_events)')}
    if not {'id', 'task_id', 'kind', 'payload', 'created_at'} <= columns:
        return None
    ready = con.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    if not ready or ready[0] != 'ready':
        return None
    events = con.execute(
        "SELECT kind,payload,created_at FROM task_events WHERE task_id=? "
        "ORDER BY created_at DESC, id DESC", (task_id,))
    alerted = False
    for kind, payload, created_at in events:
        if kind == 'capped_alerted':
            alerted = True
            continue
        if kind != 'capped':
            return None
        break
    else:
        return None
    try:
        data = json.loads(payload) if payload else {}
        if not isinstance(data, dict):
            data = {}
    except ValueError:
        data = {}
    scope = data.get('scope') if isinstance(data.get('scope'), str) else None
    assignee = data.get('assignee') if isinstance(data.get('assignee'), str) else None
    running, cap = data.get('running'), data.get('cap')
    who = scope if scope and scope.startswith('group:') else (assignee or '?')
    now = time.time() if now is None else now
    minutes = max(0, int((now - created_at) // 60)) if isinstance(created_at, (int, float)) else None
    ratio = f"{'?' if running is None else running}/{'?' if cap is None else cap}"
    return {'scope': scope or 'UNKNOWN', 'assignee': assignee, 'running': running, 'cap': cap,
            'waiting_minutes': minutes, 'alerted': alerted,
            'label': f"capped {who} {ratio} {'?' if minutes is None else minutes}m"}


def snapshot(database, task_ids, now=None):
    """One read transaction, explicit membership, fail closed on missing records."""
    if not task_ids or len(task_ids) > 200 or len(set(task_ids)) != len(task_ids):
        raise ValueError('Supply 1–200 unique task IDs')
    database = Path(database).expanduser().resolve(strict=True)
    uri = database.as_uri() + '?mode=ro'
    with closing(sqlite3.connect(uri, uri=True, timeout=3)) as con:
        con.execute('PRAGMA query_only=ON')
        con.execute('BEGIN')
        columns = {row[1] for row in con.execute('PRAGMA table_info(tasks)')}
        if not {'id', 'status', 'assignee'} <= columns:
            raise ValueError('Unsupported tasks schema')
        placeholders = ','.join('?' for _ in task_ids)
        records = {row[0]: {'id': row[0], 'status': row[1], 'assignee': row[2]}
                   for row in con.execute(
                       f'SELECT id,status,assignee FROM tasks WHERE id IN ({placeholders})', task_ids)}
        if set(records) != set(task_ids):
            raise ValueError('Requested task missing from the explicit database')
        if any(row['status'] not in KNOWN for row in records.values()):
            raise ValueError('Unknown task state: inspect with the owning Kanban tool')
        ready = [tid for tid in task_ids if records[tid]['status'] == 'ready']
        now = time.time() if now is None else now
        for tid in ready:
            capped = cap_label(con, tid, now)
            if capped:
                records[tid]['capped'] = capped
    rows = [records[tid] for tid in task_ids]
    counts = dict(sorted(Counter(row['status'] for row in rows).items()))
    capped_count = sum(1 for row in rows if 'capped' in row)
    summary = (f"{len(rows)} members | " + ', '.join(f'{k} {v}' for k, v in counts.items())
               + f' | capped {capped_count}')
    return {'database': str(database), 'members': rows,
            'counts': counts, 'capped': capped_count, 'summary': summary,
            'quiescent': all(row['status'] in QUIESCENT for row in rows),
            'all_done': all(row['status'] == 'done' for row in rows),
            'delivery': 'NOT_ASSESSED'}


def wait_for(database, task_ids, timeout, interval=30.0, stop_on_blocked=False,
             sleep=time.sleep, clock=time.monotonic):
    """Poll snapshot() until quiescent / a blocked member / timeout. Returns (report, reached)."""
    if timeout <= 0 or interval <= 0:
        raise ValueError('--wait and --interval must be positive')
    deadline = clock() + timeout
    while True:
        report = snapshot(database, task_ids)
        if report['quiescent'] or (stop_on_blocked and 'blocked' in report['counts']):
            return report, True
        if clock() >= deadline:
            return report, False
        sleep(min(interval, max(0.0, deadline - clock())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True, type=Path, help='Exact existing board DB; no global current pointer')
    parser.add_argument('--task', required=True, action='append', help='Exact task ID; repeat for each commissioned member')
    parser.add_argument('--wait', type=float, help='Seconds to keep re-reading until the members are quiescent')
    parser.add_argument('--interval', type=float, default=30.0, help='Seconds between reads with --wait (default 30)')
    parser.add_argument('--stop-on-blocked', action='store_true', help='With --wait, return as soon as any member is blocked')
    args = parser.parse_args()
    try:
        if args.wait is None:
            print(json.dumps(snapshot(args.db, args.task), sort_keys=True))
        else:
            report, reached = wait_for(args.db, args.task, args.wait, args.interval, args.stop_on_blocked)
            report['wait'] = 'REACHED' if reached else 'TIMEOUT'
            print(json.dumps(report, sort_keys=True))
            if not reached:
                return 3
    except (OSError, ValueError, sqlite3.Error):
        print(json.dumps({'state': 'UNKNOWN', 'error': 'Cannot establish this explicit wave snapshot; inspect inputs/schema/access with the owning tools'}))
        return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
