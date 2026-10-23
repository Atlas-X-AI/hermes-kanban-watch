"""Release safety contracts using only disposable Git repositories and command doubles."""
import io
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest

from conftest import ROOT

PUBLIC = "Atlas-X-AI/hermes-kanban-watch"
URL = f"https://github.com/{PUBLIC}.git"
VERSION = "0.2.0"


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def release_repo(tmp_path, monkeypatch):
    if not (ROOT / "seed").exists():
        pytest.skip("Release tooling is deliberately absent from the public distribution")
    assert (ROOT / "release/release.sh").is_file(), "release entry point is missing"
    source = tmp_path / "private"
    source.mkdir()
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "HEAD"],
                             capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        tree.extractall(source, filter="data")
    # Include the implementation before it is committed in the development checkout.
    shutil.copytree(ROOT / "release", source / "release", dirs_exist_ok=True)
    git(source, "init", "--initial-branch=main")
    git(source, "config", "user.name", "Release test")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "add", ".")
    git(source, "commit", "-m", "Private input")
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "--bare", str(source), str(origin))
    git(source, "remote", "add", "origin", str(origin))
    git(source, "fetch", "origin")
    public = tmp_path / "public.git"
    git(tmp_path, "init", "--bare", "--initial-branch=main", str(public))
    # Exact URL rewrite, confined to this test's disposable HOME/config.
    config = tmp_path / "gitconfig"
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    git(source, "config", "--global", f"url.{public.as_uri()}.insteadOf", URL)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "calls.jsonl"
    double = '''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import sys
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["RELEASE_CALLS"], "a") as stream:
    stream.write(json.dumps([name, *args]) + "\\n")
if name == "gh":
    if os.environ.get("GH_ERROR"):
        sys.exit(7)
    assert args[:2] == ["run", "list"], args
    sha = subprocess.check_output(["git", "rev-parse", "refs/remotes/origin/main"], text=True).strip()
    state = os.environ.get("CI_STATE", "success")
    if state == "stale":
        sha = "f" * 40
    rows = [{"databaseId": n, "name": name, "headSha": sha,
             "status": "completed", "conclusion": "success"}
            for n, name in enumerate(["CI", "Clean install smoke"], 1)]
    if state == "missing":
        rows = []
    elif state == "pending":
        rows[0].update(status="in_progress", conclusion="")
    elif state == "failure":
        rows[0]["conclusion"] = "failure"
    elif state == "smoke-missing":
        rows = rows[:1]
    elif state == "smoke-failure":
        rows[1]["conclusion"] = "failure"
    elif state == "mixed":
        rows.append(dict(rows[0], databaseId=3, name="Other workflow", conclusion="failure"))
    print(json.dumps(rows))
else:
    sys.exit(int(os.environ.get(name.upper() + "_EXIT", "0")))
'''
    for name in ("gh", "gitleaks", "trufflehog"):
        path = bindir / name
        path.write_text(double)
        path.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("RELEASE_CALLS", str(log))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return source, origin, public, log


def invoke(release_repo, *args):
    source, _, _, _ = release_repo
    return subprocess.run(["bash", "release/release.sh", *args], cwd=source,
                          capture_output=True, text=True, timeout=30)


def calls(release_repo):
    path = release_repo[3]
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def commit(repo):
    git(repo, "add", ".")
    git(repo, "commit", "-m", "Test change")


