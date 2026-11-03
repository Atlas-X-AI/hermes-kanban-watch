#!/usr/bin/env python3
"""Queue one project_channel_message for an exact Claude tmux session.

Row/payload contract: ping.it pingit/need_deliver.py. No direct tmux typing.
A queued row is not proof that claude-inbox-poller has delivered it.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import uuid


def resolve_pane(session, pane=None, pane_pid=None, *, fallback_single=False):
    """Return an exact (pane ID, pane process ID), never the active window's pane."""
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,199}", session):
        raise ValueError("Invalid exact tmux session name")
    if pane is not None and (not isinstance(pane, str) or not re.fullmatch(r"%[0-9]+", pane)):
        raise ValueError("Invalid exact tmux pane ID")
    if pane_pid is not None and (type(pane_pid) is not int or pane_pid <= 0 or pane is None):
        raise ValueError("Invalid recorded tmux pane process ID")
    # '=' prevents prefix matching; '-s' includes EVERY window, not just the active one.
    result = subprocess.run(["tmux", "list-panes", "-s", "-t", f"={session}:",
                             "-F", "#{pane_id} #{pane_pid}"],
                            capture_output=True, text=True, timeout=10)
    lines = result.stdout.splitlines()
    if result.returncode or not lines or any(not re.fullmatch(r"%[0-9]+ [1-9][0-9]*", line) for line in lines):
        raise ValueError("Session missing or panes unreadable; nothing queued")
    panes = [line.split() for line in lines]
    matches = [row for row in panes if pane is None or row[0] == pane]
    if not matches and fallback_single and pane_pid is None and len(panes) == 1:
        matches = panes
    if len(matches) != 1:
        raise ValueError("Recorded pane missing or target ambiguous; supply --pane or TMUX_PANE at prepare time; nothing queued")
    found, pid = matches[0][0], int(matches[0][1])
    if pane_pid is not None and pid != pane_pid:
        raise ValueError("Recorded pane process changed; nothing queued")
    return found, pid


def resolve_agent(pane_pid, agent_pid=None, agent_start=None):
    """Find one Claude process by ancestry, not the pane's long-lived shell.

    Read process names only, never command arguments or environment. Unknown
    runtimes and ambiguous trees refuse rather than treating a shell as an agent.
    """
    if (agent_pid is None) != (agent_start is None) or (agent_pid is not None and (
            type(agent_pid) is not int or agent_pid <= 0 or not isinstance(agent_start, str) or not agent_start)):
        raise ValueError("Invalid recorded agent identity")
    # Fixed fields with the state BEFORE lstart: a zombie's comm prints as
    # "claude <defunct", so any layout ending in command columns can misread
    # the state from the command text. Parse from the left, never from comm.
    result = subprocess.run(["ps", "-eo", "pid=,ppid=,stat=,lstart=,comm="],
                            capture_output=True, text=True, timeout=10, env={**os.environ, "LC_ALL": "C"})
    if result.returncode:
        raise ValueError("Agent process tree unreadable; nothing queued")
    processes = {}
    for line in result.stdout.splitlines():
        fields = line.split(None, 8)
        if len(fields) != 9 or not fields[0].isdigit() or not fields[1].isdigit():
            raise ValueError("Agent process tree unreadable; nothing queued")
        pid, parent, state = int(fields[0]), int(fields[1]), fields[2]
        if pid in processes:
            raise ValueError("Agent process tree ambiguous; nothing queued")
        processes[pid] = (parent, state, " ".join(fields[3:8]), fields[8])

    def in_pane(pid):
        seen = set()
        while pid in processes and pid not in seen:
            if pid == pane_pid:
                return True
            seen.add(pid)
            pid = processes[pid][0]
        return False

    agents = [(pid, row[2]) for pid, row in processes.items()
              if Path(row[3]).name == "claude" and row[1][:1] not in {"Z", "X"} and in_pane(pid)]
    if len(agents) != 1:
        raise ValueError("Pane has no unique Claude agent process; nothing queued")
    found, started = agents[0]
    if agent_pid is not None and (found != agent_pid or started != agent_start):
        raise ValueError("Recorded agent process changed; nothing queued")
    return found, started


def notify(session, text, key=None, database=None, *, pane=None, pane_pid=None,
           agent_pid=None, agent_start=None):
    if not isinstance(text, str) or not text.strip() or len(text) > 16000:
        raise ValueError("Supply non-empty summary text, at most 16000 characters")
    pane, root_pid = resolve_pane(session, pane, pane_pid)  # Validate before any DB open/write.
    resolve_agent(root_pid, agent_pid, agent_start)
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
    parser.add_argument("--pane", help="Exact pane ID; otherwise the whole session must have one pane")
    parser.add_argument("--pane-pid", type=int, help="Require the recorded pane process ID")
    parser.add_argument("--agent-pid", type=int, help="Require the recorded Claude agent PID")
    parser.add_argument("--agent-start", help="Require the recorded Claude process start time")
    parser.add_argument("--text-file", type=Path, required=True)
    parser.add_argument("--key", help="Stable kanban-watch: key for a safe retry")
    args = parser.parse_args()
    try:
        print(json.dumps(notify(args.session, args.text_file.read_text(encoding="utf-8"), args.key,
                                pane=args.pane, pane_pid=args.pane_pid,
                                agent_pid=args.agent_pid, agent_start=args.agent_start), sort_keys=True))
    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"notify_session: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
