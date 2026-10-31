#!/usr/bin/env python3
"""One deterministic wait + delivery per watcher tool call; no board writes."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import math
from pathlib import Path
import os
import re
import sqlite3
import subprocess
import sys
import time
from urllib import error as urlerror, parse as urlparse, request as urlrequest

from board_watch import atomic_json, short, summary, wait, waiting_count
from notify_session import notify


@contextmanager
def lease_lock(config):
    """Lock a stable inode; the JSON lease itself is atomically replaced."""
    path = Path(config["lease"])
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with Path(str(path) + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield path


def read_lease(path):
    """Read under lease_lock; safely collapse the old append bug, not unknown data."""
    text = path.read_text().strip() if path.exists() else ""
    if not text:
        return {}
    decoder, records = json.JSONDecoder(), []
    while text:
        record, end = decoder.raw_decode(text)
        if (not isinstance(record, dict) or not isinstance(record.get("launch_id"), str) or
                not record["launch_id"] or not isinstance(record.get("until"), (int, float)) or
                isinstance(record["until"], bool) or not math.isfinite(record["until"]) or
                not isinstance(record.get("stopped", False), bool)):
            raise ValueError("Malformed lease; ownership UNKNOWN")
        records.append(record)
        text = text[end:].lstrip()
    if len(records) > 1:
        if any(not old.get("stopped") and old["until"] > time.time() for old in records[:-1]):
            raise ValueError("Conflicting live lease records; ownership UNKNOWN")
        # Legacy a+ writes can only supersede an inactive lease. Preserve the
        # final owner's state: recovery is never permission to start a duplicate.
        atomic_json(path, records[-1])
    return records[-1]


@contextmanager
def owned_lease(config):
    with lease_lock(config) as path:
        lease = read_lease(path)
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
        waiting = waiting_count(data)  # A SUPERSEDED card is not waiting on anyone.
        # Progress and the final note only: problems are the starting session's to handle
        # (operator ruling, 2026-10-26: progress and results only).
        final = reason == "final"
        lines = ["🟢 FINAL: All watched work is finished." if final else "🟣 Kanban progress update.",
                 f"🟢 Done: {done}", f"🟣 Running: {running}", f"🟡 Waiting: {waiting}",
                 "🟢 Rough time left: 0." if final else "🟡 Rough time left: UNKNOWN.",
                 "🟢 Nothing needed." if final else "🟣 Work continues; nothing needed."]
    return "\n".join([*lines, f"-# Source: {session}"])


class DiscordDeliveryError(ValueError):
    """Only fixed classifications and exit status may leave the sender boundary."""

    def __init__(self, kind, exit_code=None):
        self.failure = {"kind": kind, "exit_code": exit_code}
        super().__init__(f"Discord delivery UNKNOWN ({kind}; sender exit {exit_code})")


class ChannelUnresolved(ValueError):
    """The session's channel could not be resolved; nothing was sent, so a later post may retry."""

    def __init__(self, kind):
        self.kind = kind
        super().__init__(kind)


SESSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def resolve_channel(config):
    """A fixed --discord-channel, or the session's live channel resolved now, for THIS post.

    A channel re-created after a relay crash gets a new id; resolving per post follows it.
    """
    if config.get("discord_channel"):
        return config["discord_channel"]
    session = config.get("discord_session")
    if not isinstance(session, str) or not SESSION_NAME.fullmatch(session):
        raise ChannelUnresolved("bad_session")
    base = os.environ.get("KANBAN_WATCH_DISCORD_RELAY") or "http://127.0.0.1:3100"
    parts = urlparse.urlsplit(base)
    if parts.scheme != "http" or parts.hostname not in LOOPBACK:
        raise ChannelUnresolved("relay_not_loopback")  # The session name never leaves the host.
    url = base.rstrip("/") + "/discord/sessions/" + urlparse.quote(session, safe="") + "/channel"
    opener = urlrequest.build_opener(urlrequest.ProxyHandler({}))  # Loopback only: never via a proxy.
    try:
        with opener.open(url, timeout=10) as response:
            body = json.loads(response.read(65536).decode("utf-8"))
    except urlerror.HTTPError as exc:
        raise ChannelUnresolved(f"http_{exc.code}") from None
    except (urlerror.URLError, OSError):
        raise ChannelUnresolved("relay_unreachable") from None
    except (UnicodeDecodeError, ValueError):
        raise ChannelUnresolved("bad_channel") from None
    channel = body.get("channelId") if isinstance(body, dict) else None
    channel = str(channel) if isinstance(channel, (str, int)) and not isinstance(channel, bool) else ""
    if not re.fullmatch(r"[0-9]{1,20}", channel):
        raise ChannelUnresolved("bad_channel")
    return channel


