"""A fake process tree for subprocess-based pane tests, never the host process list."""
import sys


PROCESSES = """4200 1 S Mon Nov 2 08:00:00 2026 zsh
4201 4200 S Mon Nov 2 08:01:00 2026 claude
4300 1 S Mon Nov 2 08:00:00 2026 zsh
4301 4300 S Mon Nov 2 08:01:00 2026 claude"""


def install_ps(bindir):
    fake = bindir / "ps"
    fake.write_text(f"#!{sys.executable}\n" +
                    "import os, sys\n"
                    "assert sys.argv[1:] == ['-eo', 'pid=,ppid=,stat=,lstart=,comm=']\n" +
                    f"print(os.environ.get('PANE_PROCESSES', {PROCESSES!r}))\n")
    fake.chmod(0o755)
