#!/usr/bin/env python3
"""Setup checks. --probe calls models; --fix/--suggest-routes opt in to local writes."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

# Checking an installation must not create import caches in that installation.
sys.dont_write_bytecode = True

from route import TEMPLATE, load_routes, mapping_text, resolve, routes_path, safe_identifier, scalar

SUBCOMMANDS = ("cards", "snapshot", "wait-start", "wait-any", "follow", "events", "active", "scout", "needs")
PROMPT = "Reply with the single word PONG and stop."


def model_config(path):
    """Select only model.provider/default, never deserialize other configuration.

    No recursive file discovery, includes, YAML constructors or credential opens.
    A config symlink is refused rather than following it to an unrelated file.
    Unsupported YAML shapes are UNKNOWN, not a guess.
    """
    if path.is_symlink():
        return {}
    result = {}
    in_model = False
    child_indent = None
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            indent = len(line) - len(line.lstrip(" "))
            if indent == 0:
                in_model = re.fullmatch(r"model:\s*(?:#.*)?\n?", line) is not None
                child_indent = None
                continue
            if not in_model:
                continue
            if child_indent is None:
                child_indent = indent
            if indent != child_indent:
                continue
            match = re.match(r"\s*(provider|default):\s*(.*?)\s*$", line)
            if match:
                key, value = match.groups()
                if key in result:
                    return {}  # duplicate selected key is ambiguous
                try:
                    result[key] = scalar(value)
                except ValueError:
                    result[key] = None
    return result


def run(command, timeout):
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout)


def exclusive_file(path, content, mode=0o600):
    """O_EXCL also refuses existing directories, files and dangling symlinks."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)


def lane(entry):
    return tuple(entry[key] for key in ("profile", "model", "provider"))


