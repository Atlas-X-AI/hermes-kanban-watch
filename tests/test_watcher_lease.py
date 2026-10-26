"""Lease relaunch, atomic replacement and legacy append recovery in temporary state."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from conftest import PLUGIN
from test_single_watcher import launch, launch_env


def prepared():
    result = launch("--prepare-only", "--cards", "t_done")
    assert result.returncode == 0, result.stderr
    receipt = json.loads(result.stdout)
    return receipt, json.loads(Path(receipt["config"]).read_text())


def tick(receipt):
    return subprocess.run([sys.executable, str(PLUGIN / "scripts/watcher_tick.py"),
                           "--config", receipt["config"]],
                          capture_output=True, text=True, timeout=5)


def test_stopped_watcher_can_relaunch_repeatedly(launch_env):
    prior_ids = set()
    for _ in range(3):
        receipt, config = prepared()
        lease = Path(config["lease"])
        current = json.loads(lease.read_text())
        assert current["launch_id"] == config["launch_id"]
        assert current["launch_id"] not in prior_ids and not current["stopped"]
        prior_ids.add(current["launch_id"])
        stopped = tick(receipt)
        assert stopped.returncode == 0 and "STOP final" in stopped.stdout
        assert json.loads(lease.read_text())["stopped"]


def test_expired_unstopped_lease_is_free(launch_env):
    _, first = prepared()
    lease = Path(first["lease"])
    lease.write_text(json.dumps({"launch_id": first["launch_id"], "until": 0, "stopped": False}))
    receipt, second = prepared()
    assert json.loads(lease.read_text())["launch_id"] == second["launch_id"]
    assert second["launch_id"] != first["launch_id"]
    assert tick(receipt).returncode == 0


def test_replacements_keep_a_stable_lock_inode(launch_env):
    receipt, config = prepared()
    lease = Path(config["lease"])
    lock = Path(str(lease) + ".lock")
    inode, lock_inode = lease.stat().st_ino, lock.stat().st_ino
    assert tick(receipt).returncode == 0
    assert lease.stat().st_ino != inode
    assert lock.stat().st_ino == lock_inode
    stopped_inode = lease.stat().st_ino
    prepared()
    assert lease.stat().st_ino != stopped_inode
    assert lock.stat().st_ino == lock_inode


def test_parallel_relaunch_still_reserves_only_one_watcher(launch_env):
    receipt, config = prepared()
    assert tick(receipt).returncode == 0
    command = ["bash", str(PLUGIN / "scripts/start_watcher.sh"), "--prepare-only",
               "--board", "demo", "--session", "desk", "--cards", "t_done"]
    processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(4)]
    outputs = [(process.communicate(timeout=10), process.returncode) for process in processes]
    assert sum(code == 0 for _, code in outputs) == 1
    assert all("already has a watcher lease" in err for (_, err), code in outputs if code)
    winner = next(json.loads(out) for (out, _), code in outputs if code == 0)
    winner_config = json.loads(Path(winner["config"]).read_text())
    assert json.loads(Path(config["lease"]).read_text())["launch_id"] == winner_config["launch_id"]


@pytest.mark.parametrize("stopped", [False, True])
def test_legacy_inactive_concatenation_is_replaced_not_appended(launch_env, stopped):
    receipt, config = prepared()
    assert tick(receipt).returncode == 0
    lease = Path(config["lease"])
    old = json.loads(lease.read_text())
    latest = {"launch_id": "legacy", "until": 0, "stopped": stopped}
    lease.write_text(json.dumps(old) + json.dumps(latest))
    _, replacement = prepared()
    current = json.loads(lease.read_text())
    assert current["launch_id"] == replacement["launch_id"] and not current["stopped"]


def test_legacy_live_tail_preserves_owner_and_can_resume_same_config(launch_env):
    receipt, config = prepared()
    lease = Path(config["lease"])
    current = json.loads(lease.read_text())
    old = {"launch_id": "old", "until": 0, "stopped": True}
    lease.write_text(json.dumps(old) + json.dumps(current))
    refused = launch("--prepare-only", "--cards", "t_done")
    assert refused.returncode != 0 and "already has a watcher lease" in refused.stderr
    assert json.loads(lease.read_text()) == current
    assert tick(receipt).returncode == 0
    assert json.loads(lease.read_text())["stopped"]


def test_legacy_live_prefix_is_not_discarded(launch_env):
    _, config = prepared()
    lease = Path(config["lease"])
    original = lease.read_text() + json.dumps({"launch_id": "stale", "until": 0, "stopped": True})
    lease.write_text(original)
    refused = launch("--prepare-only", "--cards", "t_done")
    assert refused.returncode != 0 and "live lease" in refused.stderr
    assert lease.read_text() == original


@pytest.mark.parametrize("trailing", ["not JSON", "{", "[]"])
def test_legacy_malformed_tail_fails_closed(launch_env, trailing):
    receipt, config = prepared()
    assert tick(receipt).returncode == 0
    lease = Path(config["lease"])
    original = lease.read_text() + trailing
    lease.write_text(original)
    assert launch("--prepare-only", "--cards", "t_done").returncode != 0
    assert lease.read_text() == original
