"""Watch-cost accounting uses disposable transcripts, never a live board or model."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugin"
SCRIPT = PLUGIN / "scripts/watch_cost.py"
FIXTURES = Path(__file__).parent / "fixtures/watch_cost"
CANARY = "WATCH_COST_MESSAGE_CANARY_8b319"


def run_cost(root, *args):
    return subprocess.run([sys.executable, str(SCRIPT), "--root", str(root), *map(str, args)],
                          capture_output=True, text=True, timeout=10)


def make_run(root, name="demo"):
    run = root / "project/session/subagents/workflows" / name
    run.mkdir(parents=True)
    return run


def install_fixture(run, name):
    for suffix in (".jsonl", ".meta.json"):
        source = FIXTURES / (name + suffix)
        if source.exists():
            shutil.copyfile(source, run / ("agent-" + name + suffix))


def write_rows(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def prompt(text="python3 kanban_live.py follow"):
    return {"type": "user", "message": {"content": text}}


def turn(message_id="m1", time="2026-10-20T00:00:01Z", model="claude-haiku-5-5", **usage):
    return {"type": "assistant", "timestamp": time,
            "message": {"id": message_id, "model": model,
                        "content": [{"type": "text", "text": CANARY}],
                        "usage": usage or {"input_tokens": 10, "output_tokens": 2}}}


@pytest.fixture
def transcript_root(tmp_path):
    root = tmp_path / "transcripts"
    root.mkdir()
    return root


@pytest.fixture
def accountant():
    spec = importlib.util.spec_from_file_location("watch_cost", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_plugin_script_is_shipped_without_seed_import():
    assert SCRIPT.is_file(), "the public plugin must ship watch_cost.py"
    text = SCRIPT.read_text()
    assert "from seed" not in text and "import seed" not in text


def test_ported_fixture_accounting(transcript_root):
    run = make_run(transcript_root)
    for name in ("follow", "cards", "scout", "cold-wake", "reviewer", "reviewer-cmd",
                 "meta-watcher", "non-watcher", "titled", "corrupt-other", "relay"):
        install_fixture(run, name)
    shutil.copyfile(FIXTURES / "journal.jsonl", run / "journal.jsonl")
    out = run_cost(transcript_root, "--run", "demo", "--json")
    assert out.returncode == 0, out.stderr
    roles = json.loads(out.stdout)["demo"]
    assert set(roles) == {"follow", "cards", "scout", "(unknown)"}
    follow = roles["follow"]["claude-sonnet-5"]
    assert follow == {"agents": 1, "turns": 2, "input_tokens": 30, "output_tokens": 7,
                      "cache_read_input_tokens": 300, "cache_write_5m_tokens": 1000,
                      "cache_write_1h_tokens": 200, "cold_wakes": 1,
                      "cold_cache_write_5m_tokens": 0, "cold_cache_write_1h_tokens": 200,
                      "weighted_total": 1745.0}
    cold = roles["follow"]["claude-sonnet-5-5"]
    assert cold["agents"] == 2 and cold["turns"] == 3
    assert cold["cold_wakes"] == 1 and cold["cold_cache_write_5m_tokens"] == 9009
    assert roles["cards"]["claude-haiku-5-5"]["agents"] == 2
    assert roles["cards"]["claude-haiku-5-5"]["cache_write_5m_tokens"] == 40
    assert roles["scout"]["claude-haiku-5-5"]["turns"] == 1
    assert roles["(unknown)"]["claude-haiku-5-5"]["input_tokens"] == 4
    assert "no recognised role" in out.stderr


@pytest.mark.parametrize("mode", [(), ("--json",), ("--workflow-run",)])
def test_canary_never_printed_in_any_output_mode(transcript_root, mode):
    run = make_run(transcript_root)
    install_fixture(run, "follow")
    # The ported fixture contains a canary in its user message, not merely in a test constant.
    assert CANARY in (FIXTURES / "follow.jsonl").read_text()
    if mode == ("--workflow-run",):
        mode = (*mode, run)
    out = run_cost(transcript_root, *mode)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() and ("30" in out.stdout or "input_tokens=30" in out.stdout)
    assert CANARY not in out.stdout + out.stderr


def test_workflow_run_is_one_counts_only_line(transcript_root):
    run = make_run(transcript_root)
    install_fixture(run, "follow")
    (run / "journal.jsonl").write_text('{"type":"started","agentId":"follow"}\n')
    other = make_run(transcript_root, "other")
    install_fixture(other, "cards")
    out = run_cost(transcript_root, "--workflow-run", run)
    assert out.returncode == 0, out.stderr
    assert out.stdout.splitlines() == [
        "agents=1 turns=2 input_tokens=30 output_tokens=7 cache_read_input_tokens=300 "
        "cache_write_5m_tokens=1000 cache_write_1h_tokens=200 cold_wakes=1"]


def test_default_root_uses_isolated_home(isolated_home):
    home, _ = isolated_home
    run = make_run(home / ".claude/projects")
    install_fixture(run, "cards")
    out = subprocess.run([sys.executable, str(SCRIPT), "--run", "demo", "--json"],
                         capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["demo"]["cards"]["claude-haiku-5-5"]["turns"] == 1


def test_repeatable_run_and_text_per_model_totals(transcript_root):
    for name in ("first", "second"):
        run = make_run(transcript_root, name)
        install_fixture(run, "cards")
        install_fixture(run, "follow")
    out = run_cost(transcript_root, "--run", "first", "--run", "second")
    assert out.returncode == 0, out.stderr
    rows = [line.split() for line in out.stdout.splitlines()]
    assert rows[0][0:4] == ["RUN", "MODEL", "AGENTS", "TURNS"]
    totals = {row[1]: row for row in rows[1:] if row[0] == "TOTAL"}
    assert totals["claude-haiku-5-5"][2:4] == ["2", "2"]
    assert totals["claude-sonnet-5"][2:4] == ["2", "4"]


def test_model_switch_dedup_and_no_nested_or_synthetic_usage(transcript_root):
    run = make_run(transcript_root)
    rows = [prompt(), turn(), turn(output_tokens=4),
            turn("m2", "2026-10-20T00:01:01Z", "claude-sonnet-5", input_tokens=20),
            turn("error", model="<synthetic>", input_tokens=0)]
    rows[1]["message"]["iterations"] = [{"usage": {"input_tokens": 999999}}]
    write_rows(run / "agent-one.jsonl", rows)
    out = run_cost(transcript_root, "--json")
    assert out.returncode == 0, out.stderr
    models = json.loads(out.stdout)["demo"]["follow"]
    assert set(models) == {"claude-haiku-5-5", "claude-sonnet-5"}
    assert models["claude-haiku-5-5"]["turns"] == 1
    assert models["claude-haiku-5-5"]["output_tokens"] == 4
    assert models["claude-sonnet-5"]["input_tokens"] == 20


@pytest.mark.parametrize("gap,expected", [(300, 0), (301, 1)])
def test_cold_wake_strict_boundary(transcript_root, gap, expected):
    run = make_run(transcript_root)
    minute, second = divmod(gap, 60)
    write_rows(run / "agent-one.jsonl", [prompt(), turn(time="2026-10-20T00:00:00Z"),
               prompt("tool result"), turn("m2", f"2026-10-20T00:{minute:02}:{second:02}Z")])
    out = run_cost(transcript_root, "--json")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["demo"]["follow"]["claude-haiku-5-5"]["cold_wakes"] == expected


@pytest.mark.parametrize("usage", [
    {"input_tokens": -1}, {"output_tokens": True}, {"input_tokens": "10"},
    {"cache_creation_input_tokens": 10, "cache_creation": {"ephemeral_5m_input_tokens": 9}},
    {"cache_creation": [1]},
])
def test_invalid_usage_fails_without_partial_totals(transcript_root, usage):
    run = make_run(transcript_root)
    write_rows(run / "agent-one.jsonl", [prompt(), turn(**usage)])
    out = run_cost(transcript_root, "--json")
    assert out.returncode == 2 and not out.stdout
    assert "Traceback" not in out.stderr and CANARY not in out.stderr


def test_corrupt_journal_warns_but_still_counts(transcript_root):
    run = make_run(transcript_root)
    install_fixture(run, "cards")
    (run / "journal.jsonl").write_text(CANARY + '\n["bad"]\n')
    out = run_cost(transcript_root, "--workflow-run", run)
    assert out.returncode == 0, out.stderr
    assert "agents=1 turns=1" in out.stdout
    assert "journal metadata skipped" in out.stderr and CANARY not in out.stderr


def test_corrupt_watcher_fails_without_message_text(transcript_root):
    run = make_run(transcript_root)
    write_rows(run / "agent-one.jsonl", [prompt(), turn()])
    with (run / "agent-one.jsonl").open("a") as stream:
        stream.write(CANARY + "\n")
    out = run_cost(transcript_root)
    assert out.returncode == 2 and not out.stdout
    assert "invalid JSON" in out.stderr and CANARY not in out.stderr


def test_since_selects_whole_agents_by_mtime_or_first_timestamp(transcript_root):
    run = make_run(transcript_root)
    for name, date, mtime in (("stale", "2026-10-19", 1),
                              ("timestamp", "2026-10-21", 1),
                              ("mtime", "2026-10-19", 2000000000)):
        path = run / f"agent-{name}.jsonl"
        write_rows(path, [prompt(), turn(time=date + "T00:00:00Z")])
        os.utime(path, (mtime, mtime))
    out = run_cost(transcript_root, "--since", "2026-10-20", "--json")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["demo"]["follow"]["claude-haiku-5-5"]["agents"] == 2


def test_namespaced_plugin_watcher_and_unknown_role(transcript_root):
    run = make_run(transcript_root)
    write_rows(run / "agent-one.jsonl", [prompt("Watch the card. " + CANARY), turn()])
    (run / "agent-one.meta.json").write_text('{"agentType":"hermes-kanban-watch:kanban-watcher"}')
    out = run_cost(transcript_root, "--json")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["demo"]["(unknown)"]["claude-haiku-5-5"]["agents"] == 1
    assert CANARY not in out.stdout + out.stderr


@pytest.mark.parametrize("args", [("--run", "missing"), ("--since", "not-a-date"),
                                   ("--workflow-run", "../outside"),
                                   ("--workflow-run", "missing"),
                                   ("--workflow-run", "demo", "--json"),
                                   ("--workflow-run", "demo", "--run", "demo")])
def test_bad_selection_is_an_error(transcript_root, args):
    make_run(transcript_root)
    out = run_cost(transcript_root, *args)
    assert out.returncode == 2 and not out.stdout
    assert "Traceback" not in out.stderr


@pytest.mark.parametrize("placement", ["project", "session", "subagents", "workflows", "run",
                                       "transcript", "journal", "meta"])
def test_symlinks_never_open_outside_root(transcript_root, tmp_path, accountant, monkeypatch, placement):
    run = make_run(transcript_root)
    install_fixture(run, "cards")
    outside = tmp_path / "outside"
    outside.mkdir()
    write_rows(outside / "data.jsonl", [prompt(), turn(input_tokens=999999)])
    (outside / "meta.json").write_text('{"agentType":"kanban-watcher"}')
    targets = {"project": transcript_root / "project",
               "session": transcript_root / "project/session",
               "subagents": transcript_root / "project/session/subagents",
               "workflows": run.parent, "run": run,
               "transcript": run / "agent-cards.jsonl", "journal": run / "journal.jsonl",
               "meta": run / "agent-cards.meta.json"}
    target = targets[placement]
    if target.is_dir():
        # Only temporary test data is moved, never a repository/worktree directory.
        target.rename(outside / "original")
        target.symlink_to(outside / "original", target_is_directory=True)
    else:
        if target.exists():
            target.unlink()
        target.symlink_to(outside / ("meta.json" if placement == "meta" else "data.jsonl"))
    real_open = os.open
    opened = []

    def audited_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        actual = Path(os.readlink(f"/proc/self/fd/{fd}"))
        assert actual.is_relative_to(transcript_root), f"opened outside root: {actual}"
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)
        opened.append(actual)
        return fd

    monkeypatch.setattr(os, "open", audited_open)
    with pytest.raises((OSError, ValueError)):
        with accountant.TranscriptRoot(transcript_root) as source:
            accountant.report(source)
    assert opened  # The audit observed the root open, not a vacuous no-op.


def test_journal_ids_cannot_escape_root(transcript_root, tmp_path):
    run = make_run(transcript_root)
    install_fixture(run, "cards")
    outside = tmp_path / "outside.jsonl"
    write_rows(outside, [prompt(), turn(input_tokens=999999)])
    write_rows(run / "journal.jsonl", [{"type": "started", "agentId": "../../outside"},
                                      {"type": "result", "agentId": ["bad"]},
                                      {"type": "started", "agentId": str(outside)}])
    out = run_cost(transcript_root, "--json")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["demo"]["cards"]["claude-haiku-5-5"]["input_tokens"] == 7


def test_read_only_and_ignores_unrelated_files(transcript_root):
    run = make_run(transcript_root)
    install_fixture(run, "cards")
    # These files are canaries, not credential files, and must never be opened.
    (run / "unrelated.txt").write_text(CANARY)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in transcript_root.rglob("*") if p.is_file()}
    out = run_cost(transcript_root, "--json")
    assert out.returncode == 0, out.stderr
    after = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in transcript_root.rglob("*") if p.is_file()}
    assert before == after
    assert json.loads(out.stdout)["demo"]["cards"]["claude-haiku-5-5"]["turns"] == 1


def test_unknown_model_error_does_not_echo_message_data(transcript_root):
    run = make_run(transcript_root)
    write_rows(run / "agent-one.jsonl", [prompt(), turn(model=CANARY)])
    out = run_cost(transcript_root, "--json")
    assert out.returncode == 2 and not out.stdout
    assert "no output ratio" in out.stderr and CANARY not in out.stderr


def test_empty_explicit_run_warns_with_zero_counts(transcript_root):
    run = make_run(transcript_root)
    install_fixture(run, "non-watcher")
    out = run_cost(transcript_root, "--run", "demo")
    assert out.returncode == 0, out.stderr
    assert "0 watcher agents among 1 transcripts" in out.stderr
    assert "demo - 0 0" in out.stdout


@pytest.mark.parametrize("command,role", [
    ('python3 "a path/kanban_live.py" needs', "needs"),
    ("`/scripts/kanban_live.py wait-any --task t_demo`", "wait-any"),
    ("python3 /scripts/wave_status.py --db board.db", "wave_status"),
])
def test_seed_command_forms(transcript_root, command, role):
    run = make_run(transcript_root)
    write_rows(run / "agent-one.jsonl", [prompt(command), turn()])
    out = run_cost(transcript_root, "--json")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["demo"][role]["claude-haiku-5-5"]["turns"] == 1


def test_relay_command_is_not_the_computed_task(transcript_root):
    run = make_run(transcript_root)
    write_rows(run / "agent-one.jsonl", [
        prompt("[Workflow harness — user request] python3 kanban_live.py cards " + CANARY),
        prompt("[Workflow harness — computed task] python3 kanban_live.py follow"), turn()])
    out = run_cost(transcript_root, "--json")
    assert out.returncode == 0, out.stderr
    assert set(json.loads(out.stdout)["demo"]) == {"follow"}
    assert CANARY not in out.stdout + out.stderr


def test_canary_cannot_become_a_role_label(transcript_root):
    run = make_run(transcript_root)
    write_rows(run / "agent-one.jsonl", [prompt("python3 kanban_live.py " + CANARY), turn()])
    (run / "agent-one.meta.json").write_text('{"agentType":"kanban-watcher"}')
    out = run_cost(transcript_root, "--json")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["demo"]["(unknown)"]["claude-haiku-5-5"]["input_tokens"] == 10
    assert CANARY not in out.stdout + out.stderr


def test_summary_counts_model_switching_agent_only_once(transcript_root):
    run = make_run(transcript_root)
    write_rows(run / "agent-one.jsonl", [prompt(), turn(), turn("m2", model="claude-sonnet-5")])
    out = run_cost(transcript_root, "--workflow-run", run.relative_to(transcript_root))
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("agents=1 turns=2 input_tokens=20 output_tokens=4 ")


def test_valid_open_audit_and_anchor_survives_root_replacement(transcript_root, tmp_path, accountant):
    run = make_run(transcript_root)
    install_fixture(run, "cards")
    with accountant.TranscriptRoot(transcript_root) as source:
        moved = tmp_path / "original-root"
        transcript_root.rename(moved)
        # A replacement pathname cannot redirect the already-open root descriptor.
        replacement = make_run(transcript_root)
        write_rows(replacement / "agent-foreign.jsonl", [prompt(), turn(input_tokens=999999)])
        data, _ = accountant.report(source)
    assert data["demo"]["cards"]["claude-haiku-5-5"]["input_tokens"] == 7
    assert "follow" not in data["demo"]


def test_files_without_message_ids_count_individually(transcript_root):
    run = make_run(transcript_root)
    rows = [prompt(), turn(), turn()]
    for row in rows[1:]:
        row["message"].pop("id")
    write_rows(run / "agent-one.jsonl", rows)
    out = run_cost(transcript_root, "--workflow-run", run)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("agents=1 turns=2 input_tokens=20 ")