def send_discord(config, wake, channel):
    draft = Path(config["relay_state"]).with_name("discord-draft.md")
    draft.write_text(discord_draft(wake, config["session"]), encoding="utf-8")
    sender = os.environ.get("KANBAN_WATCH_DISCORD_SENDER") or str(
        Path.home() / ".claude/skills/messaging-hayden/bin/send-hayden.sh")
    # The normal sender checks identity in THIS process context, not the parent's
    # resolved pane. Never pass a service identity or borrow the starting session's.
    try:
        result = subprocess.run([sender, str(draft), "--channel", channel],
                                capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        raise DiscordDeliveryError("sender_timeout") from None
    except OSError:
        raise DiscordDeliveryError("sender_unavailable") from None
    # The wrapper lints, sends and reads back. Exit zero alone is insufficient.
    # Neither stdout/stderr nor a guessed cause (e.g. identity refusal) is retained.
    if result.returncode:
        raise DiscordDeliveryError("sender_exit", result.returncode)
    lines = result.stdout.splitlines()
    receipt = re.fullmatch(r"ok ([0-9]+) reactions=0/0", lines[-1]) if lines else None
    if not receipt:
        raise DiscordDeliveryError("receipt_missing", result.returncode)
    return {"message_id": receipt[1], "channel": channel}


def relay_tick(config):
    relay_path = Path(config["relay_state"])
    relay: dict = json.loads(relay_path.read_text()) if relay_path.exists() else {"sequence": 0}
    if relay.get("stopped"):
        print("STOP " + relay["stopped"])
        return 0
    discord = bool(config.get("discord_channel") or config.get("discord_session"))
    if ("discord_failure" in relay or
            (discord and relay.get("pending") and not relay.get("discord_receipt"))):
        # A pending event from an interrupted/older helper cannot prove no send
        # occurred, even when it predates structured failure records.
        raise ValueError("Discord delivery UNKNOWN; pending event retained; reconciliation required, no retry")
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
    key = f"kanban-watch:{config['launch_id']}:{relay['sequence']}"
    # Discord carries the 15-minute progress and the final note only. A problem between
    # summaries, a time-limit stop or a failed read reaches only the starting session.
    to_discord = discord and (reason == "final" or bool(wake.get("summary_due")))
    with owned_lease(config):
        channel = None
        if to_discord and not relay.get("discord_receipt"):
            try:
                channel = resolve_channel(config)
            except ChannelUnresolved as exc:
                # Known not sent: report it to the starting session (never drop it, never guess a
                # channel) and keep watching; the next post resolves again.
                note = (f"problem: Discord channel for session {config.get('discord_session')} unresolved "
                        f"({exc.kind}); this {reason} report was not posted to Discord. "
                        "The next post resolves the channel again.")
                if reason == "summary":
                    note += "\n" + text  # Summaries otherwise reach only Discord.
                relay["last_channel_failure"] = exc.kind
                if config["delivery"] == "inbox":
                    notify(config["session"], note, key + ":channel-unresolved")
                else:
                    print(note)
        if channel:
            # Fail closed across a crash during the external call too. A verified
            # receipt clears this marker; an incomplete attempt is never retried.
            relay["discord_failure"] = {"kind": "attempt_incomplete", "exit_code": None}
            atomic_json(relay_path, relay)
            try:
                relay["discord_receipt"] = send_discord(config, wake, channel)
                relay.pop("discord_failure")
                # If the inbox fails next, an authorised retry must not repost.
                atomic_json(relay_path, relay)
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                failure = exc if isinstance(exc, DiscordDeliveryError) else DiscordDeliveryError("sender_error")
                relay["discord_failure"] = failure.failure
                atomic_json(relay_path, relay)  # Persist before trying the parent inbox.
                if config["delivery"] == "inbox":
                    try:
                        notify(config["session"], f"problem: {failure}; stop for reconciliation, no retry.",
                               key + ":discord-unknown")
                    except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
                        raise ValueError(f"{failure}; pending event retained; parent notification UNKNOWN") from None
                raise ValueError(f"{failure}; pending event retained; reconciliation required, no retry") from None
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
    with lease_lock(config) as path:
        lease = read_lease(path)
        if lease.get("launch_id") == config["launch_id"]:
            atomic_json(path, {**lease, "stopped": True})


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
