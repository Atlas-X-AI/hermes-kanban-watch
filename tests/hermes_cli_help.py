"""Print the installed CLI contract without touching live Hermes data."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    executable = shutil.which("hermes")
    if not executable:
        raise SystemExit("Hermes is not on PATH")
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory)
        env = {"PATH": os.environ["PATH"], "HOME": str(home),
               "HERMES_HOME": str(home / ".hermes"), "PYTHONDONTWRITEBYTECODE": "1"}
        for args in (("create", "--help"), ("set-model", "--help"), ("show", "--help")):
            command = [executable, "kanban", "--board", "default", *args]
            result = subprocess.run(command, env=env, cwd=home, capture_output=True,
                                    text=True, timeout=60)
            print(" ".join(command))
            print(result.stdout)
            if result.returncode:
                print(result.stderr)
                raise SystemExit(result.returncode)
        for args in (("boards", "create", "demo"),
                     ("--board", "demo", "create", "CLI contract", "--assignee", "builder", "--json"),
                     ("--board", "demo", "list", "--json")):
            result = subprocess.run([executable, "kanban", *args], env=env, cwd=home,
                                    capture_output=True, text=True, timeout=60)
            print(result.stdout)
            if result.returncode:
                print(result.stderr)
                raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
