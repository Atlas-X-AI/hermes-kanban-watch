"""Bounded agent contexts must not bound a healthy card's watch lifetime.

These fixtures replay deterministic external-agent replies, not live model calls.
The old exhaustion reply is the reported production failure. Snapshot fields
match the real reader's JSON; virtual reader timestamps avoid hours of sleeping.
"""
import json
import re
import subprocess

import pytest

from conftest import ROOT
from test_workflow import module_copy, run_workflow


def run_rounds(tmp_path, scenario, agent_type=None, source=None):
    if source is None:
        module_copy(tmp_path)
    else:
        prelude, marker, body = source.partition("const A = args || {}")
        assert marker
        (tmp_path / "workflow.mjs").write_text(
            prelude + "export async function run(args, agent, log) {\n" + marker + body + "\n}\n"
        )
    harness = tmp_path / "rounds.mjs"
    harness.write_text((ROOT / "tests/watch_scenarios.mjs").read_text())
    out = subprocess.run(
        ["node", str(harness), scenario, json.dumps(agent_type)],
        capture_output=True, text=True, timeout=10,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def event_calls(data):
    return [c for c in data["calls"] if "count_new" in c["options"]["schema"]["properties"]]


@pytest.mark.parametrize("agent_type", [None, "custom:watcher"])
def test_long_running_card_needs_three_rounds(tmp_path, agent_type):
    data = run_rounds(tmp_path, "round-long", agent_type)
    assert data["result"]["read_status"] == "OK", data["result"]
    assert [c["verdict"] for c in data["result"]["cards"]] == ["DONE"]
    assert data["eventCalls"] == 3 and data["snapshotCalls"] == 2
    rounds = event_calls(data)
    assert all(f"wait round {n}" in c["options"]["label"] for n, c in enumerate(rounds, 1))
    assert all("up to 13 times" in c["prompt"] for c in rounds)
    assert all(c["options"]["agentType"] == (agent_type or "hermes-kanban-watch:kanban-watcher")
               for c in data["calls"])
    assert all(c["prompt"].startswith("You only read. Never claim, complete, block, unblock, comment on,")
               for c in data["calls"])
    assert all("--from-start" not in c["prompt"] for c in rounds)
    # Only the original live row follows the worker; rounds do not duplicate it.
    assert sum("verdict" in c["options"]["schema"]["properties"] for c in data["calls"]) == 1
    live = next(c for c in data["calls"] if "verdict" in c["options"]["schema"]["properties"])
    assert "total watch deadline is Unix time 114400" in live["prompt"]
    assert "Before each wait-start/follow call" in live["prompt"]
    assert "shorten --max-seconds" in live["prompt"]


@pytest.mark.parametrize("status", ["triage", "todo", "ready", "scheduled"])
def test_queued_child_keeps_waiting_across_rounds(tmp_path, status):
    data = run_rounds(tmp_path, f"round-child-{status}")
    assert data["result"]["read_status"] == "OK", data["result"]
    assert [(c["id"], c["verdict"]) for c in data["result"]["cards"]] == [
        ("t_one", "DONE"), ("t_two", "DONE"),
    ]
    assert data["eventCalls"] == 4 and data["snapshotCalls"] == 2
    snapshots = [c for c in data["calls"] if c["options"]["label"].startswith("fresh snapshot")]
    assert all("--task 't_one' --task 't_two'" in c["prompt"] for c in snapshots)
    live = [c for c in data["calls"] if "verdict" in c["options"]["schema"]["properties"]]
    assert len(live) == 2
    assert not any("exit" in c["options"]["schema"]["properties"] for c in data["calls"])


@pytest.mark.parametrize("status", ["blocked", "review"])
def test_parked_card_keeps_waiting_across_rounds(tmp_path, status):
    data = run_rounds(tmp_path, f"round-parked-{status}")
    assert data["result"]["read_status"] == "OK", data["result"]
    assert [c["verdict"] for c in data["result"]["cards"]] == [status.upper(), "DONE"]
    assert data["eventCalls"] == 4 and data["snapshotCalls"] == 2
    assert all("Parked on blocked/review cards" in c["prompt"] for c in event_calls(data)[:3])
    assert any("resumed; re-arming" in message for message in data["logs"])


@pytest.mark.parametrize("status", ["blocked", "review"])
def test_parked_card_still_stops_at_total_deadline(tmp_path, status):
    data = run_rounds(tmp_path, f"round-parked-{status}-deadline")
    assert data["result"]["read_status"] == "UNKNOWN", data["result"]
    assert "deadline" in data["result"]["error"].lower()
    assert [c["verdict"] for c in data["result"]["cards"]] == [status.upper()]
    assert data["eventCalls"] == data["snapshotCalls"] == 3


@pytest.mark.parametrize("scenario,reason", [
    ("round-stale", "heartbeat"),
    ("round-second-stale", "t_two"),
    ("round-no-heartbeat", "heartbeat"),
    ("round-prior-heartbeat", "heartbeat"),
    ("round-future-heartbeat", "heartbeat"),
    ("round-unknown", "unrecognised"),
    ("round-not-found", "not_found"),
    ("round-wrong-run", "current run"),
    ("round-ended-run", "current run"),
    ("round-missing", "missing"),
    ("round-duplicate", "duplicate"),
    ("round-malformed", "malformed"),
    ("round-old-snapshot", "clock"),
    ("round-read-error", "permission denied"),
])
def test_unhealthy_snapshot_ends_after_one_round(tmp_path, scenario, reason):
    data = run_rounds(tmp_path, scenario)
    assert data["result"]["read_status"] == "UNKNOWN", data["result"]
    assert reason in data["result"]["error"].lower()
    assert data["eventCalls"] == data["snapshotCalls"] == 1


@pytest.mark.parametrize("scenario,budget,rounds", [
    ("round-deadline", 7100, 3), ("round-default-deadline", 14400, 5),
])
def test_total_deadline_ends_unknown(tmp_path, scenario, budget, rounds):
    data = run_rounds(tmp_path, scenario)
    assert data["result"]["read_status"] == "UNKNOWN", data["result"]
    assert "deadline" in data["result"]["error"].lower()
    assert data["eventCalls"] == rounds
    commands = event_calls(data)
    def number(pattern, call):
        match = re.search(pattern, call["prompt"])
        assert match, call["prompt"]
        return int(match[1])

    budgets = [number(r"--max-seconds (\d+)", c) * number(r"up to (\d+) times", c)
               for c in commands]
    assert sum(budgets) == budget
    assert all(number(r"up to (\d+) times", c) <= 13 for c in commands)


def test_finished_snapshot_needs_no_further_round(tmp_path):
    data = run_rounds(tmp_path, "round-finished")
    assert data["result"]["read_status"] == "OK", data["result"]
    assert [c["verdict"] for c in data["result"]["cards"]] == ["DONE"]
    assert data["eventCalls"] == data["snapshotCalls"] == 1


def test_finished_cards_need_no_heartbeat_while_other_cards_run(tmp_path):
    data = run_rounds(tmp_path, "round-mixed-long")
    assert data["result"]["read_status"] == "OK", data["result"]
    assert [c["verdict"] for c in data["result"]["cards"]] == ["DONE", "DONE"]
    assert data["eventCalls"] == 3 and data["snapshotCalls"] == 2


def test_all_finished_at_deadline_is_not_unknown(tmp_path):
    data = run_rounds(tmp_path, "round-at-deadline")
    assert data["result"]["read_status"] == "OK", data["result"]
    assert [c["verdict"] for c in data["result"]["cards"]] == ["DONE"]


def test_queued_rows_share_the_watch_deadline(tmp_path):
    data = run_rounds(tmp_path, "waiting")
    prompt = next(p for p in data["prompts"] if "Before every wait-any call" in p)
    assert "total watch deadline is Unix time 3701" in prompt
    assert "shorten --max-seconds" in prompt
    assert "cards:[] (no start observed)" in prompt


@pytest.mark.parametrize("value", [0, -1, 1.5, "14400", None, True, {}, [], 9007199254740992, 1e100])
def test_max_wait_seconds_validation(tmp_path, value):
    data = run_workflow(tmp_path, {"max_wait_seconds": value})
    assert "max_wait_seconds" in data["result"]["error"]
    assert not data["calls"]


def test_known_red_origin_main_exhaustion(tmp_path):
    # A depth-one PR checkout has only the merge ref. Do not fetch over the
    # network from a unit test; the local control remains an explicit card gate.
    ref = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", "refs/remotes/origin/main"],
        cwd=ROOT, capture_output=True, text=True, timeout=10,
    )
    if ref.returncode == 1:
        pytest.skip("origin/main absent in shallow checkout; run the known-red control in a full checkout")
    assert ref.returncode == 0, ref.stderr
    base = subprocess.run(
        ["git", "show", "origin/main:plugin/workflows/kanban-watch.js"],
        cwd=ROOT, capture_output=True, text=True, timeout=10, check=True,
    ).stdout
    if "If every call exits 3" in base:
        pytest.skip("origin/main already contains the bounded-round protocol")
    data = run_rounds(tmp_path, "round-long", source=base)
    assert data["result"]["read_status"] == "UNKNOWN"
    assert "exhausted its 13" in data["result"]["error"]
    assert data["eventCalls"] == 1 and data["snapshotCalls"] == 0
