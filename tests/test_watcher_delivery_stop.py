"""Interrupted and legacy pending sends must fail closed, with disposable state only."""
import json
from pathlib import Path

import pytest

from conftest import PLUGIN
from test_single_watcher import launch_env
from test_watcher_discord import discord_env, prepare


def test_interrupted_sender_leaves_durable_no_retry_marker(discord_env, monkeypatch):
    monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
    import watcher_tick

    receipt, config = prepare("--cards", "t_done")
    attempts = []

    def interrupted(*args):
        attempts.append(1)
        raise KeyboardInterrupt

    monkeypatch.setattr(watcher_tick, "send_discord", interrupted)
    with pytest.raises(KeyboardInterrupt):
        watcher_tick.tick(receipt["config"])
    state = Path(config["relay_state"])
    relay = json.loads(state.read_text())
    assert relay["discord_failure"] == {"kind": "attempt_incomplete", "exit_code": None}
    assert relay["pending"]["reason"] == "final"
    before = state.read_bytes()
    with pytest.raises(ValueError, match="reconciliation required"):
        watcher_tick.tick(receipt["config"])
    assert len(attempts) == 1 and state.read_bytes() == before


def test_legacy_pending_without_receipt_never_retries(discord_env, monkeypatch):
    monkeypatch.syspath_prepend(str(PLUGIN / "scripts"))
    import watcher_tick

    calls, inbox, _ = discord_env
    receipt, config = prepare("--cards", "t_done")
    wake = watcher_tick.wait(config["board"], config["cards"], config["every"],
                             config["cursor"], config["until"], config["board_home"])
    state = Path(config["relay_state"])
    state.write_text(json.dumps({"sequence": 1, "pending": wake}))
    before = state.read_bytes()
    with pytest.raises(ValueError, match="reconciliation required"):
        watcher_tick.tick(receipt["config"])
    assert state.read_bytes() == before and not calls.exists()
    import sqlite3
    with sqlite3.connect(inbox) as conn:
        assert conn.execute("select count(*) from messages").fetchone()[0] == 0
