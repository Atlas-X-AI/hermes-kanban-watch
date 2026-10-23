import json
import subprocess
import sys

import pytest

from conftest import PLUGIN


@pytest.mark.parametrize("command,output,board,ids", [
    ("hermes kanban create --board demo --title Test", "Created t_1234abcd\n", "demo", ["t_1234abcd"]),
    ("hermes kanban --board=demo swarm --title Test", "Swarm root: t_1234abcd\nWorkers: t_11111111, t_22222222\nVerifier: t_33333333\n", "demo", ["t_1234abcd", "t_11111111", "t_22222222", "t_33333333"]),
    ("HERMES_KANBAN_BOARD=default hermes kanban create --json", '{"id":"t_1234abcd"}', "default", ["t_1234abcd"]),
    ("hermes kanban swarm --board demo --json", '{"root_id":"t_1234abcd","worker_ids":["t_11111111"],"verifier_id":"t_22222222"}', "demo", ["t_1234abcd", "t_11111111", "t_22222222"]),
    ("hermes kanban create --board demo --json | jq -r .id", "t_1234abcd\n", "demo", ["t_1234abcd"]),
])
def test_hook_created_ids_and_board(command, output, board, ids):
    event = {"tool_name": "Bash", "tool_input": {"command": command}, "tool_response": {"stdout": output}}
    out = subprocess.run([sys.executable, str(PLUGIN / "hooks/kanban_watch_nudge.py")],
                         input=json.dumps(event), capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    note = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "/kanban-watch" in note and f'"{board}"' in note
    assert all(card in note for card in ids)
    assert "scriptPath" not in note and ".py" not in note and ".js" not in note


def test_hook_environment_board(tmp_path, monkeypatch, isolated_home):
    _, hermes = isolated_home
    (hermes / "kanban/boards/demo").mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "demo")
    event = {"tool_name": "Bash", "tool_input": {"command": "hermes kanban create --title Test"},
             "tool_response": "Created t_1234abcd"}
    out = subprocess.run([sys.executable, str(PLUGIN / "hooks/kanban_watch_nudge.py")],
                         input=json.dumps(event), capture_output=True, text=True, timeout=10)
    assert '"demo"' in json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]


@pytest.mark.parametrize("command", ["hermes kanban show t_1234abcd", "printf 'hermes kanban create'",
    "hermes kanban create --help", "cat <<'EOF'\nhermes kanban create\nEOF\n", "hermes kanban create 'unclosed"])
def test_hook_silent_for_unrelated_or_opaque_commands(command):
    out = subprocess.run([sys.executable, str(PLUGIN / "hooks/kanban_watch_nudge.py")],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "tool_response": "Created t_1234abcd"}),
        capture_output=True, text=True, timeout=10)
    assert out.returncode == 0 and not out.stdout


def test_hook_malformed_input_never_blocks():
    out = subprocess.run([sys.executable, str(PLUGIN / "hooks/kanban_watch_nudge.py")],
                         input="not json", capture_output=True, text=True, timeout=10)
    assert out.returncode == 0 and not out.stdout and not out.stderr
