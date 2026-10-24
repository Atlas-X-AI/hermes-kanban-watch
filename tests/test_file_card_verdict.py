"""Verdict-loop controls, with only disposable boards and a subprocess fake CLI.

FILE_CARD_TEST_REF=origin/main runs these same controls against exported base
scripts, without changing the checkout, reading live boards or invoking models.
"""
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from conftest import PLUGIN, ROOT
from helpers import create_board, fake_hermes

HEAD = "1234567890abcdef1234567890abcdef12345678"
NEW_HEAD = "abcdef1234567890abcdef1234567890abcdef12"
SOURCE = "t_review"


@pytest.fixture
def loop(tmp_path, isolated_home, monkeypatch, routes):
    _, home = isolated_home
    bindir = tmp_path / "bin"
    fake_hermes(bindir)
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CALLS", str(calls))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.defpath)
    db = create_board(home / "kanban/boards/demo/kanban.db")
    routefile = tmp_path / "routes.json"
    routefile.write_text(json.dumps(routes))
    worktree = tmp_path / "checked out tree"
    worktree.mkdir()
    brief = tmp_path / "gates.txt"
    brief.write_text("## Allowlist and gates\nEdit only src/example.py.\nRun the targeted tests.\n")
    card = {"id": SOURCE, "title": "Independent review 'one'", "status": "done",
            "body": f"Review the original change at `{HEAD}`.\nNever merge.\n",
            "result": "PASS stale result must not override the latest run",
            "assignee": "reviewer", "model_override": "claude-sonnet-4-5", "provider_override": "anthropic",
            "workspace_kind": "dir", "workspace_path": str(worktree)}
    run = {"id": 3, "ended_at": 100, "summary": "FAIL: two corrections required",
           "metadata": {"head_sha": HEAD, "fix_list": ["Repair the quoted 'value'.", "Keep the gate."]}}
    script = PLUGIN / "scripts/file_card.py"
    ref = os.environ.get("FILE_CARD_TEST_REF")
    if ref:
        # An ordinary export in pytest's temporary directory, never a worktree.
        export = tmp_path / "base-scripts"
        export.mkdir()
        for name in ("file_card.py", "route.py"):
            result = subprocess.run(["git", "show", f"{ref}:plugin/scripts/{name}"],
                                    cwd=ROOT, capture_output=True, check=True, timeout=10)
            (export / name).write_bytes(result.stdout)
        script = export / "file_card.py"
    return {"db": db, "calls": calls, "routes": routefile, "brief": brief,
            "tree": worktree, "card": card, "run": run, "script": script,
            "monkeypatch": monkeypatch}


def seed(loop, runs=None):
    loop["db"].with_suffix(".cards.json").write_text(json.dumps({SOURCE: loop["card"]}))
    loop["monkeypatch"].setenv("FAKE_RUNS", json.dumps({SOURCE: runs if runs is not None else [loop["run"]]}))


def call(loop, *extra, mode="fix", dry=True, runs=None):
    seed(loop, runs)
    args = [sys.executable, str(loop["script"]), "--board", "demo", "--class", "app-code",
            "--routes", str(loop["routes"])]
    if mode == "fix":
        args += ["--from-verdict", SOURCE, "--body-file", str(loop["brief"])]
    elif mode == "verify":
        args += ["--reverify", SOURCE, "--head", NEW_HEAD]
    else:
        args += ["--title", "Normal card", "--body-file", str(loop["brief"])]
    if dry:
        args += ["--dry-run"]
    return subprocess.run([*args, *extra], capture_output=True, text=True, timeout=20)


def logged(loop):
    path = loop["calls"]
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def dry_create(out):
    assert out.returncode == 0, out.stdout + out.stderr
    tokens = shlex.split(out.stdout)
    start = tokens.index("hermes")
    end = tokens.index("hermes", start + 1)
    command = tokens[start:end]
    assert command[:5] == ["hermes", "kanban", "--board", "demo", "create"]
    return command


