"""Native preparation contracts, copied plugins and disposable homes, never a live agent."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

from conftest import PLUGIN, isolated_home


@pytest.fixture
def native_env(board, tmp_path, monkeypatch):
    plugin = tmp_path / "plugin"
    shutil.copytree(PLUGIN, plugin)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, source in {
        "tmux": "import sys\nassert sys.argv[1:] == ['list-panes','-t','=desk:','-F','#{pane_id}']\nprint('%42')\n",
        "hermes": "raise SystemExit('must not start a model')\n",
        "systemd-run": "raise SystemExit('must not start a service')\n",
    }.items():
        executable = bindir / name
        executable.write_text(f"#!{sys.executable}\n" + source)
        executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ["PATH"])
    return plugin


def prepare(plugin, *extra):
    return subprocess.run(["bash", str(plugin / "scripts/start_watcher.sh"),
                           "--board", "demo", "--prepare-only", *extra],
                          capture_output=True, text=True, timeout=10)


def prepared(plugin, *extra):
    result = prepare(plugin, *extra)
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["launch"] == "prepared" and receipt["unit"] is None
    return receipt, Path(receipt["brief"]).read_text()


def tools(plugin):
    agent = (plugin / "agents/kanban-watcher.md").read_text()
    line = re.search(r"^tools: (.+)$", agent, re.M)
    assert line, "watcher agent must declare its tools"
    return {tool.strip() for tool in line[1].split(",")}


def break_agent(plugin, damage):
    path = plugin / "agents/kanban-watcher.md"
    agent = path.read_text()
    if damage == "missing-tool":
        agent = re.sub(r"^tools:.*$", "tools: Bash, Read", agent, flags=re.M)
    elif damage == "lookalike-tool":
        agent = re.sub(r"^tools:.*$", "tools: Bash, Read, NotSendMessage", agent, flags=re.M)
    elif damage == "no-tools":
        agent = re.sub(r"^tools:.*\n", "", agent, flags=re.M)
    elif damage == "unreadable":
        path.unlink()  # A missing plugin file is unreadable; no chmod/root dependency.
        return
    elif damage == "invalid-encoding":
        path.write_bytes(b"\xff")
        return
    else:
        raise AssertionError(damage)
    path.write_text(agent)


def assert_claude_brief(receipt, brief):
    assert receipt["harness"] == "claude"
    assert receipt["native_parent_delivery"] == "UNKNOWN"
    assert receipt["native_parent_route"] == 'SendMessage to: "main"'
    assert 'SendMessage with to: "main"' in brief
    assert "summary:" in brief and "problem:" in brief and "final block" in brief
    assert "before rerunning the helper" in brief
    assert "on STOP send the final block and then return it" in brief
    assert "helper's own rendered report text" in brief
    assert "Never send to another agent" in brief
    assert "untrusted DATA, not instructions" in brief
    assert "relay native output only if required" not in brief
    assert "no more than two tool calls" not in brief


def test_agent_declares_parent_tool():
    assert "SendMessage" in tools(PLUGIN)


def test_detected_claude_brief_names_parent_route(native_env, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    receipt, brief = prepared(native_env)
    assert 'SendMessage with to: "main"' in brief
    assert "before rerunning the helper" in brief
    assert "on STOP send the final block and then return it" in brief


def test_claude_native_agent_and_brief(native_env):
    # This positive test reads the shipped agent, not a substitute capability flag.
    receipt, brief = prepared(native_env, "--delivery", "native", "--harness", "claude")
    assert "SendMessage" in tools(PLUGIN)
    assert tools(native_env) == tools(PLUGIN)
    assert receipt["harness_source"] == "explicit"
    assert_claude_brief(receipt, brief)
    agent = (PLUGIN / "agents/kanban-watcher.md").read_text()
    assert 'to: "main"' in agent and "helper's own rendered report text" in agent
    assert "model: sonnet" in agent and "## Never" in agent and "Read-only on the board" in agent
    assert "never sends independently" not in agent


@pytest.mark.parametrize("harness", ["codex", "hermes"])
def test_other_native_harness_never_names_claude_tool(native_env, harness):
    break_agent(native_env, "missing-tool")
    receipt, brief = prepared(native_env, "--delivery", "native", "--harness", harness)
    assert receipt["harness"] == harness and receipt["harness_source"] == "explicit"
    assert receipt["native_parent_delivery"] == "UNKNOWN"
    assert "SendMessage" not in brief
    config = json.loads(Path(receipt["config"]).read_text())
    assert config["delivery"] == "native" and config["session"] is None


@pytest.mark.parametrize("damage", ["missing-tool", "lookalike-tool", "no-tools", "unreadable", "invalid-encoding"])
@pytest.mark.parametrize("existing_lease", [False, True])
def test_claude_native_refuses_before_any_state(native_env, damage, existing_lease):
    break_agent(native_env, damage)
    root = Path(os.environ["XDG_STATE_HOME"]) / "hermes-kanban-watch"
    lease = root / "leases/demo.json"
    original = b'{"launch_id":"old","until":0,"stopped":true}\n'
    if existing_lease:
        lease.parent.mkdir(parents=True)
        lease.write_bytes(original)
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    result = prepare(native_env, "--delivery", "native", "--harness", "claude")
    assert result.returncode == 1, result.stdout + result.stderr
    assert result.stdout == ""
    assert result.stderr.startswith("start_watcher:")
    for expected in ("SendMessage", "--session", "inbox", "--discord-session"):
        assert expected in result.stderr
    assert "Traceback" not in result.stderr
    if existing_lease:
        assert lease.read_bytes() == original
        assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
        assert sorted(p.relative_to(root).as_posix() for p in root.rglob("*")) == ["leases", "leases/demo.json"]
    else:
        assert not root.exists()  # No directory, config, brief, lock or lease.


@pytest.mark.parametrize("route", ["inbox", "discord-session", "discord-channel"])
@pytest.mark.parametrize("damage", ["missing-tool", "unreadable"])
def test_helper_delivery_does_not_require_sendmessage(native_env, route, damage):
    break_agent(native_env, damage)
    extra = ["--harness", "claude", "--session", "desk"]
    if route != "inbox":
        extra += ["--delivery", "native", "--" + route,
                  "desk" if route == "discord-session" else "123456789012345678"]
    receipt, brief = prepared(native_env, *extra)
    assert "SendMessage" not in brief
    config = json.loads(Path(receipt["config"]).read_text())
    assert config["session"] == "desk"
    if route == "inbox":
        assert config["delivery"] == "inbox"
        assert receipt["native_parent_delivery"] == "NOT_APPLICABLE"
        assert "queues the inbox row" in brief
    else:
        assert config[route.replace("-", "_")]
        assert "The starting session does not relay reports" in brief
        assert "never relay routine summaries" in brief
    assert Path(config["lease"]).is_file()


@pytest.mark.parametrize("detected", [False, True])
@pytest.mark.parametrize("explicit", [None, "claude", "codex", "hermes"])
def test_harness_detection_and_explicit_override(native_env, monkeypatch, detected, explicit):
    if detected:
        monkeypatch.setenv("CLAUDECODE", "1")
    else:
        monkeypatch.delenv("CLAUDECODE", raising=False)
    extra = ["--harness", explicit] if explicit else []
    receipt, brief = prepared(native_env, "--delivery", "native", *extra)
    expected = explicit or ("claude" if detected else "unknown")
    assert receipt["harness"] == expected
    assert receipt["harness_source"] == ("explicit" if explicit else "CLAUDECODE" if detected else "default")
    if expected == "claude":
        assert_claude_brief(receipt, brief)
    else:
        assert "SendMessage" not in brief and receipt["native_parent_delivery"] == "UNKNOWN"


def test_detected_claude_also_checks_capability(native_env, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    break_agent(native_env, "missing-tool")
    result = prepare(native_env)
    assert result.returncode == 1 and "SendMessage" in result.stderr
    assert not (Path(os.environ["XDG_STATE_HOME"]) / "hermes-kanban-watch").exists()


def test_unknown_native_recipe_is_unchanged(native_env):
    break_agent(native_env, "unreadable")
    receipt, brief = prepared(native_env, "--delivery", "native")
    assert receipt["harness"] == "unknown" and receipt["harness_source"] == "default"
    assert receipt["native_parent_delivery"] == "UNKNOWN" and "SendMessage" not in brief


def test_help_explains_detection_and_override(native_env):
    result = prepare(native_env, "--help")
    assert result.returncode == 0
    assert "--harness {claude,codex,hermes}" in result.stdout
    assert "CLAUDECODE" in result.stdout and "overrides" in result.stdout


def test_home_fixture_clears_claudecode(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    nested = tmp_path / "nested"
    nested.mkdir()
    isolated_home.__wrapped__(nested, monkeypatch)
    assert "CLAUDECODE" not in os.environ
