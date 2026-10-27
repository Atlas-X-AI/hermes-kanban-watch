#!/usr/bin/env python3
"""Prepare one board watcher or start the Hermes watcher in a transient user unit."""
from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
from string import Template
import subprocess
import sys
import time
import uuid

from board_watch import atomic_json, board_path, positive, snapshot, validate_scope
from notify_session import resolve_pane
from watcher_tick import lease_lock, read_lease, release_lease

PLUGIN = Path(__file__).resolve().parents[1]


def reserve(config):
    with lease_lock(config) as path:
        current = read_lease(path)
        if current and not current.get("stopped") and current["until"] > time.time():
            raise ValueError("This board already has a watcher lease; do not start a second watcher")
        atomic_json(path, {"launch_id": config["launch_id"], "until": config["until"], "stopped": False})


def launch(args):
    validate_scope(args.board, args.cards)
    every, hours = positive(args.every), positive(args.max_hours)
    delivery = args.delivery or ("inbox" if args.session else "native")
    if args.discord_channel is not None:
        if not re.fullmatch(r"[0-9]{1,20}", args.discord_channel):
            raise ValueError("--discord-channel must be a numeric channel ID")
        if not args.session or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", args.session):
            raise ValueError("--discord-channel requires --session with the exact starting session name")
        if not args.prepare_only:
            raise ValueError("Direct Discord delivery is unsupported in a detached watcher; "
                             "omit --discord-channel and use parent relay")
        # Resolving the parent pane is not proof of the future helper's sender identity.
        resolve_pane(args.session)
    if delivery == "inbox":
        if not args.session:
            raise ValueError("--session is required for inbox delivery")
        resolve_pane(args.session)
    if not args.prepare_only and delivery == "native":
        raise ValueError("Native delivery requires --prepare-only, then launch the brief in ONE native agent")
    board_home = Path(args.home or os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser().resolve()
    snapshot(args.board, args.cards, home=board_home)  # Fail closed before starting a model or taking a lease.
    settings = json.loads((PLUGIN / "templates/watcher.json").read_text())
    profile = settings["profile"]
    if not re.fullmatch(r"kanban-watcher(?:-[A-Za-z0-9_-]+)?", profile):
        raise ValueError("Watcher profile must be a dedicated kanban-watcher profile")
    if not args.prepare_only:
        for executable in ("hermes", "systemd-run"):
            if not shutil.which(executable):
                raise ValueError(f"{executable} is not on PATH")
    launch_id = uuid.uuid4().hex
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "hermes-kanban-watch"
    directory = root / args.board / launch_id
    directory.mkdir(mode=0o700, parents=True)
    seconds = hours * 3600
    config = {"board": args.board, "cards": args.cards, "board_home": str(board_home), "every": every,
              "until": time.time() + seconds, "session": args.session, "delivery": delivery, "launch_id": launch_id,
              "discord_channel": args.discord_channel,
              "cursor": str(directory / "cursor.json"), "relay_state": str(directory / "relay.json"),
              "lease": str(root / "leases" / f"{args.board}.json")}
    config_path = directory / "config.json"
    atomic_json(config_path, config)
    command = shlex.join([sys.executable, str(PLUGIN / "scripts/watcher_tick.py"), "--config", str(config_path)])
    # One terminal call can wait for a full summary window, bounded by the watch deadline.
    tool_timeout = math.ceil(min(every, seconds)) + 60
    if tool_timeout > 7100:
        raise ValueError("--every is too long for a foreground tool call (maximum 7040 seconds)")
    delivery_instructions = (
        "The helper attempts direct Discord delivery only if the sender verifies its actual execution-context identity; "
        "preparing this brief or resolving the starting pane does not prove that route. "
        "The starting session does not relay reports. "
        "Only problem and FINAL notes (including UNKNOWN/time-limit stops) go to its inbox; never relay routine summaries."
        if args.discord_channel else
        "It queues the inbox row when delivery=inbox; the starting session relays each summary to the operator. "
        "When delivery=native it prints the summary for YOU to relay to your native parent.")
    brief = Template((PLUGIN / "templates/watcher-brief.md").read_text()).substitute(
        board=args.board, command=command, timeout=tool_timeout, every=every, until=config["until"],
        delivery_instructions=delivery_instructions)
    brief_path = directory / "brief.md"
    brief_path.write_text(brief, encoding="utf-8")
    reserve(config)
    unit = f"kanban-watcher-{args.board}"
    if not args.prepare_only:
        argv = ["systemd-run", "--user", "--unit", unit, "--collect",
                "--property", f"RuntimeMaxSec={math.ceil(seconds) + 90}",
                "--property", "KillMode=control-group", "--property", "CPUWeight=20",
                "--property", "Nice=10", "--working-directory", str(directory),
                "--setenv", "PATH=" + os.environ["PATH"],
                shutil.which("hermes"), "--profile", profile, "chat", "-Q", "--query-file", str(brief_path),
                "--max-turns", "1600", "--run-budget", str(math.ceil(seconds) + 60), "-t", "terminal"]
        # Never inherit the filing worker's task lifecycle into the watcher.
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN_")}
        try:
            result = subprocess.run(argv, capture_output=True, text=True, timeout=30, env=env)
            if result.returncode:
                raise ValueError("systemd-run refused the watcher unit: " + result.stderr.strip()[:240])
        except BaseException:
            release_lease(config)
            raise
    return {"unit": unit if not args.prepare_only else None, "profile": profile if not args.prepare_only else None,
            "config": str(config_path), "brief": str(brief_path), "command": command,
            "delivery": delivery, "native_parent_delivery": "UNKNOWN" if delivery == "native" else "NOT_APPLICABLE",
            "launch": "prepared" if args.prepare_only else "submitted"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", required=True)
    parser.add_argument("--session", help="Exact starting Claude tmux session; implies inbox delivery")
    parser.add_argument("--discord-channel", help="Conditional direct Discord for --prepare-only; requires --session; detached launches use parent relay")
    parser.add_argument("--cards", nargs="+", action="extend")
    parser.add_argument("--every", type=float, default=900)
    parser.add_argument("--max-hours", type=float, default=12)
    parser.add_argument("--home", type=Path, help="Hermes board data root, not a worker profile")
    parser.add_argument("--prepare-only", action="store_true", help="Prepare the brief for ONE native background agent; start no model")
    parser.add_argument("--delivery", choices=("inbox", "native"))
    args = parser.parse_args()
    try:
        print(json.dumps(launch(args), sort_keys=True))
    except (OSError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"start_watcher: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