def value(command, flag):
    return command[command.index(flag) + 1]


def test_fail_dry_run_includes_worktree_head_fixes_and_gates(loop):
    before = loop["db"].read_bytes()
    out = call(loop)
    command = dry_create(out)
    assert command[5] == "fix: " + loop["card"]["title"]
    body = value(command, "--body")
    assert str(loop["tree"]) in body and HEAD in body
    assert loop["run"]["summary"] in body
    assert "1. Repair the quoted 'value'.\n2. Keep the gate." in body
    assert body.endswith(loop["brief"].read_text())
    assert value(command, "--assignee") == "builder"
    assert value(command, "--workspace") == f"dir:{loop['tree']}"
    assert "Workspace: dir:" in out.stdout
    assert logged(loop) == [["kanban", "--board", "demo", "show", SOURCE, "--json"]]
    assert loop["db"].read_bytes() == before
    assert list(json.loads(loop["db"].with_suffix(".cards.json").read_text())) == [SOURCE]


def test_pass_refused_before_any_creation(loop):
    loop["run"]["summary"] = "PASS: no changes"
    out = call(loop)
    assert out.returncode != 0 and "FAIL" in out.stderr
    assert all(command[3] == "show" for command in logged(loop))


def test_reverify_dry_run_uses_different_family_verifier(loop, routes):
    out = call(loop, mode="verify")
    command = dry_create(out)
    assert command[5] == "V2: " + loop["card"]["title"]
    assert value(command, "--assignee") == "reviewer"
    assert value(command, "--model") == "claude-sonnet-4-5"
    assert routes["providers"][value(command, "--provider")] != routes["providers"]["openai"]
    body = value(command, "--body")
    assert body.startswith(f"Independent RE-verifier at `{NEW_HEAD}`.")
    assert f"The previous review ({SOURCE}) found: {loop['run']['summary']}" in body
    assert body.endswith("## Earlier review\n\n" + loop["card"]["body"])
    assert logged(loop) == [["kanban", "--board", "demo", "show", SOURCE, "--json"]]


@pytest.mark.parametrize("dry", [False, True])
def test_reverify_parent_note_follows_first_paragraph_and_preserves_original(loop, dry):
    note = "Recheck the parent's quoted 'case'.\n\nKeep all original gates.\n"
    loop["brief"].write_text(note)
    out = call(loop, "--body-file", str(loop["brief"]), mode="verify", dry=dry)
    assert out.returncode == 0, out.stdout + out.stderr
    command = dry_create(out) if dry else next(cmd for cmd in logged(loop) if cmd[3] == "create")
    body = value(command, "--body")
    assert body == (f"Independent RE-verifier at `{NEW_HEAD}`. The previous review ({SOURCE}) found: "
                    f"{loop['run']['summary']}\n\n## Parent note\n\n{note}"
                    f"\n\n## Earlier review\n\n{loop['card']['body']}")
    if not dry:
        cards = json.loads(loop["db"].with_suffix(".cards.json").read_text())
        assert cards["t_00000002"]["body"] == body


def test_reverify_parent_note_follows_full_multi_paragraph_summary(loop):
    summary = "FAIL: first para.\n\n1. Second para fix."
    loop["run"]["summary"] = summary
    note = "NOTE-TEXT"
    loop["brief"].write_text(note)
    out = call(loop, "--body-file", str(loop["brief"]), mode="verify")
    assert out.returncode == 0, out.stdout + out.stderr
    body = value(dry_create(out), "--body")
    assert body == (f"Independent RE-verifier at `{NEW_HEAD}`. The previous review ({SOURCE}) found: "
                    f"{summary}\n\n## Parent note\n\n{note}"
                    f"\n\n## Earlier review\n\n{loop['card']['body']}")
    assert body.index("1. Second para fix.") < body.index("## Parent note")


