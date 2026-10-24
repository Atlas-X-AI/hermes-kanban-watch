#!/usr/bin/env python3
"""Print a checked cross-family review receipt and head-pinned merge command.

Read-only: never merges, comments, marks ready or changes a Kanban card.
Exit 0 = PASS, 1 = refused, 3 = UNKNOWN (evidence could not be read).
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys

# Do not create import caches in an installed plugin.
sys.dont_write_bytecode = True
import kanban_live

SHA = re.compile(r"[0-9a-fA-F]{40}\Z")
SUMMARY_SHA = re.compile(r"(?<![A-Za-z0-9])[0-9a-fA-F]{40}(?![A-Za-z0-9])")
ACCEPTED = {"SUCCESS", "NEUTRAL", "SKIPPED"}


class Refusal(Exception):
    def __init__(self, check: str, reason: str, *, unknown: bool = False):
        self.check = check
        self.reason = reason
        self.code = 3 if unknown else 1
        super().__init__(reason)


def verify_pair(args) -> dict:
    """Use the reader's actual-run selection, route precedence and family logic unchanged."""
    output = io.StringIO()
    try:
        with contextlib.redirect_stdout(output):
            code = kanban_live.cmd_verify_pair(argparse.Namespace(
                board=args.board, worker=args.worker, verifier=args.verifier, families=args.routes))
        pair = json.loads(output.getvalue())
    except (sqlite3.Error, OSError, ValueError):
        raise Refusal("families", "Cannot read board or actual-run family evidence", unknown=True) from None
    if code != 0 or pair.get("different_families") is not True:
        if pair.get("different_families") is False:
            raise Refusal("families", "Worker and verifier ran models from the same family")
        raise Refusal("families", pair.get("error") or "Actual-run families are unknown", unknown=True)
    return {role: pair[role] for role in ("worker", "verifier")}


def verifier_evidence(args, pair: dict) -> tuple[str, str]:
    """Read the full newest summary and optional metadata, not the reader's clipped snippet."""
    try:
        with kanban_live.connect(kanban_live.board_db(args.board)) as conn:
            conn.execute("begin")
            for role in ("worker", "verifier"):
                card = conn.execute("select status from tasks where id = ?", (pair[role]["id"],)).fetchone()
                if card is None:
                    raise Refusal("verifier_run", f"The {role} card cannot be found", unknown=True)
                if role == "verifier" and card["status"] != "done":
                    raise Refusal("verifier_status", "Verifier card is not done")
            columns = {row["name"] for row in conn.execute("pragma table_info(task_runs)")}
            metadata = "metadata" if "metadata" in columns else "NULL as metadata"
            run = conn.execute(
                "select id, model_used, provider_used, ended_at, summary, " + metadata +
                " from task_runs where task_id = ? order by id desc limit 1", (args.verifier,)).fetchone()
            worker = conn.execute(
                "select id, model_used, provider_used from task_runs where task_id = ?"
                " and ended_at is not null order by id desc limit 1", (args.worker,)).fetchone()
            # Do not mix a PASS from a newer unfinished run with an older finished family check.
            for role, actual in (("worker", worker), ("verifier", run)):
                recorded = pair[role]
                if (actual is None or actual["id"] != recorded["run_id"] or
                        any(actual[key] != recorded[key] for key in ("model_used", "provider_used"))):
                    raise Refusal("verifier_run", "Latest runs changed or the verifier run is unfinished",
                                  unknown=True)
            if run["ended_at"] is None:
                raise Refusal("verifier_run", "Verifier's latest run is unfinished", unknown=True)
            summary, raw_metadata = run["summary"], run["metadata"]
    except sqlite3.Error:
        raise Refusal("verifier_run", "Cannot read verifier status, last run or metadata", unknown=True) from None
    if not isinstance(summary, str):
        raise Refusal("verdict", "Verifier's last-run summary is unreadable", unknown=True)
    if not summary.startswith("PASS"):
        raise Refusal("verdict", "Verifier's last-run summary does not start with PASS")
    try:
        metadata = json.loads(raw_metadata) if raw_metadata is not None else {}
        if not isinstance(metadata, dict):
            raise ValueError("metadata must be an object")
        if "head_sha" in metadata:
            head = metadata["head_sha"]
        else:
            match = SUMMARY_SHA.search(summary)
            head = match.group() if match else None
    except (TypeError, ValueError):
        raise Refusal("head_sha", "Verifier run metadata is unreadable", unknown=True) from None
    if not isinstance(head, str) or SHA.fullmatch(head) is None:
        raise Refusal("head_sha", "Verifier has no valid full head SHA", unknown=True)
    return summary, head


