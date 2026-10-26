#!/usr/bin/env python3
"""Queue one project_channel_message for an exact Claude tmux session.

Row/payload contract: ping.it pingit/need_deliver.py. No direct tmux typing.
A queued row is not proof that claude-inbox-poller has delivered it.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import uuid


def resolve_pane(session):
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}", session):
        raise ValueError("Invalid exact tmux session name")
    # '=' prevents tmux's prefix matching; ':' selects the session, not a window.
    result = subprocess.run(["tmux", "list-panes", "-t", f"={session}:", "-F", "#{pane_id}"],
                            capture_output=True, text=True, timeout=10)
    panes = result.stdout.splitlines()
    if result.returncode or len(panes) != 1 or not re.fullmatch(r"%[0-9]+", panes[0]):
        raise ValueError("Session missing, pane unreadable or multiple panes; nothing queued")
    return panes[0]


def notify(session, text, key=None, database=None):
    if not isinstance(text, str) or not text.strip() or len(text) > 16000:
        raise ValueError("Supply non-empty summary text, at most 16000 characters")
    pane = resolve_pane(session)  # No DB open/write until exact target validation succeeds.
    target = f"={session}:.{pane}"  # Refused by tmux if the pane moves to another session.
    database = Path(database) if database else Path.home() / ".claude/state/messages.db"
    path = database.expanduser().resolve(strict=True)
    key = key or "kanban-watch:" + uuid.uuid4().hex
    if not re.fullmatch(r"kanban-watch:[A-Za-z0-9:_-]{1,200}", key):
        raise ValueError("Invalid notification key")
    data = json.dumps({"text": text, "source": "kanban-watch",
                       "project_channel": {"type": "project_channel_message", "render": text}}, ensure_ascii=False)
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=2)) as conn:
        conn.execute("begin immediate")
        have = conn.execute("select id,target,notify_target,payload from messages where sender=? and dedupe_key=?",
                            ("kanban-watcher", key)).fetchone()
        if have:
            if have[1:] != (session, target, data):
                raise ValueError("Notification key reused with another target or text")
            row_id = have[0]
        else:
            row_id = conn.execute(
                "insert into messages(sender,target,message_type,payload,notify_target,dedupe_key,notified) "
                "values(?,?,?,?,?,?,?)", ("kanban-watcher", session, "project_channel_message", data, target, key, 0)
            ).lastrowid
        conn.commit()
        row = conn.execute("select id,notified from messages where id=? and sender=? and dedupe_key=?",
                           (row_id, "kanban-watcher", key)).fetchone()
        if row is None:
            raise ValueError("Queued row could not be read back")
    return {"id": row[0], "notified": row[1], "notify_target": target}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True)
    parser.add_argument("--text-file", type=Path, required=True)
    parser.add_argument("--key", help="Stable kanban-watch: key for a safe retry")
    args = parser.parse_args()
    try:
        print(json.dumps(notify(args.session, args.text_file.read_text(encoding="utf-8"), args.key), sort_keys=True))
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"notify_session: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
