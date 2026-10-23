#!/usr/bin/env python3
"""Resolve a worker and a different-family verifier. Never dispatch or read credentials.

The routes format is a mapping-only YAML subset (two-space indentation, string
scalars, optional quotes and comments), or JSON. No YAML tags, anchors or aliases.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import sys

TEMPLATE = Path(__file__).resolve().parents[1] / "templates/routes.yaml"
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/@:+-]{0,127}\Z")
PROFILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
KEY_LIKE = re.compile(r"(?:^|[^a-z])(?:sk[-_]|pk[-_]|gh[pousr]_|github_pat_|AIza|AKIA|Bearer\s)|eyJ[A-Za-z0-9_-]+\.", re.I)


def safe_identifier(value, *, profile=False):
    pattern = PROFILE if profile else IDENTIFIER
    return (isinstance(value, str) and pattern.fullmatch(value) is not None
            and KEY_LIKE.search(value) is None
            and re.search(r"[A-Za-z0-9_-]{40,}", value) is None)


def scalar(raw):
    """Only strings, without YAML type coercion or executable constructors."""
    lexer = shlex.shlex(raw, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = "#"
    words = list(lexer)
    if len(words) != 1 or not safe_identifier(words[0]):
        raise ValueError("Expected a short identifier string in routes")
    return words[0]


def read_mapping(path):
    text = Path(path).read_text(encoding="utf-8")
    if text.lstrip().startswith("{"):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate routes key")
                result[key] = value
            return result
        data = json.loads(text, object_pairs_hook=unique)
        if not isinstance(data, dict):
            raise ValueError("Routes must be a mapping")
        return data
    root = {}
    stack = [(-2, root)]
    previous_indent = -2
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" in line:
            raise ValueError(f"Tabs are not supported in routes at line {number}")
        indent = len(line) - len(line.lstrip(" "))
        if indent % 2 or indent > previous_indent + 2:
            raise ValueError(f"Invalid routes indentation at line {number}")
        while stack and stack[-1][0] >= indent:
            stack.pop()
        if not stack or stack[-1][0] != indent - 2:
            raise ValueError(f"Invalid routes nesting at line {number}")
        match = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9._/@:+-]{0,127}):(?:\s+(.*))?", line.strip())
        if not match:
            raise ValueError(f"Expected routes mapping at line {number}")
        key, raw = match.groups()
        parent = stack[-1][1]
        if key in parent:
            raise ValueError(f"Duplicate routes key at line {number}")
        if raw is None or not raw.strip() or raw.lstrip().startswith("#"):
            parent[key] = {}
            stack.append((indent, parent[key]))
        else:
            parent[key] = scalar(raw)
        previous_indent = indent
    return root


def mapping_text(mapping, indent=0):
    """Serialize the same identifier-only mapping subset that read_mapping accepts."""
    lines = []
    for key, value in mapping.items():
        if not safe_identifier(key):
            raise ValueError("Invalid routes mapping key")
        prefix = " " * indent + key + ":"
        if isinstance(value, dict):
            lines.append(prefix + "\n" + mapping_text(value, indent + 2))
        elif safe_identifier(value):
            lines.append(prefix + " " + json.dumps(value) + "\n")
        else:
            raise ValueError("Invalid routes mapping value")
    return "".join(lines)


def routes_path(explicit=None):
    if explicit is not None:
        return Path(explicit).expanduser()
    if "KANBAN_WATCH_ROUTES" in os.environ:
        value = os.environ["KANBAN_WATCH_ROUTES"]
        if not value:
            raise ValueError("KANBAN_WATCH_ROUTES is empty")
        return Path(value).expanduser()
    local = Path.home() / ".config/hermes-kanban-watch/routes.yaml"
    return local if local.exists() else TEMPLATE


def load_routes(explicit=None):
    data = read_mapping(routes_path(explicit))
    for field in ("providers", "classes", "verifiers"):
        if not isinstance(data.get(field), dict) or not data[field]:
            raise ValueError(f"Routes need a non-empty {field} mapping")
    for provider, family in data["providers"].items():
        if not safe_identifier(provider) or not safe_identifier(family):
            raise ValueError("Invalid provider/family identifier")
    for section in ("classes", "verifiers"):
        for name, entry in data[section].items():
            if not safe_identifier(name) or not isinstance(entry, dict):
                raise ValueError("Invalid route entry")
            for field in ("profile", "model", "provider"):
                if not safe_identifier(entry.get(field), profile=field == "profile"):
                    raise ValueError(f"Invalid or missing route {field}")
            if entry["provider"] not in data["providers"]:
                raise ValueError("Route provider has no family mapping")
    return data


def resolve(data, class_name, worker_provider=None, worker_model=None):
    if class_name not in data["classes"]:
        raise ValueError("Unknown class")
    worker = {key: data["classes"][class_name][key] for key in ("profile", "model", "provider")}
    if worker_provider is not None:
        worker["provider"] = worker_provider
    if worker_model is not None:
        worker["model"] = worker_model
    if not all(safe_identifier(worker[key], profile=key == "profile") for key in worker):
        raise ValueError("Invalid worker identifier")
    family = data["providers"].get(worker["provider"])
    if family is None:
        raise ValueError("Worker provider has no family mapping")
    verifier = data["verifiers"].get(family)
    if verifier is None:
        raise ValueError("Worker family has no verifier")
    if data["providers"][verifier["provider"]] == family:
        raise ValueError("Verifier is from the same family as the worker")
    return {"class": class_name, **worker,
            "verifier": {key: verifier[key] for key in ("profile", "model", "provider")},
            "reason": f"Worker family {family}; verifier family {data['providers'][verifier['provider']]}"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--routes")
    parser.add_argument("--class", dest="class_name", required=True)
    parser.add_argument("--worker-provider")
    parser.add_argument("--worker-model")
    args = parser.parse_args(argv)
    try:
        result = resolve(load_routes(args.routes), args.class_name, args.worker_provider, args.worker_model)
    except (OSError, UnicodeError, ValueError):
        # Do not echo input or parser excerpts that could include an accidentally pasted key.
        # These controlled errors describe the validation failure, not any file contents.
        exc = sys.exc_info()[1]
        message = str(exc) if type(exc) is ValueError else "Cannot read routes file"
        if isinstance(exc, json.JSONDecodeError):
            message = "Invalid routes JSON"
        print(f"route: {message}", file=sys.stderr)
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
