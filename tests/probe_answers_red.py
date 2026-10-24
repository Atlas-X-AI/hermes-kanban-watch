"""Run the unchanged capacity control on a disposable, plain origin/main export."""
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
TEST = ROOT / "tests/test_answers.py"


def main():
    before = hashlib.sha256(TEST.read_bytes()).hexdigest()
    base = subprocess.check_output(["git", "rev-parse", "origin/main"], cwd=ROOT, text=True).strip()
    archive = subprocess.run(["git", "archive", "--format=tar", "origin/main", "plugin"],
                             cwd=ROOT, check=True, capture_output=True)
    with tempfile.TemporaryDirectory(prefix="answers-red-") as directory:
        subprocess.run(["tar", "-x", "-C", directory], input=archive.stdout, check=True)
        env = {**os.environ, "ANSWERS_TEST_PLUGIN_ROOT": str(Path(directory) / "plugin"),
               "HOME": str(Path(directory) / "home"),
               "HERMES_HOME": str(Path(directory) / "home/.hermes")}
        out = subprocess.run([sys.executable, "-m", "pytest", "-q",
                              str(TEST) + "::test_template_capacity_known_red"],
                             cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
        print(f"origin/main={base}; test_sha256={before}", flush=True)
        print(out.stdout, end="", flush=True)
        print(out.stderr, end="", file=sys.stderr, flush=True)
        assert out.returncode == 1 and "1 failed" in out.stdout, "The control did not fail as expected"
        assert "can't open file" in out.stdout and "answers.py" in out.stdout, "Unexpected failure reason"
    assert hashlib.sha256(TEST.read_bytes()).hexdigest() == before, "Test changed during the red probe"
    print("KNOWN RED confirmed: origin/main lacks answers.py; the capacity test failed unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
