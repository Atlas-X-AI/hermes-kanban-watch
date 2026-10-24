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
VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip()


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def release_repo(tmp_path, monkeypatch):
    if not (ROOT / "seed").exists():
        pytest.skip("Release tooling is deliberately absent from the public distribution")
    assert (ROOT / "release/release.sh").is_file(), "release entry point is missing"
    # Disposable Git operations must not invoke the operator's auth wrappers.
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin:/bin")
    source = tmp_path / "private"
    source.mkdir()
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "HEAD"],
                             capture_output=True, check=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tree:
        tree.extractall(source, filter="data")
    # Include the implementation before it is committed in the development checkout.
    shutil.copytree(ROOT / "release", source / "release", dirs_exist_ok=True)
    if ref := os.environ.get("RELEASE_SCRIPT_REF"):
        # CLI regressions use the same exact-revision control as release_module.
        script = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:release/release.py"],
                                capture_output=True, text=True, check=True).stdout
        (source / "release/release.py").write_text(script)
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
    sha = args[args.index("--commit") + 1]
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
    if ref := os.environ.get("RELEASE_SCRIPT_REF"):
        # Run the same regression against an exact Git revision as a known-red control.
        source = subprocess.run(["git", "-C", str(ROOT), "show", f"{ref}:release/release.py"],
                                capture_output=True, text=True, check=True).stdout
        exec(compile(source, str(path), "exec"), module.__dict__)
    else:
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("node_location", ["separate", "claude", "absent"])
def test_stranger_path_excludes_operator_git(release_module, monkeypatch, tmp_path, node_location):
    home = Path(os.environ["HOME"])
    operator_bin = home / "bin"
    operator_bin.mkdir()
    wrapper = operator_bin / "git"
    wrapper.write_text("#!/bin/sh\nexit 77\n")
    wrapper.chmod(0o755)
    claude_dir = home / "claude-runtime"
    claude_dir.mkdir()
    claude = claude_dir / "claude"
    claude.write_text("#!/bin/sh\nexit 0\n")
    claude.chmod(0o755)
    shims = home / "shims"
    shims.mkdir()
    (shims / "claude").symlink_to(claude)
    node_dir = claude_dir if node_location == "claude" else home / "node-runtime"
    if node_location != "absent":
        node_dir.mkdir(exist_ok=True)
        node = node_dir / "node"
        node.write_text("#!/bin/sh\nexit 0\n")
        node.chmod(0o755)
    monkeypatch.setenv("PATH", os.pathsep.join(map(str, [operator_bin, shims, node_dir, "/usr/bin", "/bin"])))
    monkeypatch.setenv("GH_TOKEN", "must-not-be-inherited")
    if node_location == "absent":
        which = shutil.which
        monkeypatch.setattr(shutil, "which", lambda name: None if name == "node" else which(name))
    seen = []

    def fake_run(*command, cwd, env):
        seen.append((command, cwd, env))
        # Exercise executable selection using the real disposable failing wrapper.
        result = subprocess.run(["git", "--version"], env=env, capture_output=True)
        assert result.returncode == 0, "operator's failing git leaked into stranger PATH"
        return json.dumps([{"id": release_module.PLUGIN_ID, "enabled": True, "version": VERSION}])

    monkeypatch.setattr(release_module, "run", fake_run)
    release_module.stranger_install(tmp_path, VERSION)

    expected = list(dict.fromkeys([str(claude_dir), *([str(node_dir)] if node_location != "absent" else []),
                                   "/usr/local/bin", "/usr/bin", "/bin"]))
    assert len(seen) == 3
    for command, cwd, env in seen:
        assert command[:2] == (str(claude), "plugin")
        assert cwd == tmp_path / "stranger-work"
        assert env["PATH"].split(os.pathsep) == expected
        assert env["HOME"] == str(tmp_path / "stranger-home")
        assert "GH_TOKEN" not in env


