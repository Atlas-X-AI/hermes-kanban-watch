import json
import os
import re
import shutil
import subprocess
import sys
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
    assert "hermes-kanban-live" in source and "live_cmd" in source
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
    assert "`hermes-kanban-live cards" in out["calls"][0]["prompt"]
    assert all(c["options"]["agentType"] == "hermes-kanban-watch:kanban-watcher" for c in out["calls"])
    out = run_workflow(tmp_path, {"live_cmd": "python3 ~/plugin/scripts/kanban_live.py"})
    assert "`python3 ~/plugin/scripts/kanban_live.py cards" in out["calls"][0]["prompt"]


def test_workflow_agent_types_are_namespaced_or_overridden(tmp_path):
    source = (PLUGIN / "workflows/kanban-watch.js").read_text()
    values = re.findall(r"agentType:\s*([^,}\n]+)", source)
    assert values and all(value in {"AGENT_TYPE", "options.agentType"} for value in values), values
    out = run_workflow(tmp_path, {})
    assert out["calls"] and out["result"]["read_status"] == "OK"
    assert all(call["options"]["agentType"] == "hermes-kanban-watch:kanban-watcher"
               for call in out["calls"])


@pytest.mark.parametrize("agent_type", ["local_watcher-2", "other-plugin:watcher_2", "A1:B2"])
def test_workflow_agent_type_override(tmp_path, agent_type):
    out = run_workflow(tmp_path, {"agent_type": agent_type})
    assert out["calls"] and out["result"]["read_status"] == "OK"
    assert all(call["options"]["agentType"] == agent_type for call in out["calls"])


@pytest.mark.parametrize("agent_type", ["", ":watcher", "plugin:", "a:b:c", "a/b", "a.b",
                                        "watcher;id", "two words", "watcher\n", "watcher\r",
                                        "watcher\t", "é", None, 12, {}])
def test_workflow_agent_type_validation(tmp_path, agent_type):
    out = run_workflow(tmp_path, {"agent_type": agent_type})
    assert "agent_type" in out["result"]["error"]
    assert not out["calls"]


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


def check_feature_docs(plugin):
    readme = (plugin / "README.md").read_text()
    assert readme.count("\n## Features\n") == 1
    assert readme.index("\n## Features\n") < readme.index("\n## Development checks\n")
    features = readme.split("\n## Features\n", 1)[1].split("\n## ", 1)[0]
    rule = "<!-- A new feature adds one file and one bullet, appended at the end of this list. -->"
    assert features.lstrip().startswith(rule)
    bullets = [line for line in features.splitlines() if line.startswith("- ")]
    links = re.findall(r"^- \[[^\]]+\]\((docs/[a-z0-9]+(?:-[a-z0-9]+)*\.md)\): [^\n]+$",
                       features, re.M)
    assert bullets and len(links) == len(bullets), "Use one docs link and description per bullet"
    assert re.findall(r"\[[^\]]+\]\(([^)]+)\)", features) == links
    files = sorted(path.relative_to(plugin).as_posix()
                   for path in (plugin / "docs").rglob("*") if path.is_file())
    assert files, "Feature docs must not be empty"
    assert sorted(links) == files, "Feature docs must be linked exactly once: " + repr((links, files))
    for link in links:
        assert (plugin / link).is_file(), "Unresolved feature link: " + link
    development = readme.split("\n## Development checks\n", 1)[1].split("\n## ", 1)[0]
    assert not re.search(r"^### ", development, re.M), "Feature section under Development checks"


def test_feature_docs_layout():
    check_feature_docs(PLUGIN)


