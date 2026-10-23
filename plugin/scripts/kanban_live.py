#!/usr/bin/env python3
"""Read-only live view of Hermes Kanban cards, for watcher agents in a Workflow.

The worker's own conversation (assistant text, tool calls, tool results) lives in the
assignee profile's session store, ``~/.hermes/profiles/<assignee>/state.db``, under a
session titled ``work kanban task <card id>``. This script tails it, so a watcher
subagent's transcript shows the card's agent working, step by step.

Subcommands (all read-only; nothing here writes to a board or a session store):

  snapshot   --board B --task T [--task T...]        one JSON line per card
  wait-start --board B --task T --max-seconds N      exit 0 once the card left the queue
  wait-any   --board B --task T [--task T...]        JSON started ids and cards on any queue exit;
                                                     0 a start (missing ids ride along), 3 timeout,
                                                     4 missing and no start, 5 unreadable, 6 NOT_STARTING
  follow     --board B --task T --max-seconds N      print new worker messages as they land;
                                                     exit 0 once the card is terminal
  active     --board B                                JSON {now, cards} for every queued or running card
  cards      --board B [--task T...]                  JSON {now, cards} with parents, creator and model;
                                                     no --task = every queued or running card
  report     --board B [--task T...] [--since EPOCH] [--format json|text]
                                                     cards (including finished) and run-time totals
  verify-pair --board B --worker T --verifier V [--families FILE]
                                                     compare actual last finished runs, not pins;
                                                     0 different families, 1 same/unknown, 3 no finished run
  events     --board B --watch id=status,... [--known a,b] --since EPOCH --max-seconds N
                                                     exit 0 + JSON once a watched card files/parents a card
                                                     that is in neither --watch nor --known; 2 = all watched
                                                     cards done/archived. "changed" includes queued starts,
                                                     blocked/review cards returning to running/queued, running
                                                     or queued cards that stopped as blocked/review, and
                                                     blocked/review cards that moved to done/archived.
                                                     A resume or a stop exits 0 promptly; a queue start and a
                                                     blocked/review -> done move do not end the call.
  scout      --board B --known a,b --since EPOCH --max-seconds N
                                                     exit 0 + JSON when new cards appear,
                                                     2 when every known card is terminal
  needs      --board B --since EPOCH --max-seconds N [--ready-stale S] [--cursor-key K] [--created-by NAME]
                                                     exit 0 + JSON {items} when a card needs the parent:
                                                     an ASK OPERATOR comment, a block or wait whose reason
                                                     names the operator, or a card ready for S seconds that no
                                                     dispatcher has started. --created-by also reports
                                                     that creator's blocked/waiting cards once per card
                                                     (latest reason), then hourly while it is unfinished and waits again.
                                                     Standing the operator waits repeat at most once an hour.
                                                     A card Hermes parks in triage (block_loop_detected) is reported as
                                                     parked_for_human whatever its reason, once per park event, board-wide
                                                     (not filtered by --created-by); for the caller's own card it replaces
                                                     the parent item.
                                                     --loop owns timeout retries with a fixed --since (default: now),
                                                     and includes existing current waits on its first poll.

Exit codes: 0 reached, 2 nothing left to watch (scout; events: every watched card is done or
archived) or bad arguments (argparse), 3 time
budget used (call again), 4 not found, 5 board unreadable, 6 a ready card is not starting
(wait-start; its last line is `status NOT_STARTING`). A timeout is normal: the caller runs the
same command again. Retry only exit 3. Any other code, including 1 (the script itself failed),
is reported, never retried.

Follow cursors live in ~/.cache/hermes-kanban-watch/live/; repeat calls resume.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

# Doctor's --version check must not create import caches in the installation.
sys.dont_write_bytecode = True

def _no_cap_label(conn, task_id, now=None):
    return None


def _load_cap_label():
    """Cap labels are optional: a missing, old or broken wave_status.py must not break every subcommand.

    Resolve the sibling by file path through symlinks: callers also load this script via
    importlib from outside scripts/, where a bare `import wave_status` would not resolve.
    """
    try:
        spec = importlib.util.spec_from_file_location(
            "_kanban_wave_status", Path(__file__).resolve().with_name("wave_status.py"))
        if spec is None or spec.loader is None:
            raise ImportError("no loader")
        module = importlib.util.module_from_spec(spec)
        # A sibling may print at import time: that must never reach stdout, which carries JSON.
        with contextlib.redirect_stdout(sys.stderr):
            spec.loader.exec_module(module)
        return module.cap_label
    except Exception as exc:  # a sibling may raise anything at import; cap labels are optional
        print(f"kanban_live: cap labels off, wave_status.py not usable ({type(exc).__name__})", file=sys.stderr)
        return _no_cap_label


cap_label = _load_cap_label()

HERMES = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
CURSOR_DIR = Path.home() / ".cache" / "hermes-kanban-watch" / "live"
QUEUED = {"triage", "todo", "ready", "scheduled"}
TERMINAL = {"done", "blocked", "archived", "review"}
FINAL = {"done", "archived"}
SNIP = 400
REASON_MAX = 2000
LEGACY_REASON = "Legacy block reason unknown"
# Events after which an older `blocked` event no longer describes why the card is blocked.
BLOCK_RESETS = ("unblocked", "claimed", "gave_up")
WAIT_REPEAT = 3600
# A card is still waiting only in one of these states, with none of these events after the wait.
WAITING = {"triage", "todo", "ready", "scheduled", "blocked"}
WAIT_ENDS = ("claimed", "unblocked", "completed", "gave_up", "spawned", "archived")
# Asks are headed "ASK OPERATOR:"; prose such as "no ASK OPERATOR yet" is not one.
ASK = re.compile(r"^[ \t>#*_-]*ASK OPERATOR[*_]*\s*:", re.I | re.M)
# A planned "Then: ASK OPERATOR" is not a current wait on the operator.
NAMES_OPERATOR = re.compile(r"\b(waiting (on|for) operator|needs? operator)\b", re.I)
WARNING_KINDS = ("completion_warnings", "protocol_violation", "suspected_hallucinated_references",
                 "model_fallback", "timed_out", "crashed", "block_loop_detected")



def board_db(board: str) -> Path:
    if board in ("", "default"):
        return HERMES / "kanban.db"
    return HERMES / "kanban" / "boards" / board / "kanban.db"


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def read_card(board: str, task: str) -> dict | None:
    with connect(board_db(board)) as conn:
        # The run pointer and its warning counts must describe the same snapshot.
        conn.execute("begin")
        row = conn.execute(
            "select id, title, assignee, status, created_at, started_at, completed_at,"
            " last_heartbeat_at, worker_pid, current_run_id from tasks where id = ?",
            (task,),
        ).fetchone()
        if row is None:
            return None
        card = dict(row)
        run = conn.execute(
            "select id, status, outcome, model_used, provider_used, fallback_from,"
            " started_at, ended_at, substr(coalesce(summary, ''), 1, ?) as summary"
            " from task_runs where task_id = ? and (? is null or id = ?) order by id desc limit 1",
            (SNIP, task, card["current_run_id"], card["current_run_id"]),
        ).fetchone()
        card["run"] = dict(run) if run else None
        card["warnings"] = run_warnings(conn, task, card["run"])
        event = conn.execute(
            "select kind, created_at from task_events where task_id = ?"
            " and kind != 'heartbeat' order by id desc limit 1",
            (task,),
        ).fetchone()
        card["last_event"] = dict(event) if event else None
        card["block_reason"] = clip_reason(block_reason(conn, task)) if card["status"] == "blocked" else None
    return card


def run_warnings(conn: sqlite3.Connection, task: str, run: dict | None) -> dict[str, int]:
    """Counts for this run only. NULL/missing event run ids use the run's start.

    A different explicit run_id never falls back to time, even if its event landed
    late. Without a run there is no safe time boundary, so no warnings are inferred.
    """
    if not run:
        return {}
    columns = {r["name"] for r in conn.execute("pragma table_info(task_events)")}
    scope = "created_at >= ?"
    params = [run["started_at"]]
    if "run_id" in columns:
        scope = "(run_id = ? or (run_id is null and created_at >= ?))"
        params = [run["id"], run["started_at"]]
    return dict(conn.execute(
        f"select kind, count(*) from task_events where task_id = ? and {scope}"
        f" and kind in ({','.join('?' * len(WARNING_KINDS))}) group by kind order by kind",
        (task, *params, *WARNING_KINDS),
    ).fetchall())


def block_reason(conn: sqlite3.Connection, task: str) -> str | None:
    """Why the card is blocked now, in the order `hermes kanban show` trusts it.

    The tasks.block_reason column is current (auto-blocks write it, and no new `blocked`
    event), so it wins unless it is the migration placeholder. Next comes the newest of the
    block-history events, but only when it is a `blocked` event or a `gave_up` carrying a
    block_reason: an `unblocked` or `claimed` after a block makes that block stale.
    """
    if "block_reason" in {r["name"] for r in conn.execute("pragma table_info(tasks)")}:
        column = conn.execute("select block_reason from tasks where id = ?", (task,)).fetchone()[0]
        if column and str(column).strip() != LEGACY_REASON:
            return str(column)
    marks = ",".join("?" * (len(BLOCK_RESETS) + 1))
    event = conn.execute(
        f"select kind, payload from task_events where task_id = ? and kind in ({marks})"
        " order by id desc limit 1", (task, "blocked", *BLOCK_RESETS),
    ).fetchone()
    if event and event["kind"] in ("blocked", "gave_up"):
        try:
            payload = json.loads(event["payload"]) or {}
            reason = payload.get("reason" if event["kind"] == "blocked" else "block_reason")
        except (ValueError, TypeError, AttributeError):
            reason = None
        if reason:
            return str(reason)
    if conn.execute("select 1 from sqlite_master where type='table' and name='task_comments'").fetchone():
        comment = conn.execute(
            "select body from task_comments where task_id = ? order by id desc limit 1", (task,),
        ).fetchone()
        if comment and comment[0]:
            return str(comment[0])
    return None


def clip_reason(reason: str | None) -> str | None:
    """Keep the whole reason; a rare very long one is cut with a marker saying so."""
    if not reason or len(reason) <= REASON_MAX:
        return reason
    return reason[:REASON_MAX] + f"… [cut: {len(reason)} characters in full]"


def is_heartbeat(name: str | None) -> bool:
    return (name or "").rsplit(".", 1)[-1] == "kanban_heartbeat"


def tool_names(row: sqlite3.Row) -> list[str]:
    try:
        calls = json.loads(row["tool_calls"] or "[]") or []
    except (ValueError, TypeError):
        return ["?"]
    return [str((call.get("function", call) if isinstance(call, dict) else {}).get("name", "?")) for call in calls]


def is_progress(row: sqlite3.Row) -> bool:
    """Assistant text, a tool call or a tool result that is not just the heartbeat."""
    if row["role"] == "assistant":
        return bool((row["content"] or "").strip()) or any(not is_heartbeat(n) for n in tool_names(row))
    if row["role"] == "tool":
        return not is_heartbeat(row["tool_name"])
    return False


def hhmm(epoch: float | None) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch)) if epoch else "-"


def card_line(card: dict) -> str:
    run = card.get("run") or {}
    model = run.get("model_used") or "?"
    if run.get("fallback_from"):
        model += f" (fallback from {run['fallback_from']})"
    return (
        f"{card['id']} [{card['status']}] {card['title'][:90]} | {card.get('assignee') or 'unassigned'}"
        f" on {model} | started {hhmm(card.get('started_at'))}"
    )


def session_store(assignee: str | None) -> Path | None:
    if not assignee:
        return None
    path = HERMES / "profiles" / assignee / "state.db"
    return path if path.exists() else None


def find_session(store: Path, task: str) -> str | None:
    with connect(store) as conn:
        row = conn.execute(
            "select id from sessions where source = 'kanban' and lower(title) = ?"
            " order by started_at desc limit 1",
            (f"work kanban task {task}",),
        ).fetchone()
    return row["id"] if row else None


def render(msg: sqlite3.Row) -> str | None:
    stamp = hhmm(msg["timestamp"])
    role = msg["role"]
    text = (msg["content"] or "").strip().replace("\n", " ")
    if role == "assistant":
        lines = []
        if text:
            lines.append(f"[{stamp}] agent: {text[:SNIP]}")
        for call in json.loads(msg["tool_calls"] or "[]"):
            fn = call.get("function", call)
            args = str(fn.get("arguments", ""))[:160].replace("\n", " ")
            lines.append(f"[{stamp}]   -> {fn.get('name', '?')} {args}")
        return "\n".join(lines) or None
    if role == "tool":
        return f"[{stamp}]   <- {msg['tool_name'] or 'tool'}: {text[:200]}"
    return None


class FollowOutput:
    """Bounded output, reserving space for the exit status and overflow count.

    Header and verbose lines stream as they arrive. Compact tool calls are held until
    finish(), so the cap keeps the newest calls and always leaves room for the latest
    assistant text and the collapsed count; the oldest calls go into the overflow count.
    """

    STATUS_RESERVE = 128

    def __init__(self, max_bytes: int | None):
        self.max_bytes = max_bytes
        self.used = 0
        self.hidden = 0
        self.assistants = 0
        self.latest = None
        self.calls: list[str] = []
        self.quiet = False

    @staticmethod
    def size(line: str) -> int:
        return len((line + "\n").encode("utf-8"))

    def put(self, line: str):
        print(line, flush=True)
        self.used += self.size(line)

    def emit(self, text: str):
        if self.max_bytes is None:
            print(text, flush=True)
            return
        for line in text.splitlines():
            if not self.hidden and self.used + self.size(line) <= self.max_bytes - self.STATUS_RESERVE:
                self.put(line)
            else:
                self.hidden += 1

    def hold(self, line: str):
        """A compact line that may be dropped (oldest first) when the cap is hit."""
        self.calls.append(line)

    def compact(self, rows: list[sqlite3.Row]):
        for row in rows:
            if row["role"] != "assistant":
                continue
            text = (row["content"] or "").strip().replace("\n", " ")
            if text:
                self.assistants += 1
                self.latest = f"[{hhmm(row['timestamp'])}] agent: {text[:300]}"
            for call in json.loads(row["tool_calls"] or "[]"):
                fn = call.get("function", call)
                if is_heartbeat(fn.get("name")):
                    continue
                args = str(fn.get("arguments", "")).replace("\n", " ")[:80]
                self.hold(f"→ {fn.get('name', '?')} {args}")

    def flush_compact(self):
        collapsed = f"… {self.assistants - 1} earlier assistant messages collapsed" if self.assistants > 1 else None
        if self.max_bytes is None:
            for line in self.calls + [collapsed, self.latest]:
                if line:
                    print(line, flush=True)
            return
        room = self.max_bytes - self.STATUS_RESERVE - self.used
        latest = self.latest
        latest_room = room
        # Long isolated HOME/TMP paths can use the header budget. Shorten the
        # latest text before sacrificing the newest call, when both can fit.
        if latest and self.calls and self.size(self.calls[-1]) + 48 <= room:
            latest_room -= self.size(self.calls[-1])
            if collapsed and self.size(collapsed) + 48 <= latest_room:
                latest_room -= self.size(collapsed)
        if latest and self.size(latest) > latest_room:
            raw = latest.encode("utf-8")
            latest = raw[:max(latest_room - 4, 0)].decode("utf-8", errors="ignore") + "…" if latest_room >= 48 else None
            if latest is None:
                self.hidden += 1
        room -= self.size(latest) if latest else 0
        if collapsed and self.size(collapsed) > room:
            collapsed, self.hidden = None, self.hidden + 1
        room -= self.size(collapsed) if collapsed else 0
        kept: list[str] = []
        for line in reversed(self.calls):
            if self.size(line) > room:
                break
            kept.append(line)
            room -= self.size(line)
        self.hidden += len(self.calls) - len(kept)
        for line in reversed(kept):
            self.put(line)
        for line in (collapsed, latest):
            if line:
                self.put(line)

    def finish(self, status: str):
        self.flush_compact()
        # The quiet warning is a control signal, not droppable worker output.
        # Put its short line in the reserved status budget even with a tiny cap.
        if self.quiet:
            status = "no new worker messages\n" + status
        if self.max_bytes is None:
            print(status, flush=True)
            return
        footer = f"… {self.hidden} lines not shown (the cursor has moved past them)" if self.hidden else ""
        available = self.max_bytes - self.used - len((footer + "\n").encode("utf-8")) - 1
        raw = status.encode("utf-8")
        if len(raw) > available:
            status = raw[:available - 3].decode("utf-8", errors="ignore") + "…"
        print(status, flush=True)
        if footer:
            print(footer, flush=True)


def cursor_path(board: str, task: str, key: str = "") -> Path:
    suffix = f"-{key}" if key else ""
    return CURSOR_DIR / f"{board or 'default'}-{task}{suffix}.cursor"


def new_messages(store: Path, session: str, after: int) -> list[sqlite3.Row]:
    with connect(store) as conn:
        return conn.execute(
            "select id, role, content, tool_calls, tool_name, timestamp from messages"
            " where session_id = ? and id > ? and active = 1 order by id",
            (session, after),
        ).fetchall()


def cmd_snapshot(args) -> int:
    code = 0
    for task in args.task:
        card = read_card(args.board, task)
        if card is None:
            print(json.dumps({"read_status": "OK", "now": int(time.time()), "id": task,
                              "status": "NOT_FOUND", "retried": False, "warnings": {}}))
            code = 4
        else:
            with connect(board_db(args.board)) as conn:
                card["retried"] = was_retried(conn, task)
            print(json.dumps({**card, "read_status": "OK", "now": int(time.time())},
                             ensure_ascii=False, separators=(",", ":")))
    return code


def was_retried(conn: sqlite3.Connection, task: str) -> bool:
    """Conservative dependency-handoff guard, including an unblock before the next run."""
    return bool(conn.execute(
        "select (select count(*) from (select 1 from task_runs where task_id = ? limit 2)) > 1"
        " or exists(select 1 from task_events where task_id = ?"
        " and kind in ('blocked', 'unblocked', 'gave_up', 'timed_out', 'crashed', 'changes_requested'))",
        (task, task),
    ).fetchone()[0])


def graph_card(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    card = {k: row[k] for k in ("id", "title", "status", "assignee")}
    card["retried"] = was_retried(conn, row["id"])
    card["runs"] = conn.execute("select count(*) from task_runs where task_id = ?", (row["id"],)).fetchone()[0]
    if "created_at" in row.keys():
        card["created_at"] = row["created_at"]
    card["title"] = card["title"][:120]
    card["parents"] = [r[0] for r in conn.execute(
        "select parent_id from task_links where child_id = ? order by parent_id", (row["id"],))]
    created = conn.execute(
        "select payload from task_events where task_id = ? and kind = 'created' order by id limit 1",
        (row["id"],),
    ).fetchone()
    try:
        card["creator"] = (json.loads(created[0]) or {}).get("creator_task_id") if created and created[0] else None
    except (ValueError, TypeError, AttributeError):
        card["creator"] = None
    run = conn.execute(
        "select model_used, fallback_from from task_runs where task_id = ? order by id desc limit 1",
        (row["id"],),
    ).fetchone()
    card["model"] = (run["model_used"] or "") if run else ""
    if run and run["fallback_from"]:
        card["model"] += f" (fallback from {run['fallback_from']})"
    return card


def board_cards(board: str, tasks: list[str] | None) -> list[dict]:
    with connect(board_db(board)) as conn:
        if tasks:
            rows = conn.execute(
                f"select id, title, status, assignee, created_at from tasks where id in ({','.join('?' * len(tasks))})",
                tasks,
            ).fetchall()
        else:
            live = sorted(QUEUED | {"running"})
            rows = conn.execute(
                f"select id, title, status, assignee, created_at from tasks where status in ({','.join('?' * len(live))})"
                " order by created_at",
                live,
            ).fetchall()
        return [graph_card(conn, r) for r in rows]


def cmd_cards(args) -> int:
    cards = board_cards(args.board, args.task)
    found = {c["id"] for c in cards}
    missing = [t for t in (args.task or []) if t not in found]
    now = int(time.time())
    # Explicit ids express intent. Only a bare board watch hides old idle cards. Age is the
    # card's last change (as for the ready-stale check), so an old card that was promoted or
    # re-queued recently is not hidden.
    skipped: list[str] = []
    if not args.task and args.stale_days:
        with connect(board_db(args.board)) as conn:
            skipped = [c["id"] for c in cards
                       if c["status"] in QUEUED and c.get("created_at") is not None
                       and ready_since(conn, c["id"], c["created_at"]) < now - args.stale_days * 86400]
    cards = [c for c in cards if c["id"] not in set(skipped)]
    print(json.dumps({"read_status": "OK", "now": now, "cards": cards, "count": len(cards), "missing": missing,
                      "stale_skipped": skipped, "stale_days": args.stale_days}))
    return 4 if missing else 0


def run_duration(run: dict, now: int) -> int | float | None:
    """Elapsed wall time, not billed tokens or money. Missing timing stays unknown."""
    start = run.get("started_at")
    end = run.get("ended_at")
    if end is None and run.get("status") == "running":
        end = now
    if start is None or end is None:
        return None
    return max(0, end - start)


def report_text(cards: list[dict], totals: dict, missing: list[str]):
    fields = ("id", "status", "assignee", "model_used", "provider_used", "fallback_from",
              "runs", "retried", "duration_s", "warnings", "title")
    rows = [["ID", "STATUS", "ASSIGNEE", "MODEL_USED", "PROVIDER_USED", "FALLBACK_FROM",
             "RUNS", "RETRIED", "DURATION_S", "WARNINGS", "TITLE"]]
    for card in cards:
        values = {**card, "retried": "yes" if card["retried"] else "no",
                  "warnings": ",".join(f"{k}={v}" for k, v in card["warnings"].items()) or "-"}
        # A title or assignee must not introduce another table row or terminal control.
        rows.append([" ".join("".join(c if c.isprintable() else " " for c in str(values[f])).split())
                     if values[f] is not None else "-" for f in fields])
    widths = [max(len(row[i]) for row in rows) for i in range(len(fields))]
    for row in rows:
        print("  ".join(value.ljust(width) for value, width in zip(row, widths)).rstrip())
    print("Totals: " + " ".join(f"{k}={v}" for k, v in totals["by_status"].items())
          + f"  run_seconds={totals['run_seconds']}  fell_back={totals['fell_back']}  retried={totals['retried']}")
    if missing:
        print("Missing: " + ", ".join(missing))


def cmd_report(args) -> int:
    now = int(time.time())
    cards = []
    totals = {"by_status": {}, "run_seconds": 0, "fell_back": 0, "retried": 0}
    with connect(board_db(args.board)) as conn:
        conn.execute("begin")
        filters, params = [], []
        if args.task:
            filters.append(f"id in ({','.join('?' * len(args.task))})")
            params.extend(args.task)
        if args.since is not None:
            filters.append("created_at >= ?")
            params.append(args.since)
        where = " where " + " and ".join(filters) if filters else ""
        rows = conn.execute("select id, title, status, assignee from tasks" + where
                            + " order by created_at, id", params).fetchall()
        for row in rows:
            runs = [dict(r) for r in conn.execute(
                "select id, status, model_used, provider_used, fallback_from, started_at, ended_at"
                " from task_runs where task_id = ? order by id", (row["id"],))]
            last = runs[-1] if runs else None
            card = {**dict(row), **{k: last[k] if last else None
                                    for k in ("model_used", "provider_used", "fallback_from")},
                    "runs": len(runs), "retried": was_retried(conn, row["id"]),
                    "duration_s": run_duration(last, now) if last else None,
                    "warnings": run_warnings(conn, row["id"], last)}
            cards.append(card)
            totals["by_status"][row["status"]] = totals["by_status"].get(row["status"], 0) + 1
            totals["run_seconds"] += sum(run_duration(run, now) or 0 for run in runs)
            totals["fell_back"] += int(any(run["fallback_from"] for run in runs))
            totals["retried"] += int(card["retried"])
        # A since-filtered existing card is not missing; missing means absent from the board.
        missing = [task for task in dict.fromkeys(args.task or []) if not conn.execute(
            "select 1 from tasks where id = ?", (task,)).fetchone()]
    if args.format == "text":
        report_text(cards, totals, missing)
    else:
        print(json.dumps({"read_status": "OK", "now": now, "cards": cards, "totals": totals,
                          "missing": missing}, ensure_ascii=False))
    return 4 if missing else 0


def cmd_verify_pair(args) -> int:
    pair = {}
    with connect(board_db(args.board)) as conn:
        conn.execute("begin")
        for role, task in (("worker", args.worker), ("verifier", args.verifier)):
            run = conn.execute(
                "select id as run_id, model_used, provider_used from task_runs"
                " where task_id = ? and ended_at is not null order by id desc limit 1", (task,)).fetchone()
            pair[role] = {"id": task, **dict(run), "family": None} if run else {"id": task, "family": None}
    out = {"read_status": "OK", **pair, "different_families": None}
    if any("run_id" not in run for run in pair.values()):
        print(json.dumps({**out, "error": "A card has no finished run"}))
        return 3
    try:
        # Reuse exactly the router's file precedence and dependency-free YAML/JSON parser.
        spec = importlib.util.spec_from_file_location(
            "_kanban_pair_routes", Path(__file__).resolve().with_name("route.py"))
        if spec is None or spec.loader is None:
            raise ImportError("no routes loader")
        route = importlib.util.module_from_spec(spec)
        with contextlib.redirect_stdout(sys.stderr):
            spec.loader.exec_module(route)
        providers = route.read_mapping(route.routes_path(args.families)).get("providers")
        if not isinstance(providers, dict) or not providers or any(
                not route.safe_identifier(k) or not route.safe_identifier(v) for k, v in providers.items()):
            raise ValueError("invalid providers")
        for run in pair.values():
            if not run["model_used"] or not run["provider_used"]:
                raise ValueError("missing actual model/provider")
            run["family"] = providers[run["provider_used"]]
    except (OSError, UnicodeError, ValueError, KeyError, ImportError):
        # Do not echo a routes file or parser excerpts, which might contain a pasted key.
        print(json.dumps({**out, "error": "Cannot classify actual runs: check model/provider and routes providers mapping"}))
        return 1
    different = pair["worker"]["family"] != pair["verifier"]["family"]
    print(json.dumps({**out, "different_families": different}))
    return 0 if different else 1


def cmd_wait_any(args) -> int:
    """One read transaction per poll, no per-card process or cursor. Terminal exits count too."""
    tasks = list(dict.fromkeys(args.task))
    deadline = time.monotonic() + args.max_seconds
    while True:
        try:
            with connect(board_db(args.board)) as conn:
                conn.execute("begin")
                rows = conn.execute(
                    f"select id, title, status, assignee, created_at from tasks"
                    f" where id in ({','.join('?' * len(tasks))})", tasks).fetchall()
                found = {r["id"] for r in rows}
                started = [graph_card(conn, r) for r in rows if r["status"] not in QUEUED]
                idle = [r["id"] for r in rows if r["status"] == "ready" and args.ready_stale
                        and time.time() - ready_since(conn, r["id"], r["created_at"]) >= args.ready_stale]
        except sqlite3.Error as exc:
            return observation_error(exc)
        missing = [t for t in tasks if t not in found]
        out = {"read_status": "OK", "now": int(time.time()),
               "started": [c["id"] for c in started], "cards": started,
               "missing": missing, "not_starting": idle}
        # A start must never be hidden by a missing sibling: the caller reads `missing` either way.
        code = 0 if started else 4 if missing else 6 if idle else None
        if code is None and time.monotonic() >= deadline:
            out["timeout"] = True
            code = 3
        if code is not None:
            print(json.dumps(out))
            return code
        time.sleep(min(args.interval, max(0, deadline - time.monotonic())))


def cmd_events(args) -> int:
    watch = dict(pair.split("=", 1) for pair in args.watch.split(",") if "=" in pair)
    # Every card the caller already has, watched or not: a known child is never "new" again.
    known = set(watch) | {t for t in args.known.split(",") if t}
    deadline = time.time() + args.max_seconds
    while True:
        try:
            with connect(board_db(args.board)) as conn:
                current = board_cards(args.board, list(watch))
                fresh_rows = conn.execute(
                    "select id, title, status, assignee from tasks where created_at >= ? and status != 'archived'",
                    (args.since,),
                ).fetchall()
                fresh = [graph_card(conn, r) for r in fresh_rows if r["id"] not in known]
        except sqlite3.Error as exc:
            return observation_error(exc)
        # A start is reported but never ends the call: each queued card's own wait row
        # already tracks it, and ending here would spend a caller's round on every start.
        resumed = [c for c in current if watch.get(c["id"]) in TERMINAL - FINAL
                   and c["status"] in QUEUED | {"running"}]
        # A watched live card that stopped (blocked or review) mid-round: the caller's watch list is
        # stale, so end the call and let the next round watch it as parked.
        stopped = [c for c in current if watch.get(c["id"]) in QUEUED | {"running"}
                   and c["status"] in TERMINAL - FINAL]
        # A parked card that was approved or archived without ever running again: report, no early exit.
        settled = [c for c in current if watch.get(c["id"]) in TERMINAL - FINAL and c["status"] in FINAL]
        changed = [c for c in current if watch.get(c["id"]) in QUEUED and c["status"] not in QUEUED
                   or c in resumed or c in stopped or c in settled]
        new = [c for c in fresh if c["creator"] in watch or set(c["parents"]) & set(watch)]
        out = {"read_status": "OK", "now": int(time.time()), "changed": changed, "new": new,
               "count_changed": len(changed), "count_new": len(new)}
        if new or resumed or stopped:
            print(json.dumps(out))
            return 0
        if current and len(current) == len(watch) and all(c["status"] in FINAL for c in current):
            print(json.dumps({**out, "all_finished": True}))
            return 2
        if time.time() >= deadline:
            print(json.dumps({**out, "timeout": True}))
            return 3
        time.sleep(args.interval)


def cmd_active(args) -> int:
    live = sorted(QUEUED | {"running"})
    with connect(board_db(args.board)) as conn:
        rows = conn.execute(
            f"select id, title, status, assignee from tasks where status in ({','.join('?' * len(live))})"
            " order by created_at",
            live,
        ).fetchall()
    print(json.dumps({"now": int(time.time()), "cards": [dict(r) for r in rows]}))
    return 0


def ready_since(conn: sqlite3.Connection, task: str, created_at: int | None) -> int:
    """When the card last changed (its newest non-heartbeat event), so a ready card's idle time."""
    row = conn.execute(
        "select max(created_at) from task_events where task_id = ? and kind != 'heartbeat'", (task,)
    ).fetchone()
    return int((row[0] if row and row[0] else created_at) or 0)


