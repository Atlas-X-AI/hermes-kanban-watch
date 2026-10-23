"""Exercise the installed plugin, not the development scripts; stdlib only."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers import create_board, fake_hermes


PLUGIN_ID = "hermes-kanban-watch@hermes-kanban-watch"
CANARY = "INSTALL_SMOKE_SYNTHETIC_ENV_CANARY_72931"
CARD_ID = "t_install_smoke"


def run(command, expected=0, doctor=False, cwd=None):
    result = subprocess.run([str(part) for part in command], capture_output=True,
                            text=True, timeout=120, cwd=cwd)
    output = result.stdout + result.stderr
    # Check both streams before printing anything, even when the doctor fails.
    if doctor and CANARY in output:
        raise AssertionError("CANARY ASSERTION FAILED: doctor leaked synthetic .env")
    print(output, end="" if output.endswith("\n") else "\n", flush=True)
    assert result.returncode == expected, f"{command}: exit {result.returncode}, expected {expected}"
    return result.stdout


def tree_hashes(root):
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def main():
    repo = Path(sys.argv[1]).resolve()
    home = Path.home()
    hermes = Path(os.environ["HERMES_HOME"])
    temporary = Path(os.environ["TMPDIR"])
    # Refuse accidental direct invocation against a real user home.
    assert home.parent == hermes.parent == temporary
    assert home.name == "user" and hermes.name == "hermes"
    run(["claude", "plugin", "validate", repo])
    run(["claude", "plugin", "validate", repo / "plugin"])
    run(["claude", "plugin", "validate", "."], cwd=repo)
    run(["claude", "plugin", "marketplace", "add", repo])
    run(["claude", "plugin", "install", PLUGIN_ID])
    listing = json.loads(run(["claude", "plugin", "list", "--json"]))
    entries = [item for item in listing if item["id"] == PLUGIN_ID]
    assert len(entries) == 1 and entries[0]["enabled"] is True, "plugin not enabled"
    # Claude owns this registry and its versioned installPath. Never construct
    # a cache path or fall back to running repo/plugin/scripts.
    registry = json.loads((home / ".claude/plugins/installed_plugins.json").read_text())
    installs = registry["plugins"][PLUGIN_ID]
    assert len(installs) == 1, "ambiguous installed plugin"
    installed = Path(installs[0]["installPath"]).resolve()
    assert installed.is_relative_to(home) and installed != repo / "plugin"
    doctor = installed / "scripts/doctor.py"
    assert doctor.is_file() and (installed / "scripts/kanban_live.py").is_file()

    bindir = temporary / "bin"
    fake_hermes(bindir)
    os.environ["FAKE_CALLS"] = str(temporary / "hermes-calls.jsonl")
    os.environ["PATH"] = os.pathsep.join((str(bindir), str(home / ".local/bin"), os.defpath))
    for name in ("builder", "reviewer"):
        profile = hermes / "profiles" / name
        profile.mkdir(parents=True)
        (profile / "config.yaml").write_text("model:\n  provider: openai\n  default: gpt-5\n")
        (profile / ".env").write_text(CANARY)
    routes = temporary / "routes.json"
    routes.write_text(json.dumps({
        "providers": {"openai": "gpt", "anthropic": "claude"},
        "classes": {"app-code": {"profile": "builder", "model": "gpt-5", "provider": "openai"}},
        "verifiers": {
            "gpt": {"profile": "reviewer", "model": "claude-sonnet-4-5", "provider": "anthropic"},
            "claude": {"profile": "builder", "model": "gpt-5", "provider": "openai"},
        },
    }))
    if os.environ["INSTALL_SMOKE_CANARY_CONTROL"] == "1":
        # Mutate only a disposable copy, not the real install or source tree.
        control = temporary / "leaking-scripts"
        shutil.copytree(installed / "scripts", control)
        doctor = control / "doctor.py"
        source = doctor.read_text()
        marker = 'if __name__ == "__main__":'
        assert source.count(marker) == 1
        doctor.write_text(source.replace(marker, marker + '\n    print((Path(os.environ["HERMES_HOME"]) / "profiles/builder/.env").read_text())'))
        print("Known-red control: using a doctor copy that prints synthetic .env", flush=True)

    before = tree_hashes(home), tree_hashes(hermes)
    readonly = run([sys.executable, doctor, "--routes", routes], expected=1, doctor=True)
    assert "PASS hermes:" in readonly and "PASS profile builder:" in readonly
    assert "FAIL board:" in readonly and "FAIL hermes-kanban-live:" in readonly
    assert before == (tree_hashes(home), tree_hashes(hermes)), "read-only doctor wrote user data"
    print("Read-only doctor: expected missing board/shim; no writes or canary output", flush=True)
    fixed = run([sys.executable, doctor, "--routes", routes, "--fix"], doctor=True)
    assert "PASS fix board:" in fixed and "PASS fix shim:" in fixed
    shim = home / ".local/bin/hermes-kanban-live"
    assert shim.is_file() and os.access(shim, os.X_OK)
    identity = "hermes-kanban-watch " + json.loads((installed / ".claude-plugin/plugin.json").read_text())["version"]
    assert run([shim, "--version"]) == identity + "\n"
    assert "PASS hermes-kanban-live:" in fixed and identity in fixed
    # The fake init intentionally creates a placeholder, just as in pytest.
    # Replace only that file with a real one-card board using the shared schema.
    db = hermes / "kanban.db"
    db.unlink()
    create_board(db)
    now = int(time.time())
    with sqlite3.connect(db) as con:
        con.execute("insert into tasks values(?,?,?,?,?,?,?,?,?,?)",
                    (CARD_ID, "Install smoke card", "builder", "ready", now,
                     None, None, None, None, None))
    before = db.read_bytes()
    cards = json.loads(run([shim, "cards", "--board", "default"]))
    assert cards["read_status"] == "OK" and cards["count"] == 1
    assert [card["id"] for card in cards["cards"]] == [CARD_ID]
    assert db.read_bytes() == before, "cards mutated the board"
    print("Installed hermes-kanban-live: read_status OK; one card t_install_smoke; board unchanged", flush=True)


if __name__ == "__main__":
    main()