def test_stranger_requires_claude(release_module, monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(release_module, "run", lambda *args, **kwargs: pytest.fail("must refuse first"))
    with pytest.raises(release_module.Refusal, match="claude.*not found"):
        release_module.stranger_install(tmp_path, VERSION)


@pytest.mark.parametrize("token_prefix", ["ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_"])
@pytest.mark.parametrize("subcommand", [("marketplace", "add", PUBLIC), ("install", "plugin"),
                                        ("list", "--json")])
def test_plugin_failure_has_redacted_stderr_tail(release_module, monkeypatch, subcommand, token_prefix):
    github_token = token_prefix + "A" * 24
    api_token = "sk-" + "B-" * 24
    stderr = f"discard one\ndiscard two\nclone failed\n{github_token}\n{api_token}\nno identity\nlast line\n"
    monkeypatch.setattr(release_module.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 7, stdout="not stderr", stderr=stderr))
    with pytest.raises(release_module.Refusal) as refusal:
        release_module.run("/tools/2.1.0", "plugin", *subcommand)
    message = str(refusal.value)
    assert "exited 7" in message
    assert "\n".join(["clone failed", "[REDACTED]", "[REDACTED]", "no identity", "last line"]) in message
    assert github_token not in message and api_token not in message
    assert "discard" not in message and "not stderr" not in message


def test_other_command_failure_still_omits_stderr(release_module, monkeypatch):
    monkeypatch.setattr(release_module.subprocess, "run", lambda command, **kwargs:
                        subprocess.CompletedProcess(command, 9, stdout="", stderr="private scanner output"))
    with pytest.raises(release_module.Refusal, match="git clone exited 9") as refusal:
        release_module.run("git", "clone", URL)
    assert "private scanner output" not in str(refusal.value)


@pytest.fixture
def tag_only_fixture(release_module, monkeypatch, tmp_path):
    sha = "a" * 40
    candidate_tree = "b" * 40
    events = []
    state = {"public_main": sha, "public_tree": candidate_tree, "ci": "success", "existing_tag": False}

    def fake_git(repo, *args):
        events.append(("git", args))
        if args == ("status", "--porcelain=v1", "--untracked-files=all"):
            return ""
        if args == ("rev-parse", "--show-toplevel"):
            return str(tmp_path)
        if args == ("rev-parse", "HEAD^{tree}"):
            return candidate_tree
        if args == ("rev-parse", f"{sha}^{{tree}}"):
            return state["public_tree"]
        assert args == ("rev-parse", "HEAD") or "tag" in args, args
        return sha

    def fake_run(*command, **kwargs):
        if command == ("git", "-C", tmp_path, "ls-remote", "--exit-code", "origin", "refs/heads/main"):
            return sha
        pytest.fail(f"tag-only must not open a PR or run an unstubbed command: {command[:3]}")

    def refs(version):
        events.append(("refs", version))
        if state["existing_tag"]:
            raise release_module.Refusal(f"public version tag v{version} already exists")
        if sequence := state.get("ref_sequence"):
            return sequence.pop(0)
        return state["public_main"]

    def build(*args, **kwargs):
        events.append(("build", args, kwargs))
        return tmp_path / "public-tree"

    def wait(sha, branch):
        events.append(("ci", sha, branch))
        if state["ci"] != "success":
            raise release_module.Refusal("public checks failed; no tag created")

    monkeypatch.setattr(release_module, "git", fake_git)
    monkeypatch.setattr(release_module, "run", fake_run)
    monkeypatch.setattr(release_module, "public_refs", refs)
    monkeypatch.setattr(release_module, "validate_versions", lambda *args: None)
    monkeypatch.setattr(release_module, "workflow_state", lambda *args: "success")
    monkeypatch.setattr(release_module, "build_tree", build)
    monkeypatch.setattr(release_module, "wait_workflows", wait)
    monkeypatch.setattr(release_module, "stranger_install", lambda *args: events.append(("install", args)))
    monkeypatch.setattr(release_module, "push", lambda tree, *refs: events.append(("push", refs)))
    monkeypatch.setattr(release_module.sys, "argv", ["release.py", VERSION, "--tag-only"])
    return state, events