def test_round_two_skill_pointers():
    skill_path = PLUGIN / "skills/kanban-watch/SKILL.md"
    skill = skill_path.read_text()
    assert len(skill.splitlines()) <= 150
    links = re.findall(r"\[[^\]]+\]\((../../docs/[^)]+)\)", skill)
    for name in ("wave-report", "verify-pair", "needs-loop", "pr-merge-receipt", "watcher-token-cost",
                 "duplicate-workers-parent-questions", "fix-and-reverify", "workflow-wait-rounds",
                 "standing-answers"):
        link = "../../docs/" + name + ".md"
        assert links.count(link) == 1, link
        assert (skill_path.parent / link).is_file(), link
    for line in skill.splitlines():
        assert len(re.findall(r"\]\(../../docs/", line)) <= 1, "One feature pointer per line"
    assert (ROOT / "README.md").read_text().count("(plugin/README.md#features)") == 1


@pytest.mark.parametrize("change", ["unlinked", "duplicate", "unresolved", "development"])
def test_feature_docs_reject_invalid_layout(tmp_path, change):
    check_feature_docs(PLUGIN)
    plugin = tmp_path / "plugin"
    shutil.copytree(PLUGIN / "docs", plugin / "docs")
    readme = (PLUGIN / "README.md").read_text()
    bullet = re.search(r"^- \[[^\]]+\]\(docs/[^)]+\): .+$", readme, re.M).group()
    if change == "unlinked":
        (plugin / "docs/extra.md").write_text("# Extra\n")
    elif change == "duplicate":
        readme = readme.replace(bullet, bullet + "\n" + bullet)
    elif change == "unresolved":
        readme = readme.replace(bullet, "- [Missing](docs/missing.md): Missing file.")
    else:
        readme += "\n### Misplaced feature\n"
    (plugin / "README.md").write_text(readme)
    with pytest.raises(AssertionError):
        check_feature_docs(plugin)


def test_feature_docs_known_red_unlinked_file(tmp_path):
    # A plain temp copy, with no git history, exercises the real pytest test.
    copy = tmp_path / "copy"
    shutil.copytree(PLUGIN / "docs", copy / "plugin/docs")
    shutil.copyfile(PLUGIN / "README.md", copy / "plugin/README.md")
    (copy / "tests").mkdir()
    for name in ("test_workflow.py", "conftest.py", "helpers.py"):
        shutil.copyfile(ROOT / "tests" / name, copy / "tests" / name)
    command = [sys.executable, "-m", "pytest", "-q", "tests/test_workflow.py::test_feature_docs_layout"]
    # The child checks only this repo's test, not unrelated installed plugins.
    env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    green = subprocess.run(command, cwd=copy, env=env, capture_output=True, text=True, timeout=30)
    assert green.returncode == 0, green.stdout + green.stderr
    (copy / "plugin/docs/extra.md").write_text("# Extra\n")
    red = subprocess.run(command, cwd=copy, env=env, capture_output=True, text=True, timeout=30)
    print("Known-red temp copy:", copy)
    print(red.stdout)
    assert red.returncode == 1, red.stdout + red.stderr
    assert "1 failed" in red.stdout and "docs/extra.md" in red.stdout


