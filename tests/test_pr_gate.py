"""PR receipts use disposable boards and a view-only gh stub, never live services."""
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from conftest import PLUGIN, ROOT
from helpers import create_board

HEAD = "a" * 40
STALE = "b" * 40
REPO = "example/project"
SCRIPT = PLUGIN / "scripts/pr_gate.py"


def check_run(conclusion: str | None = "SUCCESS", status="COMPLETED", name="pytest"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion}


@pytest.fixture
def gate_case(isolated_home, tmp_path, monkeypatch):
    _, hermes = isolated_home
    db = create_board(hermes / "kanban/boards/gate/kanban.db")
    with sqlite3.connect(db) as con:
        con.execute("alter table task_runs add column metadata text")
        con.executemany("insert into tasks(id,title,assignee,status,current_run_id) values(?,?,?,?,?)", [
            ("t_worker", "Build", "builder", "done", 1),
            ("t_verifier", "Review", "reviewer", "done", 2),
        ])
        con.executemany("insert into task_runs(id,task_id,status,model_used,provider_used,"
                        "ended_at,summary,metadata) values(?,?,?,?,?,?,?,?)", [
            (1, "t_worker", "done", "gpt-actual", "openai", 100, "Implemented", None),
            (2, "t_verifier", "done", "claude-actual", "anthropic", 120,
             f"PASS inspected {HEAD}", None),
        ])
    calls = tmp_path / "gh-calls.jsonl"
    payload = tmp_path / "pr.json"
    payload.write_text(json.dumps({"headRefOid": HEAD, "statusCheckRollup": [check_run()],
                                   "mergeStateStatus": "CLEAN"}))
    stub = tmp_path / "gh-stub"
    stub.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ['PR_GATE_TEST_CALLS']).open('a') as out:
    out.write(json.dumps(args) + '\\n')
if args[:6] != ['pr', 'view', '7', '--repo', 'example/project', '--json']:
    sys.exit(91)
if len(args) != 7 or args[6] not in ('headRefOid,statusCheckRollup', 'headRefOid,statusCheckRollup,mergeStateStatus'):
    sys.exit(91)
if os.environ.get('PR_GATE_TEST_GH_ERROR'):
    print('private subprocess error, must not be echoed', file=sys.stderr)
    sys.exit(1)
