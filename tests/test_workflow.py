import json
import subprocess
from pathlib import Path

import pytest

from conftest import PLUGIN, ROOT


def module_copy(tmp_path):
    # Workflow scripts have a module metadata prelude and an async-function body.
    # Keep the entire real source, including exports, and wrap only that body.
    source = (PLUGIN / "workflows/kanban-watch.js").read_text()
    prelude, marker, body = source.partition("const A = args || {}")
    assert marker
    copy = tmp_path / "workflow.mjs"
    copy.write_text(prelude + "export async function run(args, agent, log) {\n" + marker + body + "\n}\n")
    return copy


def test_workflow_module_parse(tmp_path):
    copy = module_copy(tmp_path)
    out = subprocess.run(["node", "--check", str(copy)], capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    source = (PLUGIN / "workflows/kanban-watch.js").read_text()
    assert source.startswith("// Read-only Kanban watchers:")
    assert "kanban-live" in source and "live_cmd" in source
    assert "const WAKE = 270" in source


def run_workflow(tmp_path, args, scenario="done"):
    copy = module_copy(tmp_path)
    harness = tmp_path / "harness.mjs"
    harness.write_text('''import {run} from './workflow.mjs';
const args = JSON.parse(process.argv[2]), scenario = process.argv[3];
const calls = [], logs = [];
const card = {id:'t_1234abcd',title:'Build',status:'done',runs:1,parents:[],retried:false};
let result;
try {
  result = await run(args, async (prompt, options) => {
    calls.push({prompt, options});
    if (options.schema.properties.cards && options.schema.properties.count) {
      if (scenario === 'denied') return {read_status:'UNKNOWN',error:'permission denied'};
      if (scenario === 'bad-clock') return {read_status:'OK',now:0,cards:[],count:0,stale_skipped:[]};
      return {read_status:'OK',now:100,cards:[card],count:1,stale_skipped:[]};
    }
    return {read_status:'OK',now:101,id:card.id,verdict:'DONE',model_that_ran:'test-model',
      summary:'Built',retried:false,warnings:{}};
  }, s => logs.push(s));
} catch (e) { result = {error:e.message}; }
console.log(JSON.stringify({result,calls,logs}));
''')
    out = subprocess.run(["node", str(harness), json.dumps(args), scenario],
                         capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_workflow_default_and_override_real_commands(tmp_path):
    out = run_workflow(tmp_path, {"tasks": ["t_1234abcd"]})
    assert out["result"]["read_status"] == "OK"
    assert out["result"]["cards"][0]["verdict"] == "DONE"
    assert "`kanban-live cards" in out["calls"][0]["prompt"]
    assert all(c["options"]["agentType"] == "kanban-watcher" for c in out["calls"])
    out = run_workflow(tmp_path, {"live_cmd": "python3 ~/plugin/scripts/kanban_live.py"})
    assert "`python3 ~/plugin/scripts/kanban_live.py cards" in out["calls"][0]["prompt"]


@pytest.mark.parametrize("command", ["kanban-live;id", "x\nwhoami", "$(id)", "x|cat", "x>out", "x&y", "", 12])
def test_workflow_live_command_validation(tmp_path, command):
    out = run_workflow(tmp_path, {"live_cmd": command})
    assert "live_cmd" in out["result"]["error"]
    assert not out["calls"]


@pytest.mark.parametrize("scenario", ["denied", "bad-clock"])
def test_workflow_unknown_envelope_cannot_become_done(tmp_path, scenario):
    out = run_workflow(tmp_path, {}, scenario)
    assert out["result"]["read_status"] == "UNKNOWN"
    assert "cards" not in out["result"] and len(out["calls"]) == 1


def test_public_contract_metadata_agent_skill_hooks():
    manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    assert manifest["name"] == "hermes-kanban-watch" and manifest["version"] == "0.1.0"
    assert manifest["author"]["name"] and manifest["description"]
    hook = json.loads((PLUGIN / "hooks/hooks.json").read_text())["hooks"]["PostToolUse"][0]
    assert hook["matcher"] == "Bash"
    assert "${CLAUDE_PLUGIN_ROOT}/hooks/kanban_watch_nudge.py" in hook["hooks"][0]["command"]
    agent = (PLUGIN / "agents/kanban-watcher.md").read_text()
    assert "tools: Bash, Read" in agent and "model: sonnet" in agent and "## Never" in agent
    assert "kanban-live follow" in agent and "UNKNOWN" in agent
    skill = (PLUGIN / "skills/kanban-watch/SKILL.md").read_text()
    assert len(skill.splitlines()) <= 150 and "/kanban-watch" in skill
    readme = (ROOT / "README.md").read_text()
    for document in (readme, (PLUGIN / "README.md").read_text()):
        assert "Checked against a real Hermes Agent v0.21.2 board" in document
        assert "`tests/test_real_hermes.py`; CI uses a fake Hermes CLI" in document
        assert 'hermes kanban boards create demo\nhermes kanban --board demo create "Build the feature" --assignee builder' in document
        assert "--title" not in document
    for document in (readme, (PLUGIN / "README.md").read_text(), skill,
                     (ROOT / "SECURITY.md").read_text()):
        assert "hermes kanban --board default init" in document
        assert "hermes kanban boards create <slug>" in document
    assert 'hermes kanban --board demo create "Build the feature" --assignee builder' in skill
    assert "--plugin-dir" in readme and "--fix" in readme and "subscription" in readme


@pytest.mark.parametrize("command", ["kanban-live\n", "kanban-live\r", "kanban-live\t"])
def test_workflow_rejects_trailing_control_characters(tmp_path, command):
    out = run_workflow(tmp_path, {"live_cmd": command})
    assert "live_cmd" in out["result"]["error"] and not out["calls"]


@pytest.mark.parametrize("scenario", ["waiting", "resume", "new", "ignore-new"])
def test_workflow_state_machine(tmp_path, scenario):
    module_copy(tmp_path)
    harness = tmp_path / "watch.mjs"
    harness.write_text((ROOT / "tests/watch_scenarios.mjs").read_text())
    out = subprocess.run(["node", str(harness), scenario], capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    data = json.loads(out.stdout)
    assert data["result"]["read_status"] == "OK"
    rows = data["rows"]
    live = [r for r in rows if r["kind"] == "live"]
    assert all(r["phase"] == "Build" for r in live)
    if scenario == "waiting":
        assert len([r for r in rows if r["kind"] == "wait"]) == 1
        assert sorted(r["id"] for r in live) == ["t_one", "t_two"]
    elif scenario == "resume":
        assert [r["id"] for r in live] == ["t_one", "t_one"]
        assert {r["phase"] for r in rows if r["kind"] == "flag"} == {"Needs you"}
        assert any("re-arming" in line for line in data["logs"])
    else:
        assert sorted(r["id"] for r in live) == (["t_one", "t_two"] if scenario == "new" else ["t_one"])


def test_no_private_paths():
    import re
    banned = re.compile(rb'/home/|anombyte|atlas|cortex|astra|moll-e|\.agents/skills|hayden', re.I)
    public = re.compile(rb'(?<![A-Za-z0-9_.-])(?:Atlas X AI|Atlas-X-AI|atlas-ai\.au)(?![A-Za-z0-9_.-])')
    ci = re.compile(rb'(?<![A-Za-z0-9_./-])' + re.escape(
        b'Atlas-X-AI/.github/.github/workflows/atlas-ci.yml@main') + rb'(?![A-Za-z0-9_./@-])')
    ci_paths = {".github/workflows/ci.yml", "README.md", "CONTRIBUTING.md"}

    def private(content, path=""):
        if path in ci_paths:
            content = ci.sub(b"PUBLIC_WORKFLOW", content)
        return banned.search(public.sub(b"PUBLIC", content))

    assert private(b"/home/example")  # Positive controls for the exact scanner.
    assert private(b"atlas-internal") and private(b"Atlas X AIprivate")
    assert private(b"atlas-x-ai") and private(b"atlas-ai.au.private")
    assert not private(b"Atlas X AI Atlas-X-AI https://atlas-ai.au")
    workflow = b'Atlas-X-AI/.github/.github/workflows/atlas-ci.yml@main'
    assert private(workflow, "plugin/agents/kanban-watcher.md")
    for path in ci_paths:
        assert not private(workflow, path)
        assert private(workflow + b"-private", path)
        assert private(workflow.replace(b"atlas-ci", b"atlas-private"), path)
    this_test = Path(__file__).resolve()
    paths = [p for p in ROOT.iterdir() if p.is_file() and p.name not in {"SPEC.md", ".git"}]
    for directory in (PLUGIN, ROOT / "tests", ROOT / "assets", ROOT / ".github", ROOT / ".claude-plugin"):
        paths.extend(p for p in directory.rglob("*") if p.is_file())
    assert PLUGIN / "scripts/kanban_live.py" in paths
    for path in paths:
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue  # Bytecode embeds the local checkout path; it is not public source.
        content = path.read_bytes()
        if path.resolve() == this_test:
            content = content.split(b"def test_no_private_paths():", 1)[0]
        assert not private(content, path.relative_to(ROOT).as_posix()), str(path.relative_to(ROOT))
