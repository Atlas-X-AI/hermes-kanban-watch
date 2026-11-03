#!/usr/bin/env python3
"""Prove three native-delivery regressions go red in plain temporary exports.

Run under the same memory/no-new-privileges guard as pytest. Leaves copies and
logs in its printed /tmp directory for independent inspection; starts no agent.
"""
from pathlib import Path
import os
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
CONTROLS = (
    ("agent-tool", "test_agent_declares_parent_tool", 1,
     "agents/kanban-watcher.md", "tools: Bash, Read, SendMessage", "tools: Bash, Read"),
    ("refusal", "test_claude_native_refuses_before_any_state", 10,
     "scripts/watcher_launch.py",
     "        require_native_parent_tool()  # Refuse before state creation, board reads or a lease.",
     "        pass  # Mutation: capability refusal removed."),
    ("brief-route", "test_detected_claude_brief_names_parent_route", 1,
     "scripts/watcher_launch.py", 'SendMessage with to: "main"', "native parent output"),
)


def run(copy, selector, log):
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
               "tests/test_native_delivery.py", "-k", selector, "--tb=short"]
    print("COMMAND:", " ".join(command), "cwd:", copy, flush=True)
    result = subprocess.run(command, cwd=copy, capture_output=True, text=True, timeout=120,
                            env={**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"})
    output = result.stdout + result.stderr
    log.write_text(output)
    print(output, flush=True)
    return result.returncode, output


def main():
    directory = Path(tempfile.mkdtemp(prefix="native-delivery-controls-", dir="/tmp"))
    print("CONTROL COPIES:", directory, flush=True)
    for name, selector, count, relative, old, new in CONTROLS:
        copy = directory / name
        shutil.copytree(ROOT / "plugin", copy / "plugin")
        (copy / "tests").mkdir()
        for filename in ("conftest.py", "helpers.py", "test_native_delivery.py"):
            shutil.copyfile(ROOT / "tests" / filename, copy / "tests" / filename)
        code, output = run(copy, selector, directory / f"{name}-green.log")
        assert code == 0 and f"{count} passed" in output, (name, code, output)
        path = copy / "plugin" / relative
        source = path.read_text()
        assert source.count(old) == 1, (name, "mutation target must be unique")
        path.write_text(source.replace(old, new))
        code, output = run(copy, selector, directory / f"{name}-red.log")
        assert code == 1 and f"{count} failed" in output, (name, code, output)
        print(f"CONTROL {name}: green {count} passed -> red {count} failed (exit {code})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
