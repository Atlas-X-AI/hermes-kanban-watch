"""Additional refusal controls and legacy/quoted-input preservation."""
import json

import pytest

from test_file_card_verdict import (HEAD, SOURCE, call, dry_create, logged, loop, value)


@pytest.mark.parametrize("flag", ["--from-verdict", "--reverify"])
def test_mode_flags_are_recognised_not_unknown(loop, flag):
    # Include all legacy required options: origin/main must say unknown flag,
    # rather than merely complaining about its old required title/body fields.
    extra = [flag, SOURCE]
    if flag == "--reverify":
        extra += ["--head", HEAD]
    out = call(loop, *extra, mode="normal")
    assert out.returncode != 0
    assert "unrecognized arguments" not in out.stderr
    assert "--title is generated" in out.stderr
    assert logged(loop) == []


def test_reverify_accepts_finished_pass(loop):
    loop["run"]["summary"] = "PASS: original review passed"
    command = dry_create(call(loop, mode="verify"))
    assert value(command, "--assignee") == "reviewer"
    assert loop["run"]["summary"] in value(command, "--body")


@pytest.mark.parametrize("metadata", [[], "", False, ["not a mapping"]])
def test_malformed_metadata_refused(loop, metadata):
    loop["run"]["metadata"] = metadata
    out = call(loop, mode="verify")
    assert out.returncode != 0 and "metadata" in out.stderr
    assert all(command[3] == "show" for command in logged(loop))


def test_empty_fix_gates_refused(loop):
    loop["brief"].write_text(" \n")
    out = call(loop)
    assert out.returncode != 0 and "gates" in out.stderr
    assert not any(command[3] == "create" for command in logged(loop))


def test_already_numbered_fix_list_not_double_numbered(loop):
    loop["run"]["metadata"]["fix_list"] = ["1. Repair value.", "2. Preserve gate."]
    body = value(dry_create(call(loop)), "--body")
    assert "1. Repair value.\n2. Preserve gate." in body
    assert "1. 1." not in body


def test_latest_unfinished_attempt_refused_even_when_task_done(loop):
    latest = {"id": 4, "ended_at": None, "summary": "FAIL unfinished", "metadata": {}}
    out = call(loop, runs=[loop["run"], latest])
    assert out.returncode != 0 and "finished" in out.stderr
    assert not any(command[3] == "create" for command in logged(loop))


def test_legacy_result_summary_supported_without_runs(loop):
    loop["card"]["result"] = f"FAIL at {HEAD}\n1. Legacy correction."
    body = value(dry_create(call(loop, runs=[])), "--body")
    assert "1. Legacy correction." in body and HEAD in body


@pytest.mark.parametrize("head", ["a" * 39, "g" * 40, "a" * 41])
def test_bad_metadata_head_refused_without_falling_back_to_summary(loop, head):
    loop["run"]["metadata"]["head_sha"] = head
    loop["run"]["summary"] += f" at {HEAD}"
    out = call(loop)
    assert out.returncode != 0 and "40 hex" in out.stderr
    assert not any(command[3] == "create" for command in logged(loop))


def test_ambiguous_summary_head_refused(loop):
    loop["run"]["metadata"].pop("head_sha")
    loop["run"]["summary"] += f" at {HEAD}, compared to {'f' * 40}"
    out = call(loop)
    assert out.returncode != 0 and "unambiguously" in out.stderr


def test_missing_workspace_kind_target_refused_in_reverify(loop):
    loop["card"]["workspace_path"] = None
    out = call(loop, "--workspace-kind", "dir", mode="verify")
    assert out.returncode != 0 and "workspace" in out.stderr
    assert not any(command[3] == "create" for command in logged(loop))


def test_default_future_path_is_worktree(loop, tmp_path):
    future = tmp_path / "future tree"
    out = call(loop, "--worktree", str(future), mode="normal", dry=False)
    assert out.returncode == 0, out.stderr
    assert value(logged(loop)[0], "--workspace") == f"worktree:{future}"
    assert not future.exists()


def test_dry_run_preserves_quotes_in_workspace_and_gates(loop, tmp_path):
    quoted = tmp_path / "tree 'one"
    quoted.mkdir()
    loop["card"]["workspace_path"] = str(quoted)
    loop["brief"].write_text("Only edit 'one'; $(literal text) is not executed.\n")
    out = call(loop)
    command = dry_create(out)
    assert value(command, "--workspace") == f"dir:{quoted}"
    assert value(command, "--body").endswith(loop["brief"].read_text())
    assert all(command[3] == "show" for command in logged(loop))


def test_actual_filing_source_is_not_modified(loop):
    out = call(loop, dry=False)
    assert out.returncode == 0, out.stderr
    cards = json.loads(loop["db"].with_suffix(".cards.json").read_text())
    assert cards[SOURCE] == loop["card"]
    assert len(cards) == 2
