#!/usr/bin/env python3
"""Board-only, read-only snapshots and bounded waits. Never opens worker profiles.

Only done/archived are terminal. Cursors are local files, never board writes.
Exit 2: invalid input; exit 5: unreadable/unsupported board (UNKNOWN).
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time

FINAL = {"done", "archived"}
KNOWN = FINAL | {"triage", "todo", "ready", "scheduled", "running", "review", "blocked"}
RECOVERY = {"claimed", "spawned", "unblocked", "completed", "archived"}
READY_EVENTS = {"created", "promoted", "unblocked", "changes_requested", "reclaimed", "gave_up"}
NOISE = {"heartbeat", "capped", "capped_alerted"}
ASK = re.compile(r"^[ \t>#*_-]*ASK (?:PARENT|OPERATOR)[*_]*\s*:", re.I | re.M)
# A parent's SUPERSEDED marker: the upper-case word anywhere, or any case at a line start.
# Prose such as "not superseded yet" is not a marker.
SUPERSEDED_WORD = re.compile(r"\bSUPERSEDED\b")
SUPERSEDED_LEAD = re.compile(r"^[ \t>#*_-]*superseded\b", re.I | re.M)


def marks_superseded(text):
    return bool(text) and bool(SUPERSEDED_WORD.search(text) or SUPERSEDED_LEAD.search(text))


def is_superseded(card, events, comments):
    """SUPERSEDED in the block reason, the latest block event or a comment since work last
    resumed retires a card from problem reporting; a later claim/spawn/unblock re-arms it."""
    resumed = max((e["created_at"] for e in events if e["kind"] in RECOVERY), default=None)
    texts = [c["body"] for c in comments if resumed is None or c["created_at"] > resumed]
    if card["status"] == "blocked":
        texts.append(card.get("block_reason"))
        last = next((e for e in reversed(events) if e["kind"] in {"blocked", "gave_up"}), None)
        if last and (resumed is None or last["created_at"] >= resumed):
            texts += [payload(last).get("reason"), payload(last).get("block_reason")]
    return any(isinstance(t, str) and marks_superseded(t) for t in texts)


def model_drift(card, run):
    """Pinned model vs the model the run answered on; None while the run model is UNKNOWN."""
    used = (run or {}).get("model_used")
    if not used:
        return None
    pinned = (card.get("model_override") or "").strip()
    if pinned:
        return f"Pinned to {pinned} but the run answered on {used}" if used.strip() != pinned else None
    origin = ((run or {}).get("fallback_from") or "").strip()
    if origin and origin != used.strip():
        return f"Run fell back from {origin} to {used} (no pin recorded)"
    return None


def validate_scope(board, cards=None):
    if not isinstance(board, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", board):
        raise ValueError("Invalid board slug")
    if cards is not None and (not cards or len(set(cards)) != len(cards) or
            any(not re.fullmatch(r"t_[A-Za-z0-9_]+", c) for c in cards)):
        raise ValueError("Supply unique, exact card IDs")


def board_path(board, home=None):
    validate_scope(board)
    root = Path(home or os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()
    return root / "kanban.db" if board == "default" else root / "kanban/boards" / board / "kanban.db"


def positive(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("Time budgets must be finite and positive")
    return number


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, encoding="utf-8", delete=False) as f:
        json.dump(data, f, sort_keys=True)
        name = f.name
    os.replace(name, path)


def columns(conn, table):
    # Table names are internal constants, never supplied by the caller.
    return {row["name"] for row in conn.execute(f"pragma table_info({table})")}


def payload(event):
    try:
        data = json.loads(event.get("payload") or "{}")
        return data if isinstance(data, dict) else {}
    except (TypeError, ValueError):
        return {}


def short(text, limit=240):
    return " ".join(str(text or "").split())[:limit]


def snapshot(board, cards=None, now=None, home=None):
    """One consistent read transaction; no CLI, profile, session or process inspection."""
    validate_scope(board, cards)
    now = time.time() if now is None else now
    path = board_path(board, home).resolve(strict=True)
    problems = []
    result = []
    superseded = []
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=3)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("pragma query_only=ON")
        conn.execute("begin")
        task_cols = columns(conn, "tasks")
        run_cols = columns(conn, "task_runs")
        if not {"id", "title", "status", "assignee", "created_at"} <= task_cols or not {
                "id", "task_id", "status", "started_at", "ended_at", "model_used", "provider_used"} <= run_cols:
            raise ValueError("Unsupported board schema")
        if not {"id", "task_id", "kind", "created_at", "payload"} <= columns(conn, "task_events"):
            raise ValueError("Unsupported events schema")
        has_comments = bool(columns(conn, "task_comments"))
        fields = [c for c in ("id", "title", "status", "assignee", "created_at", "started_at", "completed_at",
                             "worker_pid", "current_run_id", "max_runtime_seconds", "block_reason",
                             "model_override") if c in task_cols]
        query = "select " + ",".join(fields) + " from tasks"
        if cards:
            query += " where id in (" + ",".join("?" for _ in cards) + ")"
        rows = [dict(r) for r in conn.execute(query + " order by id", cards or [])]
        if cards and {r["id"] for r in rows} != set(cards):
            raise ValueError("Requested card missing from this board")
        for card in rows:
            tid, status = card["id"], card["status"]
            if status not in KNOWN:
                raise ValueError("Unknown card status")
            events = [dict(r) for r in conn.execute(
                "select id,kind,payload,created_at from task_events where task_id=? order by id", (tid,))]
            run_fields = [c for c in ("id", "status", "started_at", "ended_at", "model_used", "provider_used",
                                      "fallback_from", "max_runtime_seconds", "summary") if c in run_cols]
            run_query = "select " + ",".join(run_fields) + " from task_runs where task_id=?"
            pointer = card.get("current_run_id")
            params = (tid, pointer) if pointer is not None else (tid,)
            run_row = conn.execute(run_query + (" and id=?" if pointer is not None else "") +
                                   " order by id desc limit 1", params).fetchone()
            run = dict(run_row) if run_row else None
            if run:
                started = run.get("started_at")
                run["runtime_s"] = max(0, (run.get("ended_at") or now) - started) if started is not None else None
                run["max_runtime_seconds"] = run.get("max_runtime_seconds") or card.get("max_runtime_seconds")
                run.pop("summary", None)  # Board text is used below only for a current parent wait.
            card.update(run=run, model_used=run.get("model_used") if run else None,
                        provider_used=run.get("provider_used") if run else None,
                        age_s=max(0, now - card["created_at"]))
            transitions = [e for e in events if e["kind"] in READY_EVENTS]
            ready_since = transitions[-1]["created_at"] if transitions else card["created_at"]
            card["ready_age_s"] = max(0, now - ready_since) if status == "ready" else None

            def problem(kind, text, episode: object = ""):
                key = hashlib.sha256(str(episode).encode()).hexdigest()[:16]
                problems.append({"id": f"{tid}:{kind}:{key}", "card": tid, "kind": kind, "text": short(text)})

            comments = [dict(r) for r in conn.execute(
                "select id,body,created_at from task_comments where task_id=? order by id", (tid,))] if has_comments else []
            if status not in FINAL and is_superseded(card, events, comments):
                superseded.append(tid)
                result.append(card)
                continue
            drift = model_drift(card, run)
            if drift:
                # Reported on first sight of the run's model, not at harvest; once per run and model.
                problem("model_drift", drift, (run["id"], run.get("model_used")))

            if status not in FINAL:
                if status == "blocked":
                    blocked = [e for e in events if e["kind"] in RECOVERY | {"blocked", "gave_up"}]
                    reason = card.get("block_reason")
                    if not reason and blocked and blocked[-1]["kind"] in {"blocked", "gave_up"}:
                        reason = payload(blocked[-1]).get("reason") or payload(blocked[-1]).get("block_reason")
                    problem("blocked", reason or "No block reason recorded", (blocked[-1]["id"] if blocked else "", reason))
                live_run = bool(run and run["status"] == "running" and run["ended_at"] is None)
                if status == "ready" and card["ready_age_s"] > 600 and not card.get("worker_pid") and not live_run:
                    problem("ready_no_worker", "Ready over 10 minutes; no worker recorded", ready_since)
                recovery = max((e["id"] for e in events if e["kind"] in RECOVERY), default=0)
                for e in events:
                    if e["id"] > recovery and e["kind"] in {"respawn_guarded", "spawn_failed"}:
                        problem(e["kind"], payload(e).get("reason") or e["kind"], e["id"])
                meaningful = [e for e in events if e["kind"] not in NOISE]
                if status in {"ready", "todo"} and meaningful and meaningful[-1]["kind"] == "dependency_wait" and not live_run:
                    e = meaningful[-1]
                    # A resumed run invalidates the old wait even if timestamps share a second.
                    if not run or run["started_at"] <= e["created_at"]:
                        text = payload(e).get("reason") or (run_row["summary"] if run_row and "summary" in run_cols else None)
                        problem("parent_question", text or "Worker waiting on parent; no question recorded", e["id"])
                if comments:
                    comment = comments[-1]
                    if ASK.search(comment["body"] or ""):
                        recovered_after = any(e["kind"] in RECOVERY and e["created_at"] > comment["created_at"] for e in events)
                        if not recovered_after:
                            problem("parent_question", comment["body"], f"comment:{comment['id']}")
                if run and live_run and run["max_runtime_seconds"] and run["runtime_s"] is not None and run["runtime_s"] > run["max_runtime_seconds"]:
                    problem("max_runtime", f"Run exceeds its {run['max_runtime_seconds']}s runtime limit", run["id"])
            result.append(card)
    counts = dict(sorted(Counter(c["status"] for c in result).items()))
    return {"read_status": "OK", "board": board, "now": now, "cards": result,
            "counts": counts, "problems": problems, "superseded": superseded, "all_terminal": bool(result) and all(c["status"] in FINAL for c in result),
            "rough_time_left_s": 0 if result and all(c["status"] in FINAL for c in result) else None}


def waiting_count(data):
    """Unfinished, not running and not SUPERSEDED: a retired card is not waiting on anyone."""
    retired = set(data.get("superseded") or [])
    return sum(1 for c in data["cards"] if c["status"] not in FINAL | {"running"} and c["id"] not in retired)


def summary(data):
    if data.get("read_status") != "OK":
        return "Kanban UNKNOWN: " + short(data.get("error", "read failed"))
    counts = data["counts"]
    done = sum(counts.get(s, 0) for s in FINAL)
    running = counts.get("running", 0)
    retired = len(data.get("superseded") or [])
    waiting = waiting_count(data)
    problems = data["problems"]
    lines = [f"Kanban {data['board']}: done {done} / running {running} / waiting {waiting} / problems {len(problems)}" +
             (f" / superseded {retired}" if retired else ""),
             "rough time left " + ("0 (all watched cards terminal)" if data["all_terminal"] else "UNKNOWN (runtime limits are not estimates)")]
    for p in problems[:9]:
        lines.append(f"{p['card']} {p['kind']}: {short(p['text'])}")
    if len(problems) > 9:
        lines.append(f"{len(problems) - 9} more problems in snapshot")
    return "\n".join(lines)


def cursor_path(board, cards, home):
    identity = json.dumps([str(board_path(board, home).absolute()), sorted(cards or [])])
    key = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "hermes-kanban-watch" / f"{board}-{key}.json"


def wait(board, cards=None, every=900, state_file=None, until=None, home=None,
         clock=time.time, sleep=time.sleep):
    """Wake once. Current-problem IDs deduplicate alerts; alerts never defer the 15m deadline."""
    validate_scope(board, cards)
    every = positive(every)
    state_file = Path(state_file) if state_file else cursor_path(board, cards, home)
    scope = [str(board_path(board, home).absolute()), sorted(cards or []), every]
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    if state and state.get("scope") != scope:
        raise ValueError("Cursor belongs to a different watch scope or interval")
    state.setdefault("scope", scope)
    state.setdefault("next_summary_at", clock() + every)
    state.setdefault("seen", [])
    while True:
        now = clock()
        data = snapshot(board, cards, now=now, home=home)
        new = [p for p in data["problems"] if p["id"] not in state["seen"]]
        due = now >= state["next_summary_at"]
        reason = ("final" if data["all_terminal"] else "max_hours" if until is not None and now >= until
                  else "problem" if new else "summary" if due else None)
        state["seen"] = [p["id"] for p in data["problems"]]
        if reason:
            if due:
                periods = math.floor((now - state["next_summary_at"]) / every) + 1
                state["next_summary_at"] += periods * every
            atomic_json(state_file, state)
            return {"reason": reason, "snapshot": data, "new_problems": new, "summary_due": due}
        delay = min(60, state["next_summary_at"] - now)
        if until is not None:
            delay = min(delay, max(0, until - now))
        sleep(delay)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("snapshot", "wait"):
        p = sub.add_parser(name)
        p.add_argument("--board", required=True)
        p.add_argument("--cards", nargs="+", action="extend")
        p.add_argument("--home", type=Path, help="Board data root, not a worker profile")
        if name == "wait":
            p.add_argument("--every", type=float, default=900)
            p.add_argument("--state-file", type=Path)
            p.add_argument("--until", type=float, help="Absolute watch deadline, Unix seconds")
    p = sub.add_parser("summary")
    p.add_argument("--snapshot-file", type=Path, help="Otherwise read JSON from stdin")
    args = parser.parse_args()
    try:
        if args.command == "summary":
            data = json.loads(args.snapshot_file.read_text() if args.snapshot_file else sys.stdin.read())
            print(summary(data.get("snapshot", data)))
        elif args.command == "snapshot":
            print(json.dumps(snapshot(args.board, args.cards, home=args.home), sort_keys=True))
        else:
            print(json.dumps(wait(args.board, args.cards, args.every, args.state_file, args.until, args.home), sort_keys=True))
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as exc:
        print(json.dumps({"read_status": "UNKNOWN", "error": short(exc)}))
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
