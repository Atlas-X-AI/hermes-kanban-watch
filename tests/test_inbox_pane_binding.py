"""Prepare-to-inbox binding with two fake windows and disposable state only."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from conftest import PLUGIN
from test_single_watcher import launch, launch_env
from test_watcher_discord import discord_env


@pytest.fixture
def shared_tmux(launch_env, tmp_path, monkeypatch):
    fake = tmp_path / "bin/tmux"
    fake.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ['PANE_CALLS']).open('a') as log:
    log.write(json.dumps(args) + '\\n')
# Two windows: the parent's pane and a different agent in the active window.
if args == ['list-panes', '-t', '=desk:', '-F', '#{pane_id}']:
    print(os.environ.get('ACTIVE_PANE', '%43'))
elif args == ['list-panes', '-s', '-t', '=desk:', '-F', '#{pane_id} #{pane_pid}']:
    print(os.environ.get('SESSION_PANES', '%42 4200\\n%43 4300'))
else:
    sys.exit(1)
''')
    fake.chmod(0o755)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("PANE_CALLS", str(tmp_path / "pane-calls"))
    return launch_env


def tick(receipt):
    return subprocess.run([sys.executable, str(PLUGIN / "scripts/watcher_tick.py"),
                           "--config", receipt["config"]],
                          capture_output=True, text=True, timeout=5)


def rows(inbox):
    with sqlite3.connect(inbox) as conn:
        return conn.execute("select target,notify_target from messages").fetchall()


@pytest.mark.parametrize("active", ["%42", "%43"])
@pytest.mark.parametrize("source", ["explicit", "environment"])
def test_shared_windows_pin_parent_not_active_window(shared_tmux, monkeypatch, active, source):
    _, inbox = shared_tmux
    monkeypatch.setenv("ACTIVE_PANE", active)
    extra = ["--pane", "%42"] if source == "explicit" else []
    # Explicit --pane must win even over a different inherited pane.
    monkeypatch.setenv("TMUX_PANE", "%43" if source == "explicit" else "%42")
    out = launch("--prepare-only", "--cards", "t_done", *extra)
    assert out.returncode == 0, out.stderr
    receipt = json.loads(out.stdout)
    config = json.loads(Path(receipt["config"]).read_text())
    assert (config["pane"], config["pane_pid"]) == ("%42", 4200)
    assert not rows(inbox)
    # Neither the helper's inherited pane nor a changed active window may retarget it.
    monkeypatch.setenv("TMUX_PANE", "%43")
    monkeypatch.setenv("ACTIVE_PANE", "%43" if active == "%42" else "%42")
    result = tick(receipt)
    assert result.returncode == 0 and "STOP final" in result.stdout, result.stderr
    assert rows(inbox) == [("desk", "=desk:.%42")]
    calls = [json.loads(line) for line in Path(os.environ["PANE_CALLS"]).read_text().splitlines()]
    assert len(calls) == 2 and all("-s" in call for call in calls)


@pytest.mark.parametrize("pane", [None, "", "%999", "42", "%42\n", "=desk:.%42"])
def test_shared_windows_without_valid_parent_refuse_before_state(shared_tmux, monkeypatch, pane):
    log, inbox = shared_tmux
    if pane is not None:
        monkeypatch.setenv("TMUX_PANE", pane)
    out = launch("--prepare-only", "--cards", "t_done")
    assert out.returncode != 0
    assert not (Path(os.environ["XDG_STATE_HOME"]) / "hermes-kanban-watch").exists()
    assert not log.exists() and not rows(inbox)


@pytest.mark.parametrize("panes", ["%43 4300", "%42 9999\n%43 4300", "%42 0\n%43 4300"])
def test_missing_or_replaced_parent_never_redirects(shared_tmux, monkeypatch, panes):
    _, inbox = shared_tmux
    monkeypatch.setenv("TMUX_PANE", "%42")
    out = launch("--prepare-only", "--cards", "t_done")
    assert out.returncode == 0, out.stderr
    receipt = json.loads(out.stdout)
    config = json.loads(Path(receipt["config"]).read_text())
    monkeypatch.setenv("SESSION_PANES", panes)
    monkeypatch.setenv("TMUX_PANE", "%43")
    before = inbox.read_bytes()
    result = tick(receipt)
    assert result.returncode == 5 and "delivery UNKNOWN" in result.stderr
    assert inbox.read_bytes() == before and not rows(inbox)
    relay = json.loads(Path(config["relay_state"]).read_text())
    assert relay["pending"]["reason"] == "final" and "last_receipt" not in relay
    assert not json.loads(Path(config["lease"]).read_text())["stopped"]


def test_changed_pane_root_refuses_even_if_agent_is_reparented(shared_tmux, monkeypatch):
    _, inbox = shared_tmux
    monkeypatch.setenv("TMUX_PANE", "%42")
    receipt = json.loads(launch("--prepare-only", "--cards", "t_done").stdout)
    monkeypatch.setenv("SESSION_PANES", "%42 9999\n%43 4300")
    monkeypatch.setenv("PANE_PROCESSES", "9999 1 Mon Nov 2 08:00:00 2026 zsh S\n"
                       "4201 9999 Mon Nov 2 08:01:00 2026 claude S")
    before = inbox.read_bytes()
    result = tick(receipt)
    assert result.returncode == 5 and "delivery UNKNOWN" in result.stderr
    assert inbox.read_bytes() == before and not rows(inbox)


@pytest.mark.parametrize("field", ["pane", "pane_pid", "agent_pid", "agent_start"])
def test_unpinned_config_cannot_resolve_a_new_target(shared_tmux, monkeypatch, field):
    _, inbox = shared_tmux
    monkeypatch.setenv("TMUX_PANE", "%42")
    out = launch("--prepare-only", "--cards", "t_done")
    assert out.returncode == 0, out.stderr
    receipt = json.loads(out.stdout)
    config = json.loads(Path(receipt["config"]).read_text())
    config.pop(field)
    Path(receipt["config"]).write_text(json.dumps(config))
    monkeypatch.setenv("SESSION_PANES", "%43 4300")
    result = tick(receipt)
    assert result.returncode == 5 and "delivery UNKNOWN" in result.stderr
    assert not rows(inbox)


def test_one_pane_prepare_records_target_without_environment(shared_tmux, monkeypatch):
    _, inbox = shared_tmux
    monkeypatch.setenv("SESSION_PANES", "%42 4200")
    out = launch("--prepare-only", "--cards", "t_done")
    assert out.returncode == 0, out.stderr
    receipt = json.loads(out.stdout)
    config = json.loads(Path(receipt["config"]).read_text())
    assert (config["pane"], config["pane_pid"]) == ("%42", 4200)
    assert tick(receipt).returncode == 0
    assert rows(inbox) == [("desk", "=desk:.%42")]


@pytest.mark.parametrize("external", [None, "%99"])
def test_external_caller_accepts_an_unambiguous_one_pane_target(shared_tmux, monkeypatch, external):
    _, inbox = shared_tmux
    monkeypatch.setenv("SESSION_PANES", "%42 4200")
    if external:
        monkeypatch.setenv("TMUX_PANE", external)
    out = launch("--prepare-only", "--cards", "t_done")
    assert out.returncode == 0, out.stderr
    receipt = json.loads(out.stdout)
    config = json.loads(Path(receipt["config"]).read_text())
    assert (config["pane"], config["agent_pid"]) == ("%42", 4201)
    assert tick(receipt).returncode == 0
    assert rows(inbox) == [("desk", "=desk:.%42")]


def test_explicit_foreign_pane_never_falls_back(shared_tmux, monkeypatch):
    _, inbox = shared_tmux
    monkeypatch.setenv("SESSION_PANES", "%42 4200")
    out = launch("--prepare-only", "--cards", "t_done", "--pane", "%99")
    assert out.returncode != 0
    assert not rows(inbox)


@pytest.mark.parametrize("change", ["exited", "replaced", "reused-pid", "moved", "ambiguous"])
def test_agent_change_inside_unchanged_shell_refuses_delivery(shared_tmux, monkeypatch, change):
    _, inbox = shared_tmux
    monkeypatch.setenv("TMUX_PANE", "%42")
    out = launch("--prepare-only", "--cards", "t_done")
    assert out.returncode == 0, out.stderr
    receipt = json.loads(out.stdout)
    root = "4200 1 Mon Nov 2 08:00:00 2026 zsh S"
    agent = "4201 4200 Mon Nov 2 08:01:00 2026 claude S"
    trees = {
        "exited": root,
        "replaced": root + "\n4202 4200 Mon Nov 2 08:02:00 2026 claude S",
        "reused-pid": root + "\n4201 4200 Mon Nov 2 08:02:00 2026 claude S",
        "moved": root + "\n4201 9999 Mon Nov 2 08:01:00 2026 claude S",
        "ambiguous": root + "\n" + agent + "\n4202 4200 Mon Nov 2 08:02:00 2026 claude S",
    }
    monkeypatch.setenv("PANE_PROCESSES", trees[change])
    before = inbox.read_bytes()
    result = tick(receipt)
    assert result.returncode == 5 and "delivery UNKNOWN" in result.stderr
    assert inbox.read_bytes() == before and not rows(inbox)
    config = json.loads(Path(receipt["config"]).read_text())
    relay = json.loads(Path(config["relay_state"]).read_text())
    assert relay["pending"]["reason"] == "final" and "last_receipt" not in relay


def test_bare_shell_is_not_an_agent_at_prepare_time(shared_tmux, monkeypatch):
    log, inbox = shared_tmux
    monkeypatch.setenv("TMUX_PANE", "%42")
    monkeypatch.setenv("PANE_PROCESSES", "4200 1 Mon Nov 2 08:00:00 2026 zsh S")
    out = launch("--prepare-only", "--cards", "t_done")
    assert out.returncode != 0
    assert not log.exists() and not rows(inbox)
    assert not (Path(os.environ["XDG_STATE_HOME"]) / "hermes-kanban-watch").exists()


@pytest.fixture
def shared_discord(shared_tmux, discord_env):
    return discord_env


@pytest.mark.parametrize("failure", ["channel-unresolved", "sender-failed", "final"])
def test_every_parent_inbox_path_uses_recorded_pane(shared_discord, monkeypatch, failure):
    _, inbox, _ = shared_discord
    monkeypatch.setenv("TMUX_PANE", "%42")
    route = ["--discord-session", "desk"] if failure == "channel-unresolved" else [
        "--discord-channel", "123456789012345678"]
    out = launch("--prepare-only", "--cards", "t_done", *route)
    assert out.returncode == 0, out.stderr
    receipt = json.loads(out.stdout)
    monkeypatch.setenv("TMUX_PANE", "%43")
    if failure == "sender-failed":
        monkeypatch.setenv("FAKE_DISCORD_MODE", "1")
    result = tick(receipt)
    assert result.returncode == (5 if failure == "sender-failed" else 0), result.stderr
    expected = [("desk", "=desk:.%42")] * (2 if failure == "channel-unresolved" else 1)
    assert rows(inbox) == expected


def test_native_delivery_does_not_resolve_tmux(shared_tmux):
    _, inbox = shared_tmux
    out = launch("--prepare-only", "--delivery", "native", "--cards", "t_done")
    assert out.returncode == 0, out.stderr
    receipt = json.loads(out.stdout)
    assert tick(receipt).returncode == 0
    assert not Path(os.environ["PANE_CALLS"]).exists() and not rows(inbox)
