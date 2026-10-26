#!/usr/bin/env python3
"""One deterministic wait + delivery per watcher tool call; no board writes."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
from pathlib import Path
import os
import re
import sqlite3
import subprocess
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


def discord_draft(wake, session):
    """Counts and fixed prose only: board/card text must not reach a human channel."""
    if not isinstance(session, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", session):
        raise ValueError("Discord needs the exact starting session name")
    data, reason = wake["snapshot"], wake["reason"]
    if data.get("read_status") != "OK":
        lines = ["🟠 Kanban read status UNKNOWN; progress cannot be verified.",
                 "🟠 The starting session needs to investigate; completion is UNKNOWN."]
    else:
        counts = data["counts"]
        done = counts.get("done", 0) + counts.get("archived", 0)
        running = counts.get("running", 0)
        waiting = len(data["cards"]) - done - running
        problems = len(data["problems"])
        heading = ("🟢 FINAL: All watched work is finished." if reason == "final" else
                   "🟡 Watch time limit reached; completion UNKNOWN." if reason == "max_hours" else
                   "🔴 Kanban problems need attention." if problems else "🟣 Kanban progress update.")
        lines = [heading, f"🟢 Done: {done}", f"🟣 Running: {running}", f"🟡 Waiting: {waiting}",
                 f"{'🔴' if problems else '🟢'} Problems: {problems}",
                 "🟢 Rough time left: 0." if reason == "final" else "🟡 Rough time left: UNKNOWN.",
                 "🟠 The starting session needs to investigate." if problems else
                 "🟡 The starting session needs to check unfinished work." if reason == "max_hours" else
                 "🟢 Nothing needed." if reason == "final" else "🟣 Work continues; nothing needed."]
    return "\n".join([*lines, f"-# Source: {session}"])


def send_discord(config, wake):
    draft = Path(config["relay_state"]).with_name("discord-draft.md")
    draft.write_text(discord_draft(wake, config["session"]), encoding="utf-8")
    sender = os.environ.get("KANBAN_WATCH_DISCORD_SENDER") or str(
        Path.home() / ".claude/skills/messaging-hayden/bin/send-hayden.sh")
    result = subprocess.run([sender, str(draft), "--channel", config["discord_channel"]],
                            capture_output=True, text=True, timeout=45)
    # The wrapper lints, sends and reads back. Exit zero alone is insufficient.
    lines = result.stdout.splitlines()
    receipt = re.fullmatch(r"ok ([0-9]+) reactions=0/0", lines[-1]) if lines else None
    if result.returncode or not receipt:
        raise ValueError(f"Discord delivery UNKNOWN (sender exit {result.returncode}; no verified receipt)")
    return {"message_id": receipt[1], "channel": config["discord_channel"]}


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
    discord = bool(config.get("discord_channel"))
    key = f"kanban-watch:{config['launch_id']}:{relay['sequence']}"
    with owned_lease(config):
        if discord and not relay.get("discord_receipt"):
            try:
                relay["discord_receipt"] = send_discord(config, wake)
                # If the inbox fails next, an authorised retry must not repost.
                atomic_json(relay_path, relay)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                if config["delivery"] == "inbox":
                    notify(config["session"], "problem: Discord delivery UNKNOWN; stop and investigate before retrying.",
                           key + ":discord-unknown")
                raise ValueError("Discord delivery UNKNOWN; pending event retained") from exc
        if config["delivery"] == "inbox" and (not discord or reason != "summary"):
            receipt = notify(config["session"], text, key)
            relay["last_receipt"] = receipt
        # stdout is a local artifact, not proof of native parent receipt. With
        # Discord enabled, neither the watcher nor parent must relay it again.
        print(text)
    stopped = reason in {"final", "max_hours", "UNKNOWN"}
    relay.pop("pending")
    relay.pop("discord_receipt", None)
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
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"STOP delivery UNKNOWN: {short(exc)}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    raise SystemExit(main())