@pytest.mark.parametrize("failure", ["tree", "tag", "ci", "absent-main"])
def test_tag_only_refuses_unchecked_state(release_module, tag_only_fixture, failure):
    state, events = tag_only_fixture
    if failure == "tree":
        state["public_tree"] = "c" * 40
    elif failure == "tag":
        state["existing_tag"] = True
    elif failure == "ci":
        state["ci"] = "failed"
    else:
        state["public_main"] = None
    with pytest.raises(release_module.Refusal, match={"tree": "tree", "tag": "already exists",
                                                    "ci": "public checks failed", "absent-main": "main absent"}[failure]):
        release_module.main()
    assert not any(event[0] in {"install", "push"} for event in events)
    assert not any(event[0] == "git" and "tag" in event[1] for event in events)


def test_tag_only_tags_checked_public_main(release_module, tag_only_fixture):
    state, events = tag_only_fixture
    release_module.main()
    sha = state["public_main"]
    assert [event[2] for event in events if event[0] == "build"] == [{"allow_empty": True}]
    assert ("ci", sha, "main") in events
    assert len([event for event in events if event[0] == "install"]) == 1
    assert ("git", ("-c", "tag.gpgsign=false", "tag", f"v{VERSION}", sha)) in events
    assert [event for event in events if event[0] == "push"] == [
        ("push", (f"refs/tags/v{VERSION}:refs/tags/v{VERSION}",))]
    assert [event[0] for event in events].index("ci") < [event[0] for event in events].index("install")
    assert events[-2:] == [
        ("git", ("-c", "tag.gpgsign=false", "tag", f"v{VERSION}", sha)),
        ("push", (f"refs/tags/v{VERSION}:refs/tags/v{VERSION}",))]


@pytest.mark.parametrize("during_install", [False, True])
def test_tag_only_refuses_moving_main(release_module, tag_only_fixture, during_install):
    state, events = tag_only_fixture
    state["ref_sequence"] = [state["public_main"]] * (2 if during_install else 1) + ["d" * 40]
    with pytest.raises(release_module.Refusal, match="public main (moved|changed)"):
        release_module.main()
    assert any(event[0] == "install" for event in events) == during_install
    assert not any(event[0] == "push" or (event[0] == "git" and "tag" in event[1]) for event in events)


@pytest.mark.parametrize("failure", ["install", "new-tag"])
def test_tag_only_refuses_install_or_racing_tag(release_module, tag_only_fixture, monkeypatch, failure):
    state, events = tag_only_fixture

    def install(*args):
        events.append(("install", args))
        if failure == "install":
            raise release_module.Refusal("stranger install failed")
        state["existing_tag"] = True

    monkeypatch.setattr(release_module, "stranger_install", install)
    with pytest.raises(release_module.Refusal, match="stranger install failed|already exists"):
        release_module.main()
    assert any(event[0] == "install" for event in events)
    assert not any(event[0] == "push" or (event[0] == "git" and "tag" in event[1]) for event in events)


@pytest.mark.parametrize("mode", [(), ("--dry-run",), ("--publish",)])
def test_unchanged_candidate_refused_outside_tag_only(release_repo, mode):
    result = invoke(release_repo, VERSION)
    assert result.returncode == 0, result.stdout + result.stderr
    tree = Path(next(line.split("=", 1)[1] for line in result.stdout.splitlines()
                     if line.startswith("PUBLIC_TREE=")))
    # Seed the exact sanitised tree, not merely an arbitrary existing public main.
    git(tree, "push", URL, "HEAD:refs/heads/main")
    public = release_repo[2]
    refs_before = git(public, "show-ref")
    call_count = len(calls(release_repo))

    result = invoke(release_repo, VERSION, *mode)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Refused: git -C exited 1" in result.stderr
    assert "DRY RUN:" not in result.stdout
    assert git(public, "show-ref") == refs_before
    # Only the private CI read may occur, not scanners, a PR, an install or a tag.
    assert [row[:3] for row in calls(release_repo)[call_count:]] == [["gh", "run", "list"]]