@pytest.mark.parametrize("mode", ["normal", "fix", "verify"])
def test_success_says_not_dispatched_but_dry_run_does_not(loop, mode):
    out = call(loop, mode=mode, dry=False)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stdout.splitlines().count("Not dispatched. Run: hermes kanban --board demo dispatch") == 1
    assert not any(command[3] == "dispatch" for command in logged(loop))
    out = call(loop, mode=mode)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "Not dispatched." not in out.stdout
    assert not any(command[3] == "dispatch" for command in logged(loop))


@pytest.mark.parametrize("class_name", ["app-code", "review"])
@pytest.mark.parametrize("dry", [False, True])
def test_reverify_reuses_source_assignee_model_and_provider_not_class_route(loop, class_name, dry):
    data = json.loads(loop["routes"].read_text())
    data["classes"]["review"] = data["verifiers"]["gpt"]
    loop["routes"].write_text(json.dumps(data))
    loop["card"].update(assignee="earlier-reviewer", model_override="claude-earlier-pin",
                        provider_override="anthropic")
    out = call(loop, "--class", class_name, mode="verify", dry=dry)
    assert out.returncode == 0, out.stdout + out.stderr
    command = dry_create(out) if dry else next(cmd for cmd in logged(loop) if cmd[3] == "create")
    assert value(command, "--assignee") == "earlier-reviewer"
    assert value(command, "--model") == "claude-earlier-pin"
    assert value(command, "--provider") == "anthropic"
    if not dry:
        pin = next(cmd for cmd in logged(loop) if cmd[3] == "set-model")
        assert pin[5:] == ["claude-earlier-pin", "--provider", "anthropic"]
        cards = json.loads(loop["db"].with_suffix(".cards.json").read_text())
        assert cards["t_00000002"]["assignee"] == "earlier-reviewer"
        assert cards["t_00000002"]["model_override"] == "claude-earlier-pin"
        assert cards["t_00000002"]["provider_override"] == "anthropic"


@pytest.mark.parametrize("field,bad", [("model_override", None), ("model_override", ""),
                                      ("model_override", "bad model"), ("provider_override", None),
                                      ("assignee", None)])
def test_reverify_requires_source_pin_and_assignee_without_guessing(loop, field, bad):
    loop["card"][field] = bad
    out = call(loop, mode="verify", dry=False)
    assert out.returncode == 1, out.stdout + out.stderr
    assert "Earlier verifier" in out.stderr
    assert ("model pin" if field == "model_override" else field.removesuffix("_override")) in out.stderr
    assert "no card created" in out.stderr
    assert [cmd[3] for cmd in logged(loop)] == ["show"]


@pytest.mark.parametrize("mode", ["fix", "verify"])
def test_real_filing_create_pin_and_readback_use_selected_lane(loop, mode):
    out = call(loop, mode=mode, dry=False, *["--created-by", "parent-wave", "--max-runtime", "3600"])
    assert out.returncode == 0, out.stdout + out.stderr
    commands = logged(loop)
    assert [command[3] for command in commands] == ["show", "create", "set-model", "show"]
    create = commands[1]
    assert value(create, "--assignee") == ("builder" if mode == "fix" else "reviewer")
    task_id = json.loads(next(line.removeprefix("/kanban-watch ") for line in out.stdout.splitlines()
                             if line.startswith("/kanban-watch ")))["tasks"][0]
    stored = json.loads(loop["db"].with_suffix(".cards.json").read_text())[task_id]
    assert stored["created_by"] == "parent-wave" and stored["max_runtime_seconds"] == 3600
    assert stored["workspace_kind"] == "dir" and stored["workspace_path"] == str(loop["tree"])
    assert stored["body"] == value(create, "--body")


