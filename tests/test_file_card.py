import json
import os
import shlex
import subprocess
import sys

import pytest

from conftest import PLUGIN
from helpers import create_board, fake_hermes


@pytest.fixture
def filing(tmp_path, isolated_home, monkeypatch, routes):
    _, hermes = isolated_home
    bindir = tmp_path / "bin"
    fake_hermes(bindir)
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_CALLS", str(calls))
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.defpath)
    db = create_board(hermes / "kanban/boards/demo/kanban.db")
    routefile = tmp_path / "routes.json"
    routefile.write_text(json.dumps(routes))
    body = tmp_path / "brief.txt"
    body.write_text("Build this safely.\nPreserve 'quotes' and $(literal text).\n")
    return db, calls, routefile, body


def file_card(filing, *extra):
    _, _, routefile, body = filing
    return subprocess.run([sys.executable, str(PLUGIN / "scripts/file_card.py"),
                           "--board", "demo", "--class", "app-code", "--title", "A 'quoted' title; not shell",
                           "--body-file", str(body), "--routes", str(routefile), *extra],
                          capture_output=True, text=True, timeout=20)


def logged(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def watch_args(output):
    return json.loads(next(line.removeprefix("/kanban-watch ") for line in output.splitlines()
                           if line.startswith("/kanban-watch ")))


def test_create_pin_readback_and_watch_args(filing, tmp_path):
    db, calls, _, body = filing
    worktree = tmp_path / "future worktree"
    before = db.read_bytes()
    out = file_card(filing, "--worktree", str(worktree))
    assert out.returncode == 0, out.stdout + out.stderr
    args = watch_args(out.stdout)
    assert args == {"board": "demo", "tasks": ["t_00000001"]}
    assert "Card: t_00000001" in out.stdout
    commands = logged(calls)
    create = commands[0]
    assert create[:5] == ["kanban", "--board", "demo", "create", "A 'quoted' title; not shell"]
    assert create[create.index("--assignee") + 1] == "builder"
    assert create[create.index("--body") + 1] == body.read_text()
    assert create[create.index("--workspace") + 1] == f"worktree:{worktree}"
    # Pin at creation too, so an already-running dispatcher cannot race set-model.
    assert create[create.index("--model") + 1] == "gpt-5"
    assert create[create.index("--provider") + 1] == "openai"
    assert commands[1:] == [
        ["kanban", "--board", "demo", "set-model", "t_00000001", "gpt-5", "--provider", "openai"],
        ["kanban", "--board", "demo", "show", "t_00000001", "--json"],
    ]
    assert db.read_bytes() == before  # fake cards have a separate store


def test_dry_run_prints_quoted_commands_without_creating(filing):
    db, calls, _, body = filing
    before = db.read_bytes()
    out = file_card(filing, "--dry-run")
    assert out.returncode == 0, out.stderr
    tokens = shlex.split(out.stdout)
    starts = [i for i, token in enumerate(tokens) if token == "hermes"] + [len(tokens)]
    commands = [tokens[start:end] for start, end in zip(starts, starts[1:])]
    assert commands[0][:6] == ["hermes", "kanban", "--board", "demo", "create", "A 'quoted' title; not shell"]
    assert commands[0][commands[0].index("--body") + 1] == body.read_text()
    assert "set-model" in commands[1] and "show" in commands[2]
    assert "<card-id>" in commands[1] and "<card-id>" in commands[2]
    assert logged(calls) == [] and not db.with_suffix(".cards.json").exists()
    assert db.read_bytes() == before


@pytest.mark.parametrize("dry", [False, True])
@pytest.mark.parametrize("invalid", ["body", "class", "board"])
def test_invalid_inputs_create_nothing(filing, invalid, dry, tmp_path):
    db, calls, _, body = filing
    extra = ["--dry-run"] if dry else []
    if invalid == "body":
        extra += ["--body-file", str(tmp_path / "absent.txt")]
    elif invalid == "class":
        extra += ["--class", "unknown"]
    else:
        extra += ["--board", "missing"]
    out = file_card(filing, *extra)
    assert out.returncode != 0
    assert logged(calls) == [] and not db.with_suffix(".cards.json").exists()
    if invalid == "board":
        assert "hermes kanban boards create missing" in out.stderr
    elif invalid == "class":
        assert "Unknown class" in out.stderr
    else:
        assert "body file" in out.stderr.lower()
    assert body.exists()


@pytest.mark.parametrize("action", ["create", "set-model", "show"])
def test_cli_failure_never_prints_watch_success(filing, monkeypatch, action):
    _, calls, _, _ = filing
    monkeypatch.setenv("FAKE_FAIL", action)
    out = file_card(filing)
    assert out.returncode != 0 and "/kanban-watch " not in out.stdout
    assert action in out.stderr and "suppressed subprocess error" not in out.stderr
    if action != "create":
        assert "t_00000001" in out.stderr and "do not refile" in out.stderr.lower()
    assert logged(calls)[-1][3] == action


@pytest.mark.parametrize("fields", [{"model_override": "wrong"}, {"provider_override": "wrong"},
                                     {"assignee": "wrong"}, {"title": "wrong"}, {"body": "wrong"},
                                     {"id": "t_wrong"}])
def test_readback_must_match_created_card(filing, monkeypatch, fields):
    monkeypatch.setenv("FAKE_SHOW", json.dumps(fields))
    out = file_card(filing)
    assert out.returncode != 0 and "/kanban-watch " not in out.stdout
    assert "read-back" in out.stderr.lower() and "t_00000001" in out.stderr


def test_filing_reads_no_profile_or_credentials(filing, isolated_home, tmp_path):
    home, hermes = isolated_home
    profile = hermes / "profiles/builder"
    profile.mkdir(parents=True)
    for name in (".env", "auth.json", "config.yaml"):
        (profile / name).write_text("MUST_NOT_OPEN_THIS_FILE")
    runner = tmp_path / "audit.py"
    runner.write_text('''import runpy, sys
from pathlib import Path
script = sys.argv.pop(1)
sys.path.insert(0, str(Path(script).parent))
def guard(event, args):
    if event == "open" and isinstance(args[0], (str, bytes)):
        if Path(args[0]).name in (".env", "auth.json", "config.yaml"):
            raise RuntimeError("CREDENTIAL_OPEN_ATTEMPT")
sys.addaudithook(guard)
runpy.run_path(script, run_name="__main__")
''')
    _, _, routefile, body = filing
    out = subprocess.run([sys.executable, str(runner), str(PLUGIN / "scripts/file_card.py"),
                          "--board", "demo", "--class", "app-code", "--title", "Safe filing",
                          "--body-file", str(body), "--routes", str(routefile)],
                         capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    assert watch_args(out.stdout)["tasks"] == ["t_00000001"]
    assert "CREDENTIAL_OPEN_ATTEMPT" not in out.stdout + out.stderr