def suggested_routes(data, results, source):
    """Retain working routes, replacing unavailable lanes in probe order.

    Both halves of every emitted pair must have passed this run, and provider
    aliases never count as different model families. No unprobed lane is invented.
    """
    passing = [entry for entry, passed in results.items() if passed]
    providers = data["providers"]
    classes, verifiers, omitted = {}, {}, []
    for name, original in data["classes"].items():
        preferred = lane(original)
        candidates = ([preferred] if results.get(preferred) else []) + passing
        for worker in candidates:
            family = providers[worker[2]]
            configured = data["verifiers"].get(family)
            preferred_verifier = lane(configured) if configured else None
            choices = ([preferred_verifier] if preferred_verifier is not None and results.get(preferred_verifier) else []) + passing
            verifier = next((entry for entry in choices if providers[entry[2]] != family), None)
            if verifier is not None:
                keys = ("profile", "model", "provider")
                classes[name] = dict(zip(keys, worker))
                verifiers[family] = dict(zip(keys, verifier))
                break
        else:
            omitted.append(name)
    stamp = datetime.now(timezone.utc).isoformat()
    lines = [f"# PONG probe results at {stamp} UTC", f"# Source routes: {json.dumps(str(source))}",
             "# Order: profile, model, provider (lexicographic); exact successful PONG only."]
    for entry, passed in results.items():
        lines.append(f"# {'PASS' if passed else 'FAIL'} " + " ".join(entry))
    for name in omitted:
        lines.append(f"# Omitted {name}: no passing worker with a passing different-family verifier.")
    return "\n".join(lines) + "\n" + mapping_text({"providers": providers, "classes": classes, "verifiers": verifiers})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--board", default="default")
    parser.add_argument("--routes")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--suggest-routes", metavar="FILE", help="Write probe-backed routes to a new path; requires --probe")
    parser.add_argument("--fix", action="store_true")
    parser.add_argument("--timeout", type=float, default=30, help="Timeout per subprocess, seconds")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.board):
        parser.error("Invalid board slug")
    if not 0 < args.timeout <= 300:
        parser.error("--timeout must be between 0 and 300 seconds")
    suggestion = Path(args.suggest_routes).expanduser() if args.suggest_routes is not None else None
    if suggestion is not None:
        if not args.probe:
            parser.error("--suggest-routes requires --probe")
        if os.path.lexists(suggestion):
            parser.error("--suggest-routes FILE already exists; choose a new path")
    counts = Counter()

    def report(state, label, detail):
        counts[state] += 1
        print(f"{state} {label}: {detail}")

    home = Path.home()
    hermes_home = Path(os.environ.get("HERMES_HOME", home / ".hermes")).expanduser()
    board = (hermes_home / "kanban.db" if args.board == "default" else
             hermes_home / "kanban/boards" / args.board / "kanban.db")
    hermes = shutil.which("hermes")
    if hermes:
        try:
            version = run([hermes, "--version"], args.timeout)
            # Never relay arbitrary executable output, stderr or a key-looking value.
            match = re.search(r"\bv?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.]+)?\b", version.stdout)
            if version.returncode == 0 and match:
                report("PASS", "hermes", "version " + match.group()[:60])
            else:
                report("FAIL", "hermes", "--version failed or version not recognised")
        except (OSError, subprocess.SubprocessError, UnicodeError):
            report("FAIL", "hermes", "--version could not run within timeout")
    else:
        report("FAIL", "hermes", "not found on PATH")

    if args.fix:
        if not os.path.lexists(board) and hermes:
            command = ([hermes, "kanban", "--board", "default", "init"] if args.board == "default" else
                       [hermes, "kanban", "boards", "create", args.board])
            try:
                result = run(command, args.timeout)
                report("PASS" if result.returncode == 0 and board.is_file() else "FAIL", "fix board",
                       "initialised" if result.returncode == 0 and board.is_file() else "board creation failed")
            except (OSError, subprocess.SubprocessError, UnicodeError):
                report("FAIL", "fix board", "board creation could not run within timeout")
        elif os.path.lexists(board):
            report("PASS", "fix board", "existing board left unchanged")
        for label, path, content, mode in (
            ("routes", home / ".config/hermes-kanban-watch/routes.yaml", TEMPLATE.read_text(encoding="utf-8"), 0o600),
            ("shim", home / ".local/bin/hermes-kanban-live",
             "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " +
             shlex.quote(str(Path(__file__).resolve().with_name("kanban_live.py"))) + ' "$@"\n', 0o755),
        ):
            try:
                exclusive_file(path, content, mode)
                report("PASS", "fix " + label, "created")
            except FileExistsError:
                report("PASS", "fix " + label, "existing file left unchanged")
            except OSError:
                report("FAIL", "fix " + label, "could not create file")
    report("PASS" if board.is_file() else "FAIL", "board", "exists" if board.is_file() else "database missing")

    profiles = set()
    directory = hermes_home / "profiles"
    try:
        entries = sorted(directory.iterdir()) if directory.is_dir() else []
        for profile in entries:
            if not profile.is_dir():
                continue
            if not safe_identifier(profile.name, profile=True):
                report("UNKNOWN", "profile", "unsafe name omitted")
                continue
            if profile.is_symlink():
                report("UNKNOWN", "profile " + profile.name, "symlink not followed")
                continue
            profiles.add(profile.name)
            try:
                selected = model_config(profile / "config.yaml")
                provider, model = selected.get("provider"), selected.get("default")
                if safe_identifier(provider) and safe_identifier(model):
                    report("PASS", "profile " + profile.name, f"provider={provider} model={model}")
                else:
                    report("UNKNOWN", "profile " + profile.name, "model.provider/default unavailable or unsafe; values omitted")
            except (OSError, UnicodeError, ValueError):
                report("UNKNOWN", "profile " + profile.name, "config not readable")
        if not entries:
            report("UNKNOWN", "profiles", "no profile directories found")
    except OSError:
        report("UNKNOWN", "profiles", "directory not readable")

    data = None
    try:
        data = load_routes(args.routes)
        for name in data["classes"]:
            try:
                resolved = resolve(data, name)
                missing = any(entry["profile"] not in profiles for entry in (resolved, resolved["verifier"]))
                report("FAIL" if missing else "PASS", "class " + name,
                       "worker or verifier profile missing" if missing else "profiles exist; verifier is a different family")
            except ValueError:
                report("FAIL", "class " + name, "no valid different-family verifier")
    except (OSError, UnicodeError, ValueError):
        report("FAIL", "routes", "cannot read or validate routes")

    reader = shutil.which("hermes-kanban-live")
    shim = home / ".local/bin/hermes-kanban-live"
    candidate = reader or (str(shim) if args.fix and os.access(shim, os.X_OK) else None)
    if candidate:
        manifest = Path(__file__).resolve().parents[1] / ".claude-plugin/plugin.json"
        identity = "hermes-kanban-watch " + json.loads(manifest.read_text(encoding="utf-8"))["version"]
        try:
            result = run([candidate, "--version"], args.timeout)
            identified = result.returncode == 0 and result.stdout == identity + "\n"
        except (OSError, subprocess.SubprocessError, UnicodeError):
            identified = False
        if not identified:
            report("FAIL", "hermes-kanban-live",
                   f"{candidate} did not identify as {identity!r} with --version; output omitted. "
                   "Move the foreign or stale command out of PATH, then rerun --fix; "
                   "existing files are never overwritten. Alternatively set args.live_cmd to this plugin's reader.")
        elif reader:
            report("PASS", "hermes-kanban-live", f"{candidate} identifies as {identity}")
        else:
            report("UNKNOWN", "hermes-kanban-live", "shim identified, but add ~/.local/bin to PATH and rerun")
    else:
        report("FAIL", "hermes-kanban-live", "not found on PATH; use --fix or args.live_cmd")

    if args.probe and data:
        entries = {lane(entry)
                   for section in ("classes", "verifiers") for entry in data[section].values()}
        results = {}
        for profile, model, provider in sorted(entries):
            entry = (profile, model, provider)
            results[entry] = False
            label = f"probe {profile} {model} {provider}"
            if not hermes or profile not in profiles:
                report("FAIL", label, "hermes or profile unavailable")
                continue
            try:
                result = run([hermes, "-p", profile, "chat", "-Q", "-q", PROMPT,
                              "-m", model, "--provider", provider], args.timeout)
                passed = result.returncode == 0 and result.stdout.strip() == "PONG"
                results[entry] = passed
                report("PASS" if passed else "FAIL", label, "PONG" if passed else "not exactly PONG; output omitted")
            except (OSError, subprocess.SubprocessError, UnicodeError):
                report("FAIL", label, "probe failed or timed out; output omitted")
        if suggestion is not None:
            try:
                exclusive_file(suggestion, suggested_routes(data, results, routes_path(args.routes)))
                report("PASS", "suggest routes", "created from this run's PONG results; source routes unchanged")
            except FileExistsError:
                report("FAIL", "suggest routes", "FILE already exists; left unchanged")
            except (OSError, UnicodeError, ValueError):
                report("FAIL", "suggest routes", "could not write FILE")

    print("Watcher permission allowlist (read-only commands):")
    for command in SUBCOMMANDS:
        print(f"Bash(hermes-kanban-live {command}:*)")
    print(f"Summary: {counts['PASS']} PASS, {counts['FAIL']} FAIL, {counts['UNKNOWN']} UNKNOWN")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