def test_fake_show_omits_stored_max_runtime(loop):
    created = subprocess.run(["hermes", "kanban", "--board", "demo", "create", "Runtime contract",
                              "--assignee", "builder", "--max-runtime", "3600", "--json"],
                             capture_output=True, text=True, timeout=20)
    assert created.returncode == 0, created.stderr
    task_id = json.loads(created.stdout)["id"]
    stored = json.loads(loop["db"].with_suffix(".cards.json").read_text())[task_id]
    assert stored["max_runtime_seconds"] == 3600
    shown = subprocess.run(["hermes", "kanban", "--board", "demo", "show", task_id, "--json"],
                           capture_output=True, text=True, timeout=20)
    assert shown.returncode == 0, shown.stderr
    task = json.loads(shown.stdout)["task"]
    assert task["id"] == task_id and task["assignee"] == "builder"
    assert "max_runtime_seconds" not in task


@pytest.mark.parametrize("mode", ["normal", "fix", "verify"])
def test_max_runtime_filing_succeeds_when_show_omits_runtime(loop, mode):
    out = call(loop, "--max-runtime", "1800", mode=mode, dry=False)
    assert out.returncode == 0, out.stdout + out.stderr
    assert out.stderr == ""
    assert "/kanban-watch " in out.stdout
    watch = json.loads(next(line.removeprefix("/kanban-watch ") for line in out.stdout.splitlines()
                            if line.startswith("/kanban-watch ")))
    assert watch == {"board": "demo", "tasks": ["t_00000002"]}
    creates = [command for command in logged(loop) if command[3] == "create"]
    assert len(creates) == 1 and value(creates[0], "--max-runtime") == "1800"
    stored = json.loads(loop["db"].with_suffix(".cards.json").read_text())[watch["tasks"][0]]
    assert stored["max_runtime_seconds"] == 1800


@pytest.mark.parametrize("flag,argument", [("--created-by", "parent-wave"), ("--max-runtime", "3600")])
def test_filing_gap_passthroughs(loop, flag, argument):
    out = call(loop, flag, argument, mode="normal", dry=False)
    assert out.returncode == 0, out.stderr
    create = logged(loop)[0]
    assert value(create, flag) == argument
    cards = json.loads(loop["db"].with_suffix(".cards.json").read_text())
    card = cards["t_00000002"]
    field = "created_by" if flag == "--created-by" else "max_runtime_seconds"
    assert card[field] == (argument if field == "created_by" else int(argument))


@pytest.mark.parametrize("kind", [None, "dir", "worktree"])
def test_existing_directory_defaults_to_dir_but_can_be_overridden(loop, kind):
    extra = ["--worktree", str(loop["tree"])]
    if kind:
        extra += ["--workspace-kind", kind]
    out = call(loop, *extra, mode="normal", dry=False)
    assert out.returncode == 0, out.stderr
    expected_kind = kind or "dir"
    assert value(logged(loop)[0], "--workspace") == f"{expected_kind}:{loop['tree']}"
    assert f"Workspace: {expected_kind}:" in out.stdout


@pytest.mark.parametrize("mode", ["fix", "verify"])
@pytest.mark.parametrize("status", ["running", "review", "blocked", "ready", "archived"])
def test_unfinished_verdict_refused(loop, mode, status):
    loop["card"]["status"] = status
    out = call(loop, mode=mode)
    assert out.returncode != 0 and "done" in out.stderr
    assert not any(command[3] == "create" for command in logged(loop))


@pytest.mark.parametrize("head", ["short", "g" * 40, "a" * 39, "a" * 41, " " + HEAD, HEAD + "\n"])
def test_invalid_reverify_head_is_rejected_before_read(loop, head):
    out = call(loop, "--head", head, mode="verify")
    assert out.returncode != 0 and "40 hex" in out.stderr
    assert logged(loop) == []


@pytest.mark.parametrize("mode", ["fix", "verify"])
def test_empty_summary_refused(loop, mode):
    loop["run"]["summary"] = ""
    out = call(loop, mode=mode)
    assert out.returncode != 0 and "summary" in out.stderr
    assert not any(command[3] == "create" for command in logged(loop))