print(Path(os.environ['PR_GATE_TEST_PAYLOAD']).read_text())
''')
    stub.chmod(0o755)
    monkeypatch.setenv("PR_GATE_GH", str(stub))
    monkeypatch.setenv("PR_GATE_TEST_CALLS", str(calls))
    monkeypatch.setenv("PR_GATE_TEST_PAYLOAD", str(payload))
    monkeypatch.delenv("PR_GATE_TEST_GH_ERROR", raising=False)
    ref = os.environ.get("PR_GATE_TEST_REF")
    if ref:
        export = tmp_path / "base-plugin"
        for name in ("scripts/pr_gate.py", "scripts/kanban_live.py", "scripts/route.py",
                     "scripts/wave_status.py", "templates/routes.yaml"):
            result = subprocess.run(["git", "show", f"{ref}:plugin/{name}"],
                                    cwd=ROOT, capture_output=True, check=True, timeout=10)
            (export / name).parent.mkdir(parents=True, exist_ok=True)
            (export / name).write_bytes(result.stdout)
        monkeypatch.setattr(sys.modules[__name__], "SCRIPT", export / "scripts/pr_gate.py")
    return {"db": db, "calls": calls, "payload": payload, "hermes": hermes}


def gate(*args):
    return subprocess.run([sys.executable, str(SCRIPT), "--board", "gate", "--worker", "t_worker",
                           "--verifier", "t_verifier", "--repo", REPO, "--pr", "7", *args],
                          capture_output=True, text=True, timeout=10)


def update(case, query, params=()):
    with sqlite3.connect(case["db"]) as con:
        con.execute(query, params)


def pr(case, **fields):
    data = json.loads(case["payload"].read_text())
    data.update(fields)
    case["payload"].write_text(json.dumps(data))


def refused(out, code, check):
    assert out.returncode == code, (out.stdout, out.stderr)
    data = json.loads(out.stdout)
    assert data["status"] == ("UNKNOWN" if code == 3 else "FAIL")
    assert data["failed_check"] == check
    assert data["reason"]
    assert "merge_command" not in data
    assert "gh pr merge" not in out.stdout + out.stderr
    assert "Traceback" not in out.stderr


def test_all_good_receipt_and_only_read_only_gh_calls(gate_case):
    before = gate_case["db"].read_bytes()
    out = gate("--format", "json")
    assert out.returncode == 0, (out.stdout, out.stderr)
    data = json.loads(out.stdout)
    assert data["status"] == "PASS"
    assert data["worker"] == {"id": "t_worker", "run_id": 1, "model_used": "gpt-actual",
                              "provider_used": "openai", "family": "gpt"}
    assert data["verifier"] == {"id": "t_verifier", "run_id": 2, "model_used": "claude-actual",
                                "provider_used": "anthropic", "family": "claude"}
    assert data["verdict"] == f"PASS inspected {HEAD}"
    assert data["head_sha"] == HEAD
    assert data["ci"] == [{"name": "pytest", "result": "SUCCESS"}]
    assert data["merge_command"] == f"gh pr merge 7 --repo {REPO} --squash --match-head-commit {HEAD}"
    calls = [json.loads(line) for line in gate_case["calls"].read_text().splitlines()]
    assert calls == [["pr", "view", "7", "--repo", REPO, "--json",
                      "headRefOid,statusCheckRollup,mergeStateStatus"]]
    assert all(not {"merge", "comment", "ready"}.intersection(call) for call in calls)
    assert gate_case["db"].read_bytes() == before
    assert not (gate_case["hermes"] / "profiles").exists()


@pytest.mark.parametrize("control,failed_check", [
    ("stale_summary_sha", "head_sha"), ("same_family", "families"),
    ("fail_verdict", "verdict"), ("failing_ci", "ci"),
])
def test_known_red_controls(gate_case, control, failed_check):
    if control == "stale_summary_sha":
        update(gate_case, "update task_runs set summary=? where id=2", (f"PASS reviewed {STALE}",))
    elif control == "same_family":
        update(gate_case, "update task_runs set provider_used='openai' where id=2")
    elif control == "fail_verdict":
        update(gate_case, "update task_runs set summary=? where id=2", (f"FAIL reviewed {HEAD}",))
    else:
        pr(gate_case, statusCheckRollup=[check_run("FAILURE")])
    refused(gate("--format", "json"), 1, failed_check)


def test_markdown_receipt_and_default_format(gate_case):
    out = gate()
    assert out.returncode == 0, out.stderr
    for text in ("PASS", "t_verifier", "gpt-actual", "openai", "claude-actual", "anthropic",
                 HEAD, "pytest", "SUCCESS", f"gh pr merge 7 --repo {REPO} --squash --match-head-commit {HEAD}"):
        assert text in out.stdout
    assert gate("--format", "md").stdout == out.stdout


@pytest.mark.parametrize("status", ["review", "running", "blocked", "archived"])
def test_verifier_must_be_done(gate_case, status):
    update(gate_case, "update tasks set status=? where id='t_verifier'", (status,))
    refused(gate("--format", "json"), 1, "verifier_status")


@pytest.mark.parametrize("summary", ["FAIL", " PASS reviewed", "Review: PASS", "pass reviewed", ""])
def test_verdict_must_start_with_literal_pass(gate_case, summary):
    update(gate_case, "update task_runs set summary=? where id=2", (summary,))
    refused(gate("--format", "json"), 1, "verdict")


def test_metadata_sha_takes_precedence_over_summary(gate_case):
    update(gate_case, "update task_runs set summary=?, metadata=? where id=2",
           (f"PASS old mention {STALE}", json.dumps({"head_sha": HEAD})))
    out = gate("--format", "json")
    assert out.returncode == 0, out.stdout
    assert json.loads(out.stdout)["head_sha"] == HEAD
    update(gate_case, "update task_runs set summary=?, metadata=? where id=2",
           (f"PASS {HEAD}", json.dumps({"head_sha": STALE})))
    refused(gate("--format", "json"), 1, "head_sha")


@pytest.mark.parametrize("metadata", [None, "{}", '{"unrelated": "value"}'])
def test_first_full_summary_sha_is_used_without_metadata_head(gate_case, metadata):
    update(gate_case, "update task_runs set summary=?, metadata=? where id=2",
           (f"PASS {'c' * 41} short abcdef1 then {HEAD} and {STALE}", metadata))
    assert gate("--format", "json").returncode == 0
    update(gate_case, "update task_runs set summary=? where id=2", (f"PASS first {STALE}, then {HEAD}",))
    refused(gate("--format", "json"), 1, "head_sha")


def test_legacy_board_without_metadata_column(gate_case):
    update(gate_case, "alter table task_runs drop column metadata")
    assert gate().returncode == 0


@pytest.mark.parametrize("metadata", ["not json", "[]", '{"head_sha": null}', '{"head_sha": "abc"}',
                                      '{"head_sha": 12}'])
def test_unreadable_or_invalid_metadata_never_falls_back(gate_case, metadata):
    update(gate_case, "update task_runs set metadata=? where id=2", (metadata,))
    refused(gate("--format", "json"), 3, "head_sha")


@pytest.mark.parametrize("summary", ["PASS no SHA", f"PASS {'a' * 41}", f"PASS x{HEAD}x"])
def test_missing_or_partial_sha_is_unknown(gate_case, summary):
    update(gate_case, "update task_runs set summary=? where id=2", (summary,))
    refused(gate("--format", "json"), 3, "head_sha")


def test_summary_sha_beyond_reader_snip_is_not_lost(gate_case):
    update(gate_case, "update task_runs set summary=? where id=2", ("PASS " + "detail " * 100 + HEAD,))
    assert gate().returncode == 0


def test_latest_run_verdict_and_pair_must_match(gate_case):
    update(gate_case, "insert into task_runs(id,task_id,status,model_used,provider_used,ended_at,summary)"
           " values(3,'t_verifier','done','claude-new','anthropic',130,?)", (f"FAIL {HEAD}",))
    refused(gate("--format", "json"), 1, "verdict")
    update(gate_case, "update task_runs set ended_at=NULL,summary=? where id=3", (f"PASS {HEAD}",))
    refused(gate("--format", "json"), 3, "verifier_run")


@pytest.mark.parametrize("result", ["SUCCESS", "NEUTRAL", "SKIPPED"])
def test_accepted_ci_conclusions(gate_case, result):
    pr(gate_case, statusCheckRollup=[check_run(result),
                                   {"__typename": "StatusContext", "context": "legacy", "state": "SUCCESS"}])
    out = gate("--format", "json")
    assert out.returncode == 0, out.stdout
    assert json.loads(out.stdout)["ci"][-1] == {"name": "legacy", "result": "SUCCESS"}


@pytest.mark.parametrize("item", [check_run("CANCELLED"), check_run("TIMED_OUT"),
                                  check_run("ACTION_REQUIRED"), check_run(None, "QUEUED"),
                                  check_run("SUCCESS", "IN_PROGRESS"),
                                  {"__typename": "StatusContext", "context": "old-ci", "state": "FAILURE"},
                                  {"__typename": "StatusContext", "context": "old-ci", "state": "PENDING"}])
def test_not_all_ci_green_refused(gate_case, item):
    pr(gate_case, statusCheckRollup=[check_run(), item])
    refused(gate("--format", "json"), 1, "ci")


@pytest.mark.parametrize("rollup", [None, [], {}, [None], [{}],
                                    [{"__typename": "CheckRun", "name": "empty"}],
                                    [{"__typename": "Other", "state": "SUCCESS"}]])
def test_absent_or_malformed_ci_is_unknown(gate_case, rollup):
    pr(gate_case, statusCheckRollup=rollup)
    refused(gate("--format", "json"), 3, "ci")


@pytest.mark.parametrize("payload", ["not json", "[]", "{}", '{"headRefOid": "short"}',
                                     json.dumps({"headRefOid": None, "statusCheckRollup": []})])
def test_malformed_gh_read_is_unknown(gate_case, payload):
    gate_case["payload"].write_text(payload)
    refused(gate("--format", "json"), 3, "pr_read")


def test_failed_or_missing_gh_is_unknown(gate_case, monkeypatch):
    monkeypatch.setenv("PR_GATE_TEST_GH_ERROR", "1")
    out = gate("--format", "json")
    refused(out, 3, "pr_read")
    assert "private subprocess error" not in out.stdout + out.stderr
    monkeypatch.setenv("PR_GATE_GH", str(gate_case["payload"].with_name("missing-gh")))
    refused(gate("--format", "json"), 3, "pr_read")


@pytest.mark.parametrize("missing", ["board", "corrupt_board", "worker", "verifier", "run", "actual_provider"])
def test_unknown_board_or_run_evidence(gate_case, missing):
    if missing == "board":
        gate_case["db"].unlink()
    elif missing == "corrupt_board":
        gate_case["db"].write_bytes(b"not sqlite")
    elif missing in ("worker", "verifier"):
        update(gate_case, "delete from tasks where id=?", (f"t_{missing}",))
        update(gate_case, "delete from task_runs where task_id=?", (f"t_{missing}",))
    elif missing == "run":
        update(gate_case, "delete from task_runs where id=2")
    else:
        update(gate_case, "update task_runs set provider_used='unmapped-host' where id=2")
    out = gate("--format", "json")
    assert out.returncode == 3, (out.stdout, out.stderr)
    assert json.loads(out.stdout)["status"] == "UNKNOWN"
    assert "gh pr merge" not in out.stdout + out.stderr
    assert "Traceback" not in out.stderr


def test_routes_override_reuses_provider_mapping(gate_case, tmp_path):
    routes = tmp_path / "routes.json"
    routes.write_text(json.dumps({"providers": {"openai": "shared", "anthropic": "shared"}}))
    refused(gate("--routes", str(routes), "--format", "json"), 1, "families")
    routes.write_text(json.dumps({"providers": {"openai": "one", "anthropic": "two"}}))
    assert gate("--routes", str(routes)).returncode == 0
    refused(gate("--routes", str(tmp_path / "missing"), "--format", "json"), 3, "families")


def test_gate_imports_and_calls_existing_verify_pair(gate_case, monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
    spec = importlib.util.spec_from_file_location("pr_gate_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.kanban_live, "HERMES", gate_case["hermes"])
    original = module.kanban_live.cmd_verify_pair
    seen = []

    def pair(args):
        seen.append((args.board, args.worker, args.verifier, args.families))
        return original(args)

    monkeypatch.setattr(module.kanban_live, "cmd_verify_pair", pair)
    assert module.main(["--board", "gate", "--worker", "t_worker", "--verifier", "t_verifier",
                        "--repo", REPO, "--pr", "7", "--format", "json"]) == 0
    assert seen == [("gate", "t_worker", "t_verifier", None)]
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"


def test_markdown_refusal_has_reason_and_no_merge_command(gate_case):
    pr(gate_case, statusCheckRollup=[check_run("FAILURE")])
    out = gate()
    assert out.returncode == 1
    assert "FAIL" in out.stdout and "ci" in out.stdout and "FAILURE" in out.stdout
    assert "gh pr merge" not in out.stdout + out.stderr


@pytest.mark.parametrize("state", ["DIRTY", "BLOCKED", "BEHIND", "DRAFT", "UNKNOWN", "HAS_HOOKS", "UNSTABLE"])
def test_merge_state_refuses_every_non_clean_state_by_default(gate_case, state):
    pr(gate_case, mergeStateStatus=state)
    out = gate("--format", "json")
    refused(out, 1, "merge_state")
    assert state in json.loads(out.stdout)["reason"]
    calls = [json.loads(line) for line in gate_case["calls"].read_text().splitlines()]
    assert "mergeStateStatus" in calls[-1][-1].split(",")
    out = gate()
    assert out.returncode == 1 and state in out.stdout
    assert "gh pr merge" not in out.stdout + out.stderr


def test_merge_state_allowances_are_repeatable_and_only_widen_named_states(gate_case):
    flags = ["--allow-merge-state", "HAS_HOOKS", "--allow-merge-state", "UNSTABLE"]
    for state in ("CLEAN", "HAS_HOOKS", "UNSTABLE"):
        pr(gate_case, mergeStateStatus=state)
        out = gate(*flags, "--format", "json")
        assert out.returncode == 0, out.stdout + out.stderr
        assert json.loads(out.stdout)["status"] == "PASS"
    pr(gate_case, mergeStateStatus="DIRTY")
    refused(gate(*flags, "--format", "json"), 1, "merge_state")
    for state in ("DIRTY", "CLEAN", "UNKNOWN"):
        out = gate("--allow-merge-state", state)
        assert out.returncode == 2 and "invalid choice" in out.stderr


@pytest.mark.parametrize("state", [None, "", 1, [], {}])
def test_merge_state_absent_or_malformed_is_unknown(gate_case, state):
    pr(gate_case, mergeStateStatus=state)
    refused(gate("--format", "json"), 3, "merge_state")
    data = json.loads(gate_case["payload"].read_text())
    del data["mergeStateStatus"]
    gate_case["payload"].write_text(json.dumps(data))
    refused(gate("--format", "json"), 3, "merge_state")


@pytest.mark.parametrize("repo,number", [("--bad", "7"), ("owner/repo;do-bad", "7"),
                                        (REPO, "--help"), (REPO, "0")])
def test_invalid_target_arguments_are_not_forwarded_to_gh(gate_case, repo, number):
    out = subprocess.run([sys.executable, str(SCRIPT), "--board", "gate", "--worker", "t_worker",
                          "--verifier", "t_verifier", f"--repo={repo}", f"--pr={number}"],
                         capture_output=True, text=True, timeout=10)
    assert out.returncode != 0
    assert "gh pr merge" not in out.stdout + out.stderr
    assert not gate_case["calls"].exists()
