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

from route import load_routes, resolve, safe_identifier


class FilingError(Exception):
    pass


def text(value):
    return isinstance(value, str) and bool(value.strip()) and "\0" not in value


def verdict_context(data, task_id):
    """Use the newest attempt, not an old result or the order of a CLI array."""
    task = data.get("task")
    if not isinstance(task, dict) or task.get("id") != task_id or task.get("status") != "done":
        raise FilingError("Earlier verifier must be done")
    if not text(task.get("title")):
        raise FilingError("Earlier verifier has no valid title")
    runs = data.get("runs", [])
    if not isinstance(runs, list) or any(not isinstance(run, dict) or type(run.get("id")) is not int for run in runs):
        raise FilingError("Earlier verifier has invalid run history")
    latest = max(runs, key=lambda run: run["id"]) if runs else None
    if latest is not None and latest.get("ended_at") is None:
        raise FilingError("Earlier verifier's latest run must be finished")
    summary = latest.get("summary") if latest is not None else task.get("result")
    if not text(summary):
        raise FilingError("Earlier verifier has no valid summary")
    metadata = latest.get("metadata") if latest is not None else None
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise FilingError("Earlier verifier has invalid run metadata")
    return task, summary, metadata


def fix_body(task, summary, metadata, gates):
    if not re.match(r"^FAIL\b", summary):
        raise FilingError("--from-verdict requires a summary starting FAIL")
    if not text(gates):
        raise FilingError("--body-file must contain the fix allowlist and gates")
    head = metadata.get("head_sha")
    if head is None:
        heads = set(re.findall(r"(?<![A-Za-z0-9])[0-9a-fA-F]{40}(?![A-Za-z0-9])", summary))
        head = next(iter(heads)) if len(heads) == 1 else None
    if not isinstance(head, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", head):
        raise FilingError("Failing head must be 40 hex characters in run metadata head_sha or unambiguously in the summary")
    fixes = metadata.get("fix_list", summary)
    if isinstance(fixes, list) and fixes and all(text(item) for item in fixes):
        fixes = "\n".join(item if re.match(r"^\d+[.)]\s", item) else f"{number}. {item}"
                          for number, item in enumerate(fixes, 1))
    if not text(fixes):
        raise FilingError("Run metadata fix_list must be non-empty text or a list of text")
    return (f"Worktree: {task['workspace_path']}\nFailing head: `{head}`\n\n"
            f"## Verifier summary\n\n{summary}\n\n## Fix list\n\n{fixes}\n\n" + gates)


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
    parser.add_argument("--title")
    parser.add_argument("--body-file")
    parser.add_argument("--worktree")
    parser.add_argument("--workspace-kind", choices=("dir", "worktree"))
    parser.add_argument("--created-by")
    parser.add_argument("--max-runtime", type=int)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--from-verdict", metavar="V")
    modes.add_argument("--reverify", metavar="V")
    parser.add_argument("--head", metavar="SHA")
    parser.add_argument("--routes")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    source = args.from_verdict or args.reverify
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.board):
        parser.error("Invalid board slug")
    if source is not None and not re.fullmatch(r"t_[A-Za-z0-9]+", source):
        parser.error("Invalid verifier card id")
    if source and args.title is not None:
        parser.error("--title is generated from the earlier verifier")
    if not source and not text(args.title):
        parser.error("A non-empty title without NUL characters is required")
    if not args.reverify and args.body_file is None:
        parser.error("--body-file is required (include the allowlist and gates)")
    if args.head is not None and not re.fullmatch(r"[0-9a-fA-F]{40}", args.head):
        parser.error("--head must be exactly 40 hex characters")
    if args.reverify and args.head is None:
        parser.error("--reverify requires --head with 40 hex characters")
    if args.head is not None and not args.reverify:
        parser.error("--head requires --reverify")
    if args.created_by is not None and not text(args.created_by):
        parser.error("--created-by must be non-empty text without NUL characters")
    if args.max_runtime is not None and args.max_runtime <= 0:
        parser.error("--max-runtime must be a positive number of seconds")
    if args.worktree is not None and (not args.worktree.strip() or "\0" in args.worktree):
        parser.error("A non-empty worktree path without NUL characters is required")
    if source and args.worktree is not None:
        parser.error("Verdict modes inherit the earlier verifier's workspace; omit --worktree")
    if args.workspace_kind is not None and args.worktree is None and not source:
        parser.error("--workspace-kind requires --worktree or a verdict mode")

    body = ""
    try:
        if args.body_file is not None:
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

    def show(task_id):
        return [*base, "show", task_id, "--json"]

    title = args.title
    if source:
        try:
            task, summary, metadata = verdict_context(invoke(show(source), json_output=True), source)
            inherited = task.get("workspace_path")
            if inherited is not None and (not text(inherited) or not Path(inherited).is_absolute()):
                raise FilingError("Earlier verifier has no valid absolute workspace path")
            if inherited is None and args.workspace_kind is not None:
                raise FilingError("--workspace-kind needs an earlier verifier workspace path")
            if args.from_verdict:
                if inherited is None:
                    raise FilingError("Earlier verifier has no workspace path for the fix")
                body = fix_body(task, summary, metadata, body)
                title = "fix: " + task["title"]
            else:
                if not text(task.get("body")):
                    raise FilingError("Earlier verifier has no valid original body")
                parent_note = f"\n\n## Parent note\n\n{body}" if args.body_file is not None else ""
                header = f"Independent RE-verifier at `{args.head}`. The previous review ({source}) found: {summary}"
                body = header + parent_note + "\n\n## Earlier review\n\n" + task["body"]
                title = "V2: " + task["title"]
                if not safe_identifier(task.get("model_override")):
                    raise FilingError("Earlier verifier has no valid model pin (model_override); refusing to guess")
                if not safe_identifier(task.get("provider_override")):
                    raise FilingError("Earlier verifier has no valid provider pin (provider_override); refusing to guess")
                if not safe_identifier(task.get("assignee"), profile=True):
                    raise FilingError("Earlier verifier has no valid assignee")
                worker = {"profile": task["assignee"], "model": task["model_override"],
                          "provider": task["provider_override"]}
            args.worktree = inherited
        except FilingError as exc:
            print(f"file-card: {exc}; no card created", file=sys.stderr)
            return 1

    create = [*base, "create", title, "--assignee", worker["profile"], "--body", body, "--json"]
    worktree = str(Path(args.worktree).expanduser().resolve()) if args.worktree is not None else None
    workspace_kind = args.workspace_kind or ("dir" if worktree and Path(worktree).is_dir() else "worktree")
    if worktree is not None:
        create += ["--workspace", workspace_kind + ":" + worktree]
    if args.created_by is not None:
        create += ["--created-by", args.created_by]
    if args.max_runtime is not None:
        create += ["--max-runtime", str(args.max_runtime)]
    # Creation is pinned atomically, even if a dispatcher is already active.
    if worker["model"]:
        create += ["--model", worker["model"], "--provider", worker["provider"]]

    def pin(task_id):
        return [*base, "set-model", task_id, worker["model"], "--provider", worker["provider"]]

    if args.dry_run:
        print(shlex.join(create))
        if worker["model"]:
            print(shlex.join(pin("<card-id>")))
        print(shlex.join(show("<card-id>")))
        if worktree is not None:
            print(f"Workspace: {workspace_kind}:" + shlex.quote(worktree))
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
        expected = {"id": task_id, "title": title, "body": body, "assignee": worker["profile"],
                    "model_override": worker["model"], "provider_override": worker["provider"]}
        if worktree is not None:
            expected.update(workspace_kind=workspace_kind, workspace_path=worktree)
        if args.created_by is not None:
            expected["created_by"] = args.created_by
        # Hermes stores --max-runtime but does not expose it in show --json.
        if not isinstance(shown, dict) or any(shown.get(key) != value for key, value in expected.items()):
            raise FilingError("read-back did not match the requested card and model pin")
    except FilingError as exc:
        recovery = (f"Card {task_id} already exists; do not refile. Inspect it with " + shlex.join(show(task_id))
                    if task_id else "Creation may have reached Hermes; inspect the board before refiling.")
        print(f"file-card: {exc}. {recovery}", file=sys.stderr)
        return 1
    print(f"Card: {task_id}")
    if worktree is not None:
        print(f"Workspace: {workspace_kind}:" + shlex.quote(worktree))
    print("/kanban-watch " + json.dumps({"board": args.board, "tasks": [task_id]}, separators=(",", ":")))
    print(f"Not dispatched. Run: hermes kanban --board {args.board} dispatch")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