@pytest.mark.parametrize("fix_list", [None, "1. Text fix.\n2. Next fix."])
def test_fix_list_fallback_or_text_is_preserved(loop, fix_list):
    loop["run"]["metadata"].pop("fix_list")
    if fix_list is not None:
        loop["run"]["metadata"]["fix_list"] = fix_list
    out = call(loop)
    assert (fix_list or loop["run"]["summary"]) in value(dry_create(out), "--body")


def test_summary_head_fallback_without_metadata(loop):
    loop["run"]["summary"] += f" at {HEAD}\n1. Repair the value."
    loop["run"]["metadata"] = {}
    body = value(dry_create(call(loop)), "--body")
    assert f"Failing head: `{HEAD}`" in body
    assert "1. Repair the value." in body


def test_latest_run_is_used_not_stale_result_or_array_order(loop):
    old = {"id": 1, "ended_at": 50, "summary": "PASS old review", "metadata": {"head_sha": NEW_HEAD}}
    body = value(dry_create(call(loop, runs=[loop["run"], old])), "--body")
    assert loop["run"]["summary"] in body and HEAD in body
    assert "PASS old review" not in body


@pytest.mark.parametrize("missing", ["workspace", "head", "fixes"])
def test_missing_or_malformed_fix_evidence_refused(loop, missing):
    if missing == "workspace":
        loop["card"]["workspace_path"] = None
    elif missing == "head":
        loop["run"]["metadata"]["head_sha"] = "bad-sha"
    else:
        loop["run"]["metadata"]["fix_list"] = {"not": "a list"}
    out = call(loop)
    assert out.returncode != 0
    assert "unrecognized arguments" not in out.stderr
    assert not any(command[3] == "create" for command in logged(loop))


def test_from_verdict_requires_explicit_body_file(loop):
    seed(loop)
    out = subprocess.run([sys.executable, str(loop["script"]), "--board", "demo", "--class", "app-code",
                          "--routes", str(loop["routes"]), "--from-verdict", SOURCE, "--dry-run"],
                         capture_output=True, text=True, timeout=20)
    assert out.returncode != 0 and "--body-file" in out.stderr
    assert logged(loop) == []


@pytest.mark.parametrize("mode", ["fix", "verify"])
def test_source_show_failure_never_creates_or_claims_creation_may_have_happened(loop, mode):
    loop["monkeypatch"].setenv("FAKE_FAIL", "show")
    out = call(loop, mode=mode, dry=False)
    assert out.returncode != 0 and "show" in out.stderr
    assert "Creation may have reached" not in out.stderr
    assert all(command[3] == "show" for command in logged(loop))


def test_same_family_verifier_refused(loop):
    data = json.loads(loop["routes"].read_text())
    data["verifiers"]["gpt"]["provider"] = "compatible"
    loop["routes"].write_text(json.dumps(data))
    out = call(loop, mode="verify")
    assert out.returncode != 0 and "resolve routes" in out.stderr
    assert logged(loop) == []


@pytest.mark.parametrize("flag,argument,message", [("--max-runtime", "0", "positive"),
                                                    ("--max-runtime", "-1", "positive"),
                                                    ("--created-by", "", "created-by"),
                                                    ("--workspace-kind", "dir", "--worktree")])
def test_invalid_filing_options_refused_before_read(loop, flag, argument, message):
    out = call(loop, flag, argument, mode="normal")
    assert out.returncode != 0 and message in out.stderr
    assert logged(loop) == []


@pytest.mark.parametrize("field,bad", [("created_by", "other"), ("workspace_kind", "worktree"),
                                      ("workspace_path", "/other/workspace")])
def test_option_readback_must_match(loop, field, bad):
    loop["monkeypatch"].setenv("FAKE_SHOW", json.dumps({field: bad}))
    out = call(loop, "--created-by", "parent-wave", "--max-runtime", "3600",
               "--worktree", str(loop["tree"]), mode="normal", dry=False)
    assert out.returncode != 0 and "read-back" in out.stderr
    assert "/kanban-watch " not in out.stdout
