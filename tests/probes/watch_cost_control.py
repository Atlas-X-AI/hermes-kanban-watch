#!/usr/bin/env python3
"""Run the current watch-cost tests against an unmodified exported remote base."""
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile


def main():
    repo = Path(__file__).resolve().parents[2]
    base = sys.argv[1] if len(sys.argv) > 1 else "origin/main"
    sha = subprocess.check_output(["git", "rev-parse", base], cwd=repo, text=True).strip()
    test = repo / "tests/test_watch_cost.py"
    digest = hashlib.sha256(test.read_bytes()).hexdigest()
    archive = subprocess.check_output(["git", "archive", base, "plugin", "tests/conftest.py",
                                       "tests/helpers.py", "pyproject.toml"], cwd=repo)
    with tempfile.TemporaryDirectory(prefix="watch-cost-control-") as directory:
        export = Path(directory)
        with tarfile.open(fileobj=io.BytesIO(archive)) as files:
            for member in files.getmembers():
                path = export / member.name
                if not path.is_relative_to(export) or ".." in Path(member.name).parts:
                    raise ValueError("unsafe archive member")
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    source = files.extractfile(member)
                    assert source is not None
                    with source, path.open("wb") as target:
                        shutil.copyfileobj(source, target)
                else:
                    raise ValueError("unexpected archive member")
        shutil.copyfile(test, export / "tests/test_watch_cost.py")
        shutil.copytree(repo / "tests/fixtures/watch_cost", export / "tests/fixtures/watch_cost")
        env = dict(os.environ, HOME=str(export / "home"), HERMES_HOME=str(export / "home/.hermes"))
        result = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_watch_cost.py", "-x"],
                                cwd=export, env=env, capture_output=True, text=True, timeout=60)
        print(f"base={sha} test_sha256={digest}")
        print(result.stdout, end="")
        print(result.stderr, end="", file=sys.stderr)
        if result.returncode != 1 or "the public plugin must ship watch_cost.py" not in result.stdout:
            print("known-red control did not fail for the expected missing plugin script", file=sys.stderr)
            return 1
        print("KNOWN-RED: expected missing-script assertion reproduced on exported base")
    return 0


if __name__ == "__main__":
    sys.exit(main())
