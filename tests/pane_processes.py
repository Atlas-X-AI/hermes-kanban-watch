"""A fake process tree for subprocess-based pane tests, never the host process list.

With no PANE_PROCESSES override the default table is /proc-visible: the pane
root is PANE_FAKE_ROOT_PID (the test runner's parent) and the agent is
PANE_FAKE_AGENT_PID (the test runner itself), so the in-lock /proc recheck can
observe both. conftest sets those for every test.
"""
import sys


PROCESSES = """4200 1 S Mon Nov 2 08:00:00 2026 zsh
4201 4200 S Mon Nov 2 08:01:00 2026 claude
4300 1 S Mon Nov 2 08:00:00 2026 zsh
4301 4300 S Mon Nov 2 08:01:00 2026 claude"""


def install_ps(bindir):
    fake = bindir / "ps"
    fake.write_text(f"#!{sys.executable}\n" +
                    "import os, sys\n"
                    "assert sys.argv[1:] == ['-eo', 'pid=,ppid=,stat=,lstart=,comm=']\n"
                    "table = os.environ.get('PANE_PROCESSES')\n"
                    "if table is None:\n"
                    "    root = os.environ.get('PANE_FAKE_ROOT_PID', '4200')\n"
                    "    agent = os.environ.get('PANE_FAKE_AGENT_PID', '4201')\n"
                    "    table = (root + ' 1 S Mon Nov 2 08:00:00 2026 zsh\\n'\n"
                    "             + agent + ' ' + root + ' S Mon Nov 2 08:01:00 2026 claude')\n"
                    "print(table)\n")
    fake.chmod(0o755)