def test_public_contract_metadata_agent_skill_hooks():
    manifest = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    assert manifest["name"] == "hermes-kanban-watch" and manifest["version"] == "0.4.1"
    assert manifest["author"]["name"] and manifest["description"]
    hook = json.loads((PLUGIN / "hooks/hooks.json").read_text())["hooks"]["PostToolUse"][0]
    assert hook["matcher"] == "Bash"
    assert "${CLAUDE_PLUGIN_ROOT}/hooks/kanban_watch_nudge.py" in hook["hooks"][0]["command"]
    agent = (PLUGIN / "agents/kanban-watcher.md").read_text()
    assert "tools: Bash, Read" in agent and "model: sonnet" in agent and "## Never" in agent
    assert "watcher_tick.py" in agent and "UNKNOWN" in agent
    assert "background" in agent and "Workflow is legacy" in agent
    skill = (PLUGIN / "skills/kanban-watch/SKILL.md").read_text()
    assert "--prepare-only" in skill and "15 minutes" in skill
    assert "Luna" in skill and "GLM" in skill and "doctrine/running-a-kanban.md" in skill
    assert "Workflow({" not in skill
    assert len(skill.splitlines()) <= 150 and "/kanban-watch" in skill
    readme = (ROOT / "README.md").read_text()
    for document in (readme, (PLUGIN / "README.md").read_text()):
        assert "Checked against a real Hermes Agent v0.21.2 board" in document
        assert "`tests/test_real_hermes.py`; CI uses a fake Hermes CLI" in document
        assert 'hermes kanban boards create demo\nhermes kanban --board demo create "Build the feature" --assignee builder' in document
        # Only the upstream CLI uses a positional title. file_card.py owns --title.
        hermes_commands = re.findall(r"hermes kanban[^\n`]*", document)
        assert hermes_commands and all("--title" not in command for command in hermes_commands)
    for document in (readme, (PLUGIN / "README.md").read_text(), skill,
                     (ROOT / "SECURITY.md").read_text()):
        assert "hermes kanban --board default init" in document
        assert "hermes kanban boards create <slug>" in document
    assert 'hermes kanban --board demo create "Build the feature" --assignee builder' in skill
    assert "--plugin-dir" in readme and "--fix" in readme and "subscription" in readme


@pytest.mark.parametrize("document_path", ["README.md", "plugin/README.md"])
def test_public_contract_rejects_inline_title_flag(tmp_path, monkeypatch, document_path):
    test_public_contract_metadata_agent_skill_hooks()  # The unmodified documents must pass.
    original = ROOT / document_path
    copy = tmp_path / "README.md"
    copy.write_text(original.read_text() +
                    '\nFor example, run `hermes kanban --board demo create --title "X"` to file work.\n')
    read_text = Path.read_text

    def planted_read_text(path, *args, **kwargs):
        return read_text(copy if path == original else path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", planted_read_text)
    with pytest.raises(AssertionError):
        test_public_contract_metadata_agent_skill_hooks()


@pytest.mark.parametrize("command", ["kanban-live\n", "kanban-live\r", "kanban-live\t"])
def test_workflow_rejects_trailing_control_characters(tmp_path, command):
    out = run_workflow(tmp_path, {"live_cmd": command})
    assert "live_cmd" in out["result"]["error"] and not out["calls"]


@pytest.mark.parametrize("scenario", ["waiting", "resume", "new", "ignore-new"])
@pytest.mark.parametrize("agent_type", [None, "custom:watcher"])
def test_workflow_state_machine(tmp_path, scenario, agent_type):
    module_copy(tmp_path)
    harness = tmp_path / "watch.mjs"
    harness.write_text((ROOT / "tests/watch_scenarios.mjs").read_text())
    out = subprocess.run(["node", str(harness), scenario, json.dumps(agent_type)],
                         capture_output=True, text=True, timeout=10)
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
    # Commissioned integration names, not private machine paths or arbitrary prose.
    sender_path = b'.claude/skills/messaging-hayden/bin/send-hayden.sh'
    integration_tokens = {
        "plugin/scripts/watcher_tick.py": (sender_path,),
        "plugin/skills/kanban-watch/SKILL.md": (sender_path, b'messaging-hayden'),
        "plugin/docs/single-watcher.md": (sender_path, b'astra-context-layer'),
        "tests/test_watcher_discord.py": (b'send-hayden.sh', b'astra_context_layer'),
    }

    def private(content, path=""):
        if path in ci_paths:
            content = ci.sub(b"PUBLIC_WORKFLOW", content)
        for token in integration_tokens.get(path, ()):
            exact = rb'(?<![A-Za-z0-9_.-])' + re.escape(token) + rb'(?![A-Za-z0-9_.-])'
            content = re.sub(exact, b"PUBLIC_INTEGRATION", content)
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
    for path, tokens in integration_tokens.items():
        for token in tokens:
            assert not private(token, path)
            assert private(token, "README.md")  # The name is not allowed estate-wide.
            assert private(token + b"-private", path)
            assert private(b"/home/example/" + token, path)
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