def dispatcher_state(board: str) -> str:
    """Whether a board runner is active; informational only (a manual pass also counts)."""
    try:
        out = subprocess.run(["systemctl", "--user", "is-active", f"hermes-kanban-board@{board}"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def cmd_wait_start(args) -> int:
    deadline = time.time() + args.max_seconds
    last = None
    while True:
        card = read_card(args.board, args.task)
        if card is None:
            print(f"{args.task}: NOT_FOUND on board {args.board}")
            return 4
        capped = None
        if card["status"] == "ready":
            with connect(board_db(args.board)) as conn:
                capped = cap_label(conn, args.task)
        label = capped["label"] if capped else ""
        # The label carries a minute counter; only a real change in the cap facts reprints.
        state = (card["status"], capped and (capped["scope"], capped["assignee"], capped["running"],
                                            capped["cap"], capped["alerted"]))
        if state != last:
            print(card_line(card) + (f" | {label}" if label else ""), flush=True)
            last = state
        if card["status"] not in QUEUED:
            return 0
        if card["status"] == "ready" and args.ready_stale:
            with connect(board_db(args.board)) as conn:
                idle = time.time() - ready_since(conn, args.task, card.get("created_at"))
            if idle >= args.ready_stale:
                print(f"NOT STARTING: {args.task} has been ready {int(idle // 60)} min and no dispatcher took it"
                      f" (board runner hermes-kanban-board@{args.board}: {dispatcher_state(args.board)})", flush=True)
                # One exact token for a watcher agent to copy (review: "NOT STARTING" read as a start).
                print("status NOT_STARTING", flush=True)
                return 6
        if time.time() >= deadline:
            print(f"still {card['status']} - call again", flush=True)
            return 3
        time.sleep(args.interval)


def cmd_follow(args) -> int:
    deadline = time.time() + args.max_seconds
    output = FollowOutput(args.max_bytes if args.max_bytes is not None else (1500 if args.compact else None))
    cpath = cursor_path(args.board, args.task, args.cursor_key)
    cpath.parent.mkdir(parents=True, exist_ok=True)
    after = int(cpath.read_text()) if cpath.exists() and not args.from_start else 0
    card = read_card(args.board, args.task)
    if card is None:
        output.finish(f"{args.task}: NOT_FOUND on board {args.board}")
        return 4
    output.emit(card_line(card))
    store = session_store(card.get("assignee"))
    session = None
    quiet_since = time.time()

    def finish(status: str):
        output.finish(status)
        # Compact messages stay buffered until finish. A killed call must leave
        # the saved cursor behind them so the next call can replay the batch.
        if args.compact:
            cpath.write_text(str(after))

    while True:
        if store and session is None:
            session = find_session(store, args.task)
            if session and after == 0:
                output.emit(f"session {session} in {store}")
        if store and session:
            rows = new_messages(store, session, after)
            if rows and after == 0 and len(rows) > args.backlog:
                output.emit(f"... {len(rows) - args.backlog} earlier messages skipped")
                rows = rows[-args.backlog:]
            if args.compact:
                output.compact(rows)
            else:
                for row in rows:
                    line = render(row)
                    if line:
                        output.emit(line)
            if rows:
                after = rows[-1]["id"]
                if not args.compact:
                    cpath.write_text(str(after))
            # A heartbeat alone does not prove progress: a wedged worker keeps heartbeating.
            if any(is_progress(row) for row in rows):
                quiet_since = time.time()
                # The worker spoke after an earlier quiet spell in this call, so the call is not quiet.
                output.quiet = False
        previous_run = (card.get("run") or {}).get("id")
        card = read_card(args.board, args.task) or card
        if card["status"] in TERMINAL:
            finish("FINAL " + card_line(card))
            return 0
        if (card.get("run") or {}).get("id") != previous_run:
            # A crash or reclaim started a new run, which writes to a new session.
            line = f"[{hhmm(time.time())}] new run: " + card_line(card)
            if args.compact:
                output.hold(line)
            else:
                output.emit(line)
            session, after = None, 0
            if not args.compact:
                cpath.write_text("0")
        if time.time() - quiet_since > args.quiet_warn:
            if args.compact:
                output.quiet = True
            else:
                output.emit(f"[{hhmm(time.time())}] no new worker messages for {int(time.time() - quiet_since)}s"
                            f" (heartbeat {hhmm(card.get('last_heartbeat_at'))})")
            quiet_since = time.time()
        if time.time() >= deadline:
            finish(f"[{hhmm(time.time())}] still {card['status']} - call again")
            return 3
        time.sleep(args.interval)


def wait_in_force(conn: sqlite3.Connection, task: str, event_id: int) -> bool:
    """A wait still holds while its card waits and nothing after it claimed, freed or ended the card."""
    row = conn.execute("select status from tasks where id = ?", (task,)).fetchone()
    if row is None or row["status"] not in WAITING:
        return False
    # A later wait of any reason replaces this one, so only the card's newest wait can still be in force.
    ends = (*WAIT_ENDS, "blocked", "dependency_wait")
    marks = ",".join("?" * len(ends))
    return conn.execute(f"select 1 from task_events where task_id = ? and id > ? and kind in ({marks}) limit 1",
                        (task, event_id, *ends)).fetchone() is None


def read_seen(path: Path) -> dict[str, float]:
    """{key: epoch reported}. A missing, cut-short or odd file counts as empty (one repeat, never a crash)."""
    try:
        raw = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError) as exc:
        print(f"needs: ignoring unreadable seen file {path}: {exc}", file=sys.stderr)
        return {}
    now = time.time()
    if isinstance(raw, list):  # an older list-shaped file counts as reported now
        return {k: now for k in raw if isinstance(k, str)}
    if isinstance(raw, dict):
        seen = {}
        for k, v in raw.items():
            if not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            try:
                v = float(v)
            except OverflowError:
                continue
            if math.isfinite(v) and 0 <= v <= now + 86400:  # NaN, inf or a far-future time would silence a key
                seen[k] = v
        return seen
    return {}


def write_seen(path: Path, seen: dict[str, float]):
    """Write a temp file and rename it over the old one, so a kill mid-write leaves the old file whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(seen, sort_keys=True))
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def needs_items(board: str, since: int, ready_stale: int, ready_stale_capped: int = 0,
                *, created_by: str | None = None, loop_runs: int = 6,
                include_waiting_before: bool = False) -> list[dict]:
    items = []
    if ready_stale and dispatcher_state(board) == "active":
        # A running board runner at its --max makes ready cards wait for a slot; that is normal.
        ready_stale = ready_stale_capped
    with connect(board_db(board)) as conn:
        try:
            for r in conn.execute(
                    "select id, task_id, body, created_at from task_comments where created_at >= ? order by id",
                    (since,)):
                if ASK.search(r["body"] or ""):
                    items.append({"key": f"comment:{r['id']}", "kind": "ask_operator", "task": r["task_id"],
                                  "at": r["created_at"], "text": (r["body"] or "")[:2000]})
        except sqlite3.OperationalError:
            pass  # an older board without a comments table
        # Older boards need no created_by column unless the caller opts in, and an opted-in
        # call on a board without the column adds no parent_question items rather than failing.
        has_creator = created_by is not None and any(
            c["name"] == "created_by" for c in conn.execute("pragma table_info(tasks)"))
        creator_column = "t.created_by" if has_creator else "NULL"
        parent: dict[str, dict] = {}  # one item per card; the newest event's wording wins
        for r in conn.execute(
                f"select e.id, e.task_id, e.kind, e.payload, e.created_at, t.title, t.status,"
                f" {creator_column} as created_by"
                " from task_events e left join tasks t on t.id = e.task_id"
                " where (e.created_at >= ? or ?) and e.kind in ('blocked', 'dependency_wait', 'block_loop_detected')"
                " order by e.id", (since, include_waiting_before)):
            # Only bootstrap standing waits, not old questions already answered or superseded.
            if r["created_at"] < since and not wait_in_force(conn, r["task_id"], r["id"]):
                continue
            try:
                reason = (json.loads(r["payload"] or "{}") or {}).get("reason") or ""
            except (ValueError, AttributeError):
                reason = ""
            if not isinstance(reason, str):
                reason = str(reason)
            if r["kind"] == "block_loop_detected":
                # Hermes parks a re-blocking card in triage "for a human", whatever its reason says.
                items.append({"key": f"parked:{r['task_id']}:{r['id']}", "kind": "parked_for_human",
                              "task": r["task_id"], "at": r["created_at"],
                              "title": (r["title"] or "")[:120], "status": r["status"],
                              "text": reason[:2000]})
                if has_creator and r["created_by"] == created_by:
                    parent.pop(r["task_id"], None)  # the parked item supersedes an older parent item
                continue
            if has_creator and r["created_by"] == created_by:
                # Do not gate the first report on status or wait_in_force: the Hermes respawn
                # loop follows each wait within seconds with promoted, claimed and spawned events.
                # Keyed by card, not reason: a looping card rewords its reason on nearly every run
                # (55 wordings across 57 waits on the live board), so a reason key would wake the
                # parent on every respawn. The latest reason is the item's text.
                parent[r["task_id"]] = {"key": f"parent:{r['task_id']}", "kind": "parent_question",
                                        "task": r["task_id"], "at": r["created_at"],
                                        "title": (r["title"] or "")[:120], "status": r["status"],
                                        "text": reason[:2000]}
            if NAMES_OPERATOR.search(reason) and wait_in_force(conn, r["task_id"], r["id"]):
                # Keyed by card, not event: a waiting lane re-runs and re-states the same wait.
                items.append({"key": f"waiting:{r['task_id']}", "kind": "waiting_on_operator", "task": r["task_id"],
                              "at": r["created_at"], "text": reason[:2000]})
        items.extend(parent.values())
        if loop_runs and conn.execute(
                "select 1 from sqlite_master where type = 'table' and name = 'task_runs'").fetchone():
            now = time.time()
            creator_filter = " and t.created_by = ?" if has_creator else ""
            params = (max(since, now - 3600), now, *([created_by] if has_creator else []), loop_runs)
            for r in conn.execute(
                    "select t.id, t.title, t.status, count(*) as run_count, max(r.started_at) as newest"
                    " from task_runs r join tasks t on t.id = r.task_id"
                    " where r.started_at >= ? and r.started_at <= ?" + creator_filter +
                    " group by t.id having count(*) >= ?", params):
                items.append({"key": f"loop:{r['id']}", "kind": "respawn_loop", "task": r["id"],
                              "at": r["newest"], "title": (r["title"] or "")[:120], "status": r["status"],
                              "text": f"{r['run_count']} runs in the last 60 min"})
        if ready_stale:
            now = time.time()
            for r in conn.execute("select id, title, created_at from tasks where status = 'ready'"):
                since_ready = ready_since(conn, r["id"], r["created_at"])
                if now - since_ready >= ready_stale:
                    items.append({"key": f"ready:{r['id']}:{since_ready}", "kind": "not_started", "task": r["id"],
                                  "at": since_ready,
                                  "text": f"{r['title'][:120]} - ready {int((now - since_ready) // 60)} min, not started"})
    return items


def cmd_needs(args) -> int:
    seen_path = CURSOR_DIR / f"{args.board or 'default'}-needs{('-' + args.cursor_key) if args.cursor_key else ''}.seen"
    seen = read_seen(seen_path)
    deadline = time.time() + args.max_seconds
    first_poll = True
    while True:
        now = time.time()
        # waiting: keys repeat at most once per WAIT_REPEAT per card. loop: keys do too, while
        # unfinished and still meeting the run threshold. parent: keys do too, but only
        # while the card is unfinished AND it waited again after the last report: a card answered
        # once and now running normally must not wake its parent every hour. The first report
        # (key not seen) is never status-gated.
        def fresh(item):
            key = item["key"]
            if key not in seen:
                return True
            if now - seen[key] < WAIT_REPEAT:
                return False
            if key.startswith("waiting:"):
                return True
            if key.startswith("loop:"):
                return item.get("status") not in ("done", "archived")
            return (key.startswith("parent:") and item.get("status") not in ("done", "archived")
                    and item["at"] > seen[key])
        try:
            items = [i for i in needs_items(args.board, args.since, args.ready_stale, args.ready_stale_capped,
                                           created_by=args.created_by, loop_runs=args.loop_runs,
                                           include_waiting_before=args.loop and first_poll)
                     if fresh(i)]
            items = list({i["key"]: i for i in items}.values())
        except sqlite3.Error as exc:
            print(json.dumps({"error": f"board unreadable: {exc}"}))
            return 5
        first_poll = False
        if items:
            seen.update({i["key"]: now for i in items})
            write_seen(seen_path, seen)
            out = {"now": int(time.time()), "items": items}
            if any(i["kind"] == "not_started" for i in items):
                # An active runner at its --max is a slot wait, not a missing dispatcher.
                out["dispatcher"] = dispatcher_state(args.board)
                with connect(board_db(args.board)) as conn:
                    out["running"] = conn.execute("select count(*) from tasks where status = 'running'").fetchone()[0]
            print(json.dumps(out))
            return 0
        if time.time() >= deadline:
            if not args.loop:
                print(json.dumps({"now": int(time.time()), "items": [], "timeout": True}))
                return 3
            # Own the exit-3 retry, but never move args.since or retry any other exit.
            deadline = time.time() + args.max_seconds
        time.sleep(args.interval)


def cmd_scout(args) -> int:
    known = {t for t in args.known.split(",") if t}
    deadline = time.time() + args.max_seconds
    while True:
        try:
            with connect(board_db(args.board)) as conn:
                rows = conn.execute(
                    "select id, title, status, assignee from tasks where created_at >= ?"
                    " and status != 'archived' order by created_at",
                    (args.since,),
                ).fetchall()
                states = {
                    r["id"]: r["status"]
                    for r in conn.execute(
                        f"select id, status from tasks where id in ({','.join('?' * len(known))})",
                        tuple(known),
                    )
                } if known else {}
        except sqlite3.Error as exc:
            print(f"board unreadable: {exc}")
            return 5
        fresh = [dict(r) for r in rows if r["id"] not in known]
        if fresh:
            print(json.dumps({"new": fresh}))
            return 0
        if known and all(states.get(t) in TERMINAL for t in known):
            print(json.dumps({"new": [], "all_terminal": True}))
            return 2
        if time.time() >= deadline:
            print(json.dumps({"new": [], "timeout": True}))
            return 3
        time.sleep(args.interval)


def observation_error(exc) -> int:
    """No card facts on a failed read. Exit 5 is observation UNKNOWN, never missing."""
    print(json.dumps({"read_status": "UNKNOWN", "error": f"board unreadable: {exc}"}))
    return 5


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    manifest = Path(__file__).resolve().parents[1] / ".claude-plugin/plugin.json"
    version = json.loads(manifest.read_text(encoding="utf-8"))["version"]
    parser.add_argument("--version", action="version", version=f"hermes-kanban-watch {version}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p, many=False):
        p.add_argument("--board", default="default")
        if many:
            p.add_argument("--task", action="append", required=True)
        p.add_argument("--max-seconds", type=int, default=540)
        p.add_argument("--interval", type=int, default=15)

    p = sub.add_parser("snapshot")
    common(p, many=True)
    p = sub.add_parser("cards")
    p.add_argument("--board", default="default")
    p.add_argument("--task", action="append")
    p.add_argument("--stale-days", type=float, default=7,
                   help="skip older queued cards on unscoped board watches; 0 disables (explicit ids always kept)")
    p = sub.add_parser("report")
    p.add_argument("--board", default="default")
    p.add_argument("--task", action="append")
    p.add_argument("--since", type=int, help="include cards created at or after this epoch")
    p.add_argument("--format", choices=("json", "text"), default="json")
    p = sub.add_parser("verify-pair")
    p.add_argument("--board", default="default")
    p.add_argument("--worker", required=True)
    p.add_argument("--verifier", required=True)
    p.add_argument("--families", help="routes file with a providers-to-model-family mapping")
    p = sub.add_parser("events")
    common(p)
    p.add_argument("--watch", required=True, help="id=status,id=status as last seen")
    p.add_argument("--known", default="", help="every other card id already watched; never reported as new")
    p.add_argument("--since", type=int, required=True)
    p = sub.add_parser("active")
    p.add_argument("--board", default="default")
    p = sub.add_parser("wait-start")
    common(p)
    p.add_argument("--task", required=True)
    p.add_argument("--ready-stale", type=int, default=0,
                   help="exit 6 once the card has sat ready this many seconds (0 = never)")
    p = sub.add_parser("wait-any")
    common(p, many=True)
    p.set_defaults(max_seconds=270)
    p.add_argument("--ready-stale", type=int, default=0,
                   help="exit 6 with not_starting ids after this ready idle time; 0 disables")
    p = sub.add_parser("follow")
    common(p)
    p.add_argument("--task", required=True)
    p.add_argument("--backlog", type=int, default=15, help="first call: show at most this many earlier messages")
    p.add_argument("--quiet-warn", type=int, default=600, help="say so after this many seconds with no new message")
    p.add_argument("--from-start", action="store_true", help="ignore the saved cursor")
    p.add_argument("--cursor-key", default="", help="separate cursor per watcher, so two watchers never share one")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--compact", action="store_true", help="tool calls and latest assistant text only; default cap 1500 bytes")
    mode.add_argument("--verbose", action="store_true", help="legacy worker text, tool calls and results (default)")
    p.add_argument("--max-bytes", type=int, help="cap all follow output, including status and overflow count (minimum 128)")
    p = sub.add_parser("scout")
    common(p)
    p.add_argument("--known", default="")
    p.add_argument("--since", type=int, required=True)
    p = sub.add_parser("needs")
    common(p)
    p.add_argument("--since", type=int, help="only comments, events and runs from this epoch on; --loop defaults to now")
    p.add_argument("--loop", action="store_true", help="retry timeouts with a fixed since; first poll includes existing waits")
    p.add_argument("--created-by", help="also wake for this creator's blocked/waiting cards: once per card, then hourly"
                        " while it is unfinished and still waiting again; filter respawn loops to this creator")
    p.add_argument("--loop-runs", type=int, default=6,
                   help="flag a card with this many runs started in the last hour (0 = off)")
    p.add_argument("--ready-stale", type=int, default=900,
                   help="flag a card ready this many seconds and not started (0 = off)")
    p.add_argument("--ready-stale-capped", type=int, default=3600,
                   help="the same, while the board runner is active (slot waits are normal; 0 = off)")
    p.add_argument("--cursor-key", default="", help="separate seen-list per waker")

    args = parser.parse_args(argv)
    if args.cmd == "needs":
        if args.since is None:
            if not args.loop:
                parser.error("needs requires --since without --loop")
            args.since = int(time.time())
        if args.loop and (args.interval <= 0 or args.max_seconds < 0):
            parser.error("needs --loop requires --interval > 0 and --max-seconds >= 0")
    if args.cmd == "needs" and args.loop_runs < 0:
        parser.error("--loop-runs must be non-negative")
    if args.cmd == "cards" and (not math.isfinite(args.stale_days) or args.stale_days < 0):
        parser.error("--stale-days must be finite and non-negative")
    if args.cmd == "wait-any" and (not 0 <= args.max_seconds <= 270 or args.interval <= 0 or args.ready_stale < 0):
        parser.error("wait-any requires 0 <= --max-seconds <= 270, --interval > 0 and --ready-stale >= 0")
    if args.cmd == "follow" and args.max_bytes is not None and args.max_bytes < 128:
        parser.error("--max-bytes must be at least 128 to fit the status and overflow count")
    if not board_db(args.board).exists():
        return observation_error(f"board {args.board} has no kanban.db at {board_db(args.board)}")
    try:
        return {"snapshot": cmd_snapshot, "cards": cmd_cards, "events": cmd_events, "active": cmd_active, "wait-start": cmd_wait_start,
                "wait-any": cmd_wait_any,
                "follow": cmd_follow, "scout": cmd_scout, "needs": cmd_needs,
                "report": cmd_report, "verify-pair": cmd_verify_pair}[args.cmd](args)
    except sqlite3.Error as exc:
        return observation_error(exc)


if __name__ == "__main__":
    sys.exit(main())
