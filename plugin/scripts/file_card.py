#!/usr/bin/env python3
"""File one routed, model-pinned card and print its read-back-verified watch args."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

sys.dont_write_bytecode = True

from route import load_routes, resolve


class FilingError(Exception):
    pass


def invoke(command, *, json_output=False):
    # A filing command's explicit board must not inherit a dispatcher's DB/task context.
    env = {key: value for key, value in os.environ.items() if not key.startswith("HERMES_KANBAN_")}
    try:
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError, UnicodeError):
        raise FilingError(f"{command[4]} could not run or timed out") from None
    if result.returncode:
        raise FilingError(f"{command[4]} failed; subprocess output omitted")
    if json_output:
        try:
            data = json.loads(result.stdout)
            if isinstance(data, dict):
                return data
        except ValueError:
            pass
        raise FilingError(f"{command[4]} returned invalid JSON; output omitted")
    return {}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True)
    parser.add_argument("--class", dest="class_name", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--body-file", required=True)
    parser.add_argument("--worktree")
    parser.add_argument("--routes")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.board):
        parser.error("Invalid board slug")
    if not args.title.strip() or "\0" in args.title:
        parser.error("A non-empty title without NUL characters is required")
    if args.worktree is not None and (not args.worktree.strip() or "\0" in args.worktree):
        parser.error("A non-empty worktree path without NUL characters is required")

    try:
        body = Path(args.body_file).expanduser().read_text(encoding="utf-8")
        if "\0" in body:
            raise ValueError
    except (OSError, UnicodeError, ValueError):
        print("file-card: Cannot read body file as UTF-8 text without NUL characters", file=sys.stderr)
        return 1
    try:
        worker = resolve(load_routes(args.routes), args.class_name)
    except (OSError, UnicodeError, ValueError) as exc:
        message = "Unknown class" if type(exc) is ValueError and str(exc) == "Unknown class" else "Cannot read or resolve routes"
        print(f"file-card: {message}", file=sys.stderr)
        return 1
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()
    board = home / "kanban.db" if args.board == "default" else home / "kanban/boards" / args.board / "kanban.db"
    if not board.is_file():
        print(f"file-card: Board does not exist; create it with: hermes kanban boards create {args.board}", file=sys.stderr)
        return 1

    base = ["hermes", "kanban", "--board", args.board]
    create = [*base, "create", args.title, "--assignee", worker["profile"], "--body", body, "--json"]
    worktree = str(Path(args.worktree).expanduser().resolve()) if args.worktree is not None else None
    if worktree is not None:
        create += ["--workspace", "worktree:" + worktree]
    # Creation is pinned atomically, even if a dispatcher is already active.
    if worker["model"]:
        create += ["--model", worker["model"], "--provider", worker["provider"]]

    def pin(task_id):
        return [*base, "set-model", task_id, worker["model"], "--provider", worker["provider"]]

    def show(task_id):
        return [*base, "show", task_id, "--json"]

    if args.dry_run:
        print(shlex.join(create))
        if worker["model"]:
            print(shlex.join(pin("<card-id>")))
        print(shlex.join(show("<card-id>")))
        return 0

    task_id = None
    try:
        created = invoke(create, json_output=True)
        candidate = created.get("id")
        if not isinstance(candidate, str) or not re.fullmatch(r"t_[A-Za-z0-9]+", candidate):
            raise FilingError("create returned no valid card id; output omitted")
        task_id = candidate
        if worker["model"]:
            invoke(pin(task_id))
        shown = invoke(show(task_id), json_output=True).get("task")
        expected = {"id": task_id, "title": args.title, "body": body, "assignee": worker["profile"],
                    "model_override": worker["model"], "provider_override": worker["provider"]}
        if worktree is not None:
            expected.update(workspace_kind="worktree", workspace_path=worktree)
        if not isinstance(shown, dict) or any(shown.get(key) != value for key, value in expected.items()):
            raise FilingError("read-back did not match the requested card and model pin")
    except FilingError as exc:
        recovery = (f"Card {task_id} already exists; do not refile. Inspect it with " + shlex.join(show(task_id))
                    if task_id else "Creation may have reached Hermes; inspect the board before refiling.")
        print(f"file-card: {exc}. {recovery}", file=sys.stderr)
        return 1
    print(f"Card: {task_id}")
    print("/kanban-watch " + json.dumps({"board": args.board, "tasks": [task_id]}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
