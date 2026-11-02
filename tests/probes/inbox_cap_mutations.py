#!/usr/bin/env python3
"""Kill issue 11 regressions in disposable exports, never mutate the checkout.

Run under the same resource guard as the individual pytest files. Every case runs
one named test file at a time. A mutant must fail assertions, not collection/setup.
"""
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
CASES = [
    ("pane-membership", "plugin/scripts/notify_session.py",
     "matches = [row for row in panes if pane is None or row[0] == pane]",
     "matches = panes[-1:]",
     "tests/test_inbox_pane_binding.py", "test_shared_windows_pin_parent_not_active_window"),
    ("pane-process", "plugin/scripts/notify_session.py",
     "if pane_pid is not None and pid != pane_pid:", "if False:",
     "tests/test_inbox_pane_binding.py", "test_missing_or_replaced_parent_never_redirects"),
    ("stored-binding", "plugin/scripts/watcher_tick.py",
     'return notify(config["session"], text, key, pane=pane, pane_pid=pane_pid)',
     'return notify(config["session"], text, key)',
     "tests/test_inbox_pane_binding.py", "test_every_parent_inbox_path_uses_recorded_pane"),
    ("all-windows", "plugin/scripts/notify_session.py",
     '["tmux", "list-panes", "-s", "-t",', '["tmux", "list-panes", "-t",',
     "tests/test_inbox_pane_binding.py", "test_shared_windows_pin_parent_not_active_window"),
    ("live-cap", "plugin/scripts/board_watch.py",
     'and not live_run and not capped:', 'and not live_run:',
     "tests/test_board_watch.py", "test_live_cap_is_reported_not_a_problem"),
]


def run(directory, test_file, selector):
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", test_file, "-k", selector,
                             "--tb=short"], cwd=directory, capture_output=True, text=True, timeout=90)
    lines = result.stdout.splitlines()
    summary = next((line for line in reversed(lines) if re.search(r"\d+ (?:passed|failed)", line)), "")
    return result, summary


def main():
    for name, relative, old, new, test_file, selector in CASES:
        with tempfile.TemporaryDirectory(prefix="watch-mutation-") as temporary:
            directory = Path(temporary)
            for tree in ("plugin", "tests"):
                shutil.copytree(ROOT / tree, directory / tree,
                                ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
            shutil.copy2(ROOT / "pyproject.toml", directory / "pyproject.toml")
            baseline, summary = run(directory, test_file, selector)
            if baseline.returncode != 0 or not re.search(r"\d+ passed", summary):
                print(f"{name}: BASELINE NOT GREEN\n{baseline.stdout}\n{baseline.stderr}")
                return 1
            print(f"{name}: baseline {summary}", flush=True)
            target = directory / relative
            text = target.read_text()
            if text.count(old) != 1:
                print(f"{name}: mutation site is not unique")
                return 1
            target.write_text(text.replace(old, new, 1))
            mutant, summary = run(directory, test_file, selector)
            if (mutant.returncode != 1 or not re.search(r"\d+ failed", summary) or
                    re.search(r"\d+ errors?\b", summary)):
                print(f"{name}: MUTANT SURVIVED OR HARNESS FAILED\n{mutant.stdout}\n{mutant.stderr}")
                return 1
            print(f"{name}: KILLED ({summary})", flush=True)
    print(f"{len(CASES)} mutation checks killed; all baselines green")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
