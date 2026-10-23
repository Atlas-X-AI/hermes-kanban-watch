#!/usr/bin/env python3
"""Run explicit read-only CLI controls against a supplied base and the working reader.

Run from the repository root: python3 tests/probe_reader_controls.py --baseline-ref origin/main
All board, baseline-export and cursor files live inside this script's temporary directory.
"""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

from helpers import create_board


ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", default="origin/main")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="reader-controls-") as directory:
        home = Path(directory)
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("HERMES_KANBAN_") and k != "KANBAN_WATCH_ROUTES"}
        env.update(HOME=str(home), HERMES_HOME=str(home / ".hermes"), PYTHONDONTWRITEBYTECODE="1")
        board = create_board(home / ".hermes/kanban/boards/control/kanban.db")
        since = int(time.time())
        with sqlite3.connect(board) as con:
            con.execute("alter table tasks add column created_by text")
            con.execute("alter table tasks add column model_override text")
            con.executemany("insert into tasks(id,title,status,created_by,created_at,model_override)"
                            " values(?,?,?,?,?,?)", [
                ("t_worker", "Worker", "done", "parent", since - 100, "claude-pin"),
                ("t_verifier", "Verifier", "done", "parent", since - 100, "gpt-pin"),
                ("t_wait", "Old question", "blocked", "parent", since - 100, None),
            ])
            con.executemany("insert into task_runs(id,task_id,status,model_used,provider_used,started_at,ended_at)"
                            " values(?,?,?,?,?,?,?)", [
                (1, "t_worker", "done", "gpt-actual", "openai", since - 90, since - 80),
                (2, "t_verifier", "done", "gpt-review", "openai", since - 70, since - 60),
            ])
            con.execute("insert into task_events(task_id,kind,payload,created_at) values(?,?,?,?)",
                        ("t_wait", "blocked", json.dumps({"reason": "Which interface?"}), since - 50))

        baseline = home / "base/plugin/scripts/kanban_live.py"
        baseline.parent.mkdir(parents=True)
        for relative in ("scripts/kanban_live.py", "scripts/wave_status.py", ".claude-plugin/plugin.json"):
            export = subprocess.run(["git", "show", f"{args.baseline_ref}:plugin/{relative}"],
                                    cwd=ROOT, capture_output=True, check=True)
            target = home / "base/plugin" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(export.stdout)
        candidate = ROOT / "plugin/scripts/kanban_live.py"

        def invoke(reader, arguments, expected):
            before = board.read_bytes()
            result = subprocess.run([sys.executable, str(reader), *arguments], env=env,
                                    capture_output=True, text=True, timeout=10)
            assert result.returncode == expected, (result.returncode, result.stdout, result.stderr)
            assert board.read_bytes() == before, "reader changed board"
            data = json.loads(result.stdout)
            print(json.dumps({"reader": "baseline" if reader == baseline else "candidate",
                              "command": arguments[0], "exit": result.returncode, "result": data}))
            return data

        pair = ["verify-pair", "--board", "control", "--worker", "t_worker", "--verifier", "t_verifier"]
        same = invoke(candidate, pair, 1)
        assert same["different_families"] is False and same["worker"]["model_used"] == "gpt-actual"
        with sqlite3.connect(board) as con:
            con.execute("update task_runs set model_used='claude-actual',provider_used='anthropic' where id=2")
        cross = invoke(candidate, pair, 0)
        assert cross["different_families"] is True
        needs = ["needs", "--board", "control", "--since", str(since), "--created-by", "parent",
                 "--max-seconds", "0", "--ready-stale", "0"]
        old = invoke(baseline, needs, 3)
        assert old["items"] == []
        new = invoke(candidate, [*needs, "--loop"], 0)
        assert [(item["kind"], item["task"]) for item in new["items"]] == [("parent_question", "t_wait")]
        print("PASS: same-family=1, cross-family=0, actual runs beat pins; baseline misses standing question, loop reports it")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