def test_tag_only_disposable_repository_recovery(release_repo):
    # All URL writes are rewritten to release_repo's disposable public.git.
    result = invoke(release_repo, VERSION)
    assert result.returncode == 0, result.stdout + result.stderr
    tree = Path(next(line.split("=", 1)[1] for line in result.stdout.splitlines()
                     if line.startswith("PUBLIC_TREE=")))
    git(tree, "push", URL, "HEAD:refs/heads/main")
    public = release_repo[2]
    main_before = git(public, "rev-parse", "main")
    claude_dir = public.parent / "claude-tools"
    claude_dir.mkdir()
    claude_log = public.parent / "claude-calls.jsonl"
    claude = claude_dir / "claude"
    claude.write_text(
        "#!/usr/bin/env python3\nimport json, sys\n"
        f"with open({str(claude_log)!r}, 'a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"print(json.dumps([{{'id': '{PUBLIC.split('/')[-1]}@{PUBLIC.split('/')[-1]}', "
        f"'version': '{VERSION}', 'enabled': True}}]))\n")
    claude.chmod(0o755)
    # invoke() inherits only this test's PATH; stranger_install sanitises it again.
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setenv("PATH", str(claude_dir) + os.pathsep + os.environ["PATH"])
        result = invoke(release_repo, VERSION, "--tag-only")
    assert result.returncode == 0, result.stdout + result.stderr
    assert git(public, "rev-parse", f"v{VERSION}") == main_before
    assert git(public, "rev-parse", "main") == main_before
    assert git(public, "for-each-ref", "--format=%(refname)", "refs/heads") == "refs/heads/main"
    assert [json.loads(line) for line in claude_log.read_text().splitlines()] == [
        ["plugin", "marketplace", "add", PUBLIC], ["plugin", "install", f"{PUBLIC.split('/')[-1]}@{PUBLIC.split('/')[-1]}"],
        ["plugin", "list", "--json"]]
    assert all(row[1:3] == ["run", "list"] for row in calls(release_repo) if row[0] == "gh")


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


@pytest.mark.parametrize("mode", [(), ("--dry-run",), ("--publish",), ("--tag-only",)])
def test_head_not_private_main_refused(release_repo, mode):
    source, origin, public, _ = release_repo
    # Seed public main so no mode stops early on "public main absent".
    seed_public(release_repo)
    public_refs_before = git(public, "show-ref")
    origin_main = git(origin, "rev-parse", "refs/heads/main")
    git(source, "checkout", "-b", "release/unmerged")
    (source / "unmerged.txt").write_text("Not on private origin main\n")
    commit(source)
    head = git(source, "rev-parse", "HEAD")
    assert head != origin_main

    result = invoke(release_repo, VERSION, *mode)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "Refused:" in result.stderr
    assert head in result.stderr and origin_main in result.stderr
    assert "private origin/main" in result.stderr
    assert "PUBLIC_TREE=" not in result.stdout and "Would push" not in result.stdout
    # Refused before any CI read, scan, push, PR, install or tag.
    assert calls(release_repo) == []
    assert git(public, "show-ref") == public_refs_before
    assert git(origin, "for-each-ref", "--format=%(refname) %(objectname)") == \
        f"refs/heads/main {origin_main}"


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
                                  (VERSION, "--tag-only", "--publish"), (VERSION, "--tag-only", "--dry-run"),
                                  (VERSION, "--checks-timeout", "0"), (VERSION, "--checks-timeout", "-1"),
                                  (VERSION, "--checks-timeout", "invalid")])
def test_bad_arguments_refused(release_repo, args):
    result = invoke(release_repo, *args)
    assert result.returncode != 0 and calls(release_repo) == []
