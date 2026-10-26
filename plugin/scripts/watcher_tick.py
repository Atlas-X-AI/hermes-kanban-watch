#!/usr/bin/env python3
"""One deterministic wait + delivery per watcher tool call; no board writes."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import sqlite3
import sys

from board_watch import atomic_json, short, summary, wait
from notify_session import notify


@contextmanager
def owned_lease(config):
    with Path(config["lease"]).open() as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        lease = json.load(f)
        if lease.get("launch_id") != config["launch_id"] or lease.get("stopped"):
            raise ValueError("Watch lease superseded or stopped; nothing delivered")
        yield


def tick(config_path):
    config = json.loads(Path(config_path).read_text())
    with Path(config["relay_state"] + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return relay_tick(config)


def relay_tick(config):
    relay_path = Path(config["relay_state"])
    relay: dict = json.loads(relay_path.read_text()) if relay_path.exists() else {"sequence": 0}
    if relay.get("stopped"):
        print("STOP " + relay["stopped"])
        return 0
    with owned_lease(config):
        pass
    if not relay.get("pending"):
        try:
            wake = wait(config["board"], config["cards"], config["every"], config["cursor"],
                        config["until"], config["board_home"])
        except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as exc:
            wake = {"reason": "UNKNOWN", "snapshot": {"read_status": "UNKNOWN", "error": short(exc)}}
        relay["sequence"] += 1
        relay["pending"] = wake
        atomic_json(relay_path, relay)
    wake = relay["pending"]
    reason = wake["reason"]
    text = summary(wake["snapshot"])
    prefix = "FINAL: " if reason == "final" else f"{reason}: "
    text = prefix + text
    # Stable pending event + notification key make a retry after an interrupted
    # inbox write idempotent. Never advance the delivery cursor on a failed send.
    with owned_lease(config):
        if config["delivery"] == "inbox":
            receipt = notify(config["session"], text,
                             f"kanban-watch:{config['launch_id']}:{relay['sequence']}")
            relay["last_receipt"] = receipt
        # stdout is the real summary artifact for both routes, not proof of native
        # parent receipt. The native watcher must relay it through its own harness.
        print(text)
    stopped = reason in {"final", "max_hours", "UNKNOWN"}
    relay.pop("pending")
    if stopped:
        relay["stopped"] = reason
    atomic_json(relay_path, relay)
    if stopped:
        release_lease(config)
    print(("STOP " if stopped else "CONTINUE ") + reason)
    return 5 if reason == "UNKNOWN" else 0


def release_lease(config):
    """Release only this launch's local lease, never a card or another watcher."""
    import fcntl

    with Path(config["lease"]).open("r+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        lease = json.load(f)
        if lease.get("launch_id") == config["launch_id"]:
            f.seek(0)
            json.dump({**lease, "stopped": True}, f)
            f.truncate()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    try:
        return tick(args.config)
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as exc:
        print(f"STOP delivery UNKNOWN: {short(exc)}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