@pytest.fixture
def release_module():
    path = ROOT / "release/release.py"
    if not path.is_file():
        pytest.skip("Release tooling is deliberately absent from the public distribution")
    spec = importlib.util.spec_from_file_location("release_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_publish_without_public_main_refused(release_module, monkeypatch, tmp_path):
    pushes = []
    git_calls = []
    ref_queries = []

    def fake_git(tree, *args):
        git_calls.append(args)
        return "a" * 40

    def no_public_main(version):
        ref_queries.append(version)
        return None

    monkeypatch.setattr(release_module, "git", fake_git)
    monkeypatch.setattr(release_module, "public_refs", no_public_main)
    monkeypatch.setattr(release_module, "push", lambda tree, *refs: pushes.append(refs))
    monkeypatch.setattr(release_module, "wait_workflows", lambda *args: None)
    monkeypatch.setattr(release_module, "stranger_install", lambda *args: None)

    with pytest.raises(release_module.Refusal) as refusal:
        release_module.publish(tmp_path, None, VERSION)

    assert ref_queries, "The absent public-main state must actually be queried"
    assert pushes == []
    assert not any("tag" in args for args in git_calls)
    assert str(refusal.value) == (
        "public main absent; the initial release is a manual ceremony (going-public.md)"
    )


@pytest.mark.parametrize("checks_timeout", [None, 42])
def test_checks_timeout_refuses_without_tag(release_module, monkeypatch, tmp_path, checks_timeout):
    sha = "a" * 40
    url = f"https://github.com/{PUBLIC}/pull/123"
    pushes = []
    git_calls = []
    command_calls = []
    workflows = []

    def fake_git(tree, *args):
        git_calls.append(args)
        return sha

    def fake_run(command, **kwargs):
        command_calls.append((command, kwargs["timeout"]))
        if command[:3] == ["gh", "pr", "create"]:
            return subprocess.CompletedProcess(command, 0, stdout=url + "\n")
        assert command == ["gh", "pr", "checks", url, "--repo", PUBLIC, "--watch", "--fail-fast"]
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    def unexpected_install(*args):
        pytest.fail("An unchecked release must not reach the stranger install")

    monkeypatch.setattr(release_module, "git", fake_git)
    monkeypatch.setattr(release_module, "public_refs", lambda version: sha)
    monkeypatch.setattr(release_module, "push", lambda tree, *refs: pushes.append(refs))
    monkeypatch.setattr(release_module, "wait_workflows", lambda *args: workflows.append(args))
    monkeypatch.setattr(release_module, "stranger_install", unexpected_install)
    monkeypatch.setattr(release_module.subprocess, "run", fake_run)
    options = {} if checks_timeout is None else {"checks_timeout": checks_timeout}

    with pytest.raises(release_module.Refusal) as refusal:
        release_module.publish(tmp_path, sha, VERSION, **options)

    assert "timed out" in str(refusal.value)
    assert url in str(refusal.value) and "no tag created" in str(refusal.value)
    assert pushes == [(f"HEAD:refs/heads/release/v{VERSION}",)]
    assert git_calls == [("rev-parse", "HEAD")]
    assert workflows == [(sha, f"release/v{VERSION}")]
    assert [timeout for _, timeout in command_calls] == [300, checks_timeout or 1800]


@pytest.mark.parametrize("mode", [(), ("--dry-run",), ("--dry-run", "--checks-timeout", "42")])
def test_dry_run_is_local_and_sanitised(release_repo, mode):
    source, _, public, _ = release_repo
    before = git(source, "status", "--porcelain=v1", "--untracked-files=all")
    result = invoke(release_repo, VERSION, *mode)
    assert result.returncode == 0, result.stdout + result.stderr
    tree = Path(next(line.removeprefix("PUBLIC_TREE=") for line in result.stdout.splitlines()
                     if line.startswith("PUBLIC_TREE=")))
    assert tree.is_dir() and (tree / "plugin/.claude-plugin/plugin.json").is_file()
    assert (tree / "VERSION").read_text().strip() == VERSION
    assert all(not (tree / name).exists() for name in ("seed", "SPEC.md", "release"))
    assert git(tree, "log", "-1", "--format=%s") == f"Initial public release (v{VERSION})"
    assert git(tree, "rev-list", "--count", "HEAD") == "1"
    assert "Publish would refuse: public main absent" in result.stdout and URL in result.stdout
    assert "manual ceremony (going-public.md)" in result.stdout
    assert subprocess.run(["git", "-C", str(public), "show-ref"], capture_output=True).returncode == 1
    assert git(source, "status", "--porcelain=v1", "--untracked-files=all") == before == ""
    names = [row[0] for row in calls(release_repo)]
    assert names == ["gh", "gitleaks", "trufflehog"]
    assert calls(release_repo)[1][1] == "detect"
    assert calls(release_repo)[2][1] == "filesystem" and "--fail" in calls(release_repo)[2]


@pytest.mark.parametrize("path", ["VERSION", "pyproject.toml", "plugin/.claude-plugin/plugin.json",
                                  ".claude-plugin/marketplace.json"])
def test_version_disagreement_refused(release_repo, path):
    repo = release_repo[0]
    target = repo / path
    target.write_text(target.read_text().replace(VERSION, "9.9.9"))
    commit(repo)
    result = invoke(release_repo, VERSION)
    assert result.returncode != 0 and path in result.stderr and "version" in result.stderr.lower()
    assert not any(row[0] in {"gitleaks", "trufflehog"} for row in calls(release_repo))


@pytest.mark.parametrize("kind", ["tracked", "untracked", "staged"])
def test_dirty_tree_refused(release_repo, kind):
    repo = release_repo[0]
    target = repo / ("VERSION" if kind == "tracked" else "extra.txt")
    target.write_text("changed")
    if kind == "staged":
        git(repo, "add", ".")
    result = invoke(release_repo, VERSION)
    assert result.returncode != 0 and "dirty" in result.stderr.lower()
    assert calls(release_repo) == []


@pytest.mark.parametrize("state", ["missing", "stale", "pending", "failure", "smoke-missing",
                                   "smoke-failure", "mixed"])
def test_non_green_private_main_refused(release_repo, monkeypatch, state):
    monkeypatch.setenv("CI_STATE", state)
    result = invoke(release_repo, VERSION)
    assert result.returncode != 0 and "private main" in result.stderr.lower()
    assert [row[0] for row in calls(release_repo)] == ["gh"]


def test_api_error_fails_closed(release_repo, monkeypatch):
    monkeypatch.setenv("GH_ERROR", "1")
    result = invoke(release_repo, VERSION)
    assert result.returncode != 0 and "7" in result.stderr
    assert [row[0] for row in calls(release_repo)] == ["gh"]


@pytest.mark.parametrize("name,code", [("gitleaks", 23), ("trufflehog", 24)])
def test_scanner_exit_is_not_masked(release_repo, monkeypatch, name, code):
    monkeypatch.setenv(name.upper() + "_EXIT", str(code))
    result = invoke(release_repo, VERSION)
    assert result.returncode == code and f"{name} exited {code}" in result.stderr
    expected = ["gh", "gitleaks"] + (["trufflehog"] if name == "trufflehog" else [])
    assert [row[0] for row in calls(release_repo)] == expected


def seed_public(release_repo):
    repo, _, public, _ = release_repo
    base = public.parent / "public-source"
    git(public.parent, "clone", str(public), str(base))
    git(base, "config", "user.name", "Public test")
    git(base, "config", "user.email", "public@example.invalid")
    (base / "old-only.txt").write_text("Must disappear from the next snapshot")
    (base / "VERSION").write_text("0.1.0\n")
    commit(base)
    git(base, "push", "origin", "main")
    return base


def test_existing_tag_refused(release_repo):
    base = seed_public(release_repo)
    git(base, "tag", f"v{VERSION}")
    git(base, "push", "origin", f"v{VERSION}")
    result = invoke(release_repo, VERSION)
    assert result.returncode != 0 and f"v{VERSION}" in result.stderr and "already" in result.stderr
    assert not any(row[0] in {"gitleaks", "trufflehog"} for row in calls(release_repo))


def test_later_release_has_only_public_parent(release_repo):
    base = seed_public(release_repo)
    parent = git(base, "rev-parse", "HEAD")
    result = invoke(release_repo, VERSION)
    assert result.returncode == 0, result.stdout + result.stderr
    tree = Path(next(line.split("=", 1)[1] for line in result.stdout.splitlines()
                     if line.startswith("PUBLIC_TREE=")))
    assert git(tree, "rev-parse", "HEAD^") == parent
    assert git(tree, "log", "-1", "--format=%s") == f"Release v{VERSION}"
    assert not (tree / "old-only.txt").exists()
    assert git(release_repo[2], "rev-parse", "main") == parent
    assert "PR" in result.stdout


@pytest.mark.parametrize("args", [(), ("not-a-version",), (VERSION, "--wrong"), (VERSION, "--dry-run", "extra"),
                                  (VERSION, "--checks-timeout", "0"), (VERSION, "--checks-timeout", "-1"),
                                  (VERSION, "--checks-timeout", "invalid")])
def test_bad_arguments_refused(release_repo, args):
    result = invoke(release_repo, *args)
    assert result.returncode != 0 and calls(release_repo) == []