def read_pr(args) -> dict:
    """Read the live head, CI and merge state together. No mutation subcommand exists here."""
    try:
        result = subprocess.run(
            [os.environ.get("PR_GATE_GH", "gh"), "pr", "view", args.pr, "--repo", args.repo,
             "--json", "headRefOid,statusCheckRollup,mergeStateStatus"],
            capture_output=True, text=True, timeout=30)
        if result.returncode != 0:
            raise ValueError("view failed")
        data = json.loads(result.stdout)
        if (not isinstance(data, dict) or not isinstance(data.get("headRefOid"), str) or
                SHA.fullmatch(data["headRefOid"]) is None):
            raise ValueError("missing or malformed head")
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        # Do not relay arbitrary gh errors, which may include private provider details.
        raise Refusal("pr_read", "Cannot read a valid PR head and check rollup from gh pr view",
                      unknown=True) from None
    return data


def ci_evidence(rollup) -> list[dict]:
    if not isinstance(rollup, list) or not rollup:
        raise Refusal("ci", "CI rollup is absent, empty or unreadable", unknown=True)
    checks = []
    for item in rollup:
        if not isinstance(item, dict):
            raise Refusal("ci", "Malformed CI entry", unknown=True)
        kind = item.get("__typename")
        if kind == "CheckRun":
            name, status, result = item.get("name"), item.get("status"), item.get("conclusion")
            if not isinstance(status, str) or not status:
                raise Refusal("ci", "Check run status is unreadable", unknown=True)
            if status != "COMPLETED":
                raise Refusal("ci", f"Check run is not complete ({display(status)})")
        elif kind == "StatusContext":
            name, result = item.get("context"), item.get("state")
        else:
            raise Refusal("ci", "Unknown CI entry type", unknown=True)
        if not isinstance(name, str) or not name or not isinstance(result, str) or not result:
            raise Refusal("ci", "CI name or result is unreadable", unknown=True)
        if result not in ACCEPTED:
            raise Refusal("ci", f"CI result is not accepted ({display(result)})")
        checks.append({"name": name, "result": result})
    return checks


def receipt(args) -> dict:
    pair = verify_pair(args)
    verdict, recorded_head = verifier_evidence(args, pair)
    pr = read_pr(args)
    if recorded_head != pr["headRefOid"]:
        raise Refusal("head_sha", "Verifier recorded SHA differs from the live PR head")
    checks = ci_evidence(pr.get("statusCheckRollup"))
    state = pr.get("mergeStateStatus")
    if not isinstance(state, str) or not state.strip():
        raise Refusal("merge_state", "PR merge state is absent or unreadable", unknown=True)
    if state not in {"CLEAN", *args.allow_merge_state}:
        raise Refusal("merge_state", f"PR merge state is not accepted ({display(state)})")
    return {"status": "PASS", "board": args.board, "repo": args.repo, "pr": int(args.pr),
            **pair, "verdict": verdict, "head_sha": recorded_head, "ci": checks,
            "merge_command": shlex.join(["gh", "pr", "merge", args.pr, "--repo", args.repo,
                                         "--squash", "--match-head-commit", recorded_head])}


def display(value: str) -> str:
    """Keep untrusted receipt fields on one Markdown line, without terminal controls or markup."""
    text = " ".join("".join(char if char.isprintable() else " " for char in value).split())
    for char in ("\\", "`", "*", "[", "]", "<", ">", "#", "|"):
        text = text.replace(char, "\\" + char)
    return text


def emit(data: dict, format_: str):
    if format_ == "json":
        print(json.dumps(data, ensure_ascii=False))
    elif data["status"] != "PASS":
        print(f"{data['status']}: {data['failed_check']}: {data['reason']}")
    else:
        print(f"## PASS: PR gate for {data['repo']}#{data['pr']}")
        print(f"- Board: {display(data['board'])}")
        for role in ("worker", "verifier"):
            run = data[role]
            print(f"- {role.title()} card: {display(run['id'])}, run {run['run_id']}; "
                  f"model {display(run['model_used'])}; provider {display(run['provider_used'])}; "
                  f"family {display(run['family'])}")
        print(f"- Verdict: {display(data['verdict'])}")
        print(f"- Head SHA: {data['head_sha']}")
        for check in data["ci"]:
            print(f"- CI: {display(check['name'])}: {check['result']}")
        print("\nMerge command (printed only):\n")
        print(data["merge_command"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--verifier", required=True)
    parser.add_argument("--repo", required=True, help="OWNER/NAME")
    parser.add_argument("--pr", required=True)
    parser.add_argument("--routes", help="routes file with the providers-to-family mapping")
    parser.add_argument("--allow-merge-state", action="append", default=[],
                        choices=("HAS_HOOKS", "UNSTABLE"),
                        help="Accept this merge state in addition to CLEAN (repeatable)")
    parser.add_argument("--format", choices=("md", "json"), default="md")
    args = parser.parse_args(argv)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", args.repo) is None:
        parser.error("--repo must be OWNER/NAME")
    if re.fullmatch(r"[1-9][0-9]*", args.pr) is None:
        parser.error("--pr must be a positive PR number")
    try:
        data = receipt(args)
    except Refusal as exc:
        emit({"status": "UNKNOWN" if exc.code == 3 else "FAIL", "failed_check": exc.check,
              "reason": exc.reason}, args.format)
        return exc.code
    emit(data, args.format)
    return 0


if __name__ == "__main__":
    sys.exit(main())
