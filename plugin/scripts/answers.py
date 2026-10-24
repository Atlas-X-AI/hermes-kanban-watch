#!/usr/bin/env python3
"""Suggest standing answers as JSON lines. Never execute the printed commands.

Rules are a JSON list or a dependency-free YAML subset: two-space block rule
mappings, block/inline match lists and single-line string scalars. Single quotes
use YAML's doubled apostrophes; double quotes use JSON escapes. Comments are
allowed. YAML tags, anchors, aliases and multiline scalars are not supported.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shlex
import sys


def unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate answers field")
        result[key] = value
    return result


def yaml_string(raw):
    raw = raw.strip()
    if raw.startswith("'"):
        match = re.fullmatch(r"'((?:[^']|'')*)'\s*(?:#.*)?", raw)
        if match is None:
            raise ValueError("Invalid single-quoted YAML string")
        return match[1].replace("''", "'")
    if raw.startswith('"'):
        try:
            value, end = json.JSONDecoder().raw_decode(raw)
        except ValueError:
            raise ValueError("Invalid double-quoted YAML string") from None
        rest = raw[end:].strip()
        if not isinstance(value, str) or (rest and not rest.startswith("#")):
            raise ValueError("Invalid quoted YAML string suffix")
        return value
    value = re.split(r"\s+#", raw, maxsplit=1)[0].rstrip()
    if not value or value.startswith(("#", "!", "&", "*", "[", "]", "{", "}", "|", ">", "@", "`")):
        raise ValueError("Expected a YAML string (quote special characters)")
    return value


def yaml_list(raw):
    """Split only outside quoted regexes, preserving backslashes and commas."""
    parts = []
    start = 1
    quote = None
    i = 1
    while i < len(raw):
        char = raw[i]
        if quote == '"' and char == "\\":
            i += 2
            continue
        if quote == "'" and raw[i:i + 2] == "''":
            i += 2
            continue
        if char == quote:
            quote = None
        elif quote is None:
            if char in "'\"" and not raw[start:i].strip():
                quote = char
            elif char == ",":
                parts.append(yaml_string(raw[start:i]))
                start = i + 1
            elif char == "]":
                suffix = raw[i + 1:].strip()
                if suffix and not suffix.startswith("#"):
                    raise ValueError("Invalid inline match list suffix")
                if raw[start:i].strip():
                    parts.append(yaml_string(raw[start:i]))
                return parts
        i += 1
    raise ValueError("Unterminated inline match list")


def yaml_rules(text):
    rules = []
    current = None
    in_match = False
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        content = line[indent:]
        if "\t" in line[:indent + 1]:
            raise ValueError(f"Tabs are not YAML indentation (line {number})")
        if indent == 0 and content.startswith("- "):
            current = {}
            rules.append(current)
            content = content[2:]
        elif indent in (2, 4) and content.startswith("- ") and current is not None and in_match:
            current["match"].append(yaml_string(content[2:]))
            continue
        elif indent != 2 or current is None:
            raise ValueError(f"Expected a YAML rule list (line {number})")
        field = re.fullmatch(r"([a-z_]+):(?:\s+(.*))?", content)
        if field is None:
            raise ValueError(f"Expected a YAML rule field (line {number})")
        key, raw = field.groups()
        if key in current:
            raise ValueError(f"Duplicate answers field (line {number})")
        in_match = key == "match"
        raw = (raw or "").strip()
        if in_match:
            if not raw or raw.startswith("#"):
                current[key] = []
            elif raw.startswith("["):
                current[key] = yaml_list(raw)
                in_match = False
            else:
                raise ValueError(f"Expected a match list (line {number})")
        else:
            current[key] = yaml_string(raw)
    if not rules:
        raise ValueError("Answers must be a list (use [] for an empty rule list)")
    return rules


def load_rules(path):
    text = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(text, object_pairs_hook=unique_fields)
    except json.JSONDecodeError:
        # A JSON-looking document must not be reinterpreted as a YAML rule list.
        if text.lstrip().startswith(("[", "{")):
            raise ValueError("Invalid answers JSON") from None
        data = yaml_rules(text)
    if not isinstance(data, list):
        raise ValueError("Answers must be a list")
    compiled = []
    seen = set()
    for number, entry in enumerate(data, 1):
        if not isinstance(entry, dict):
            raise ValueError(f"Rule {number} must be a mapping")
        rule_id = entry.get("id")
        if not isinstance(rule_id, str) or not rule_id.strip() or rule_id == "AMBIGUOUS":
            raise ValueError(f"Rule {number} needs a non-empty, non-reserved id")
        if rule_id in seen:
            raise ValueError("Rule ids must be unique")
        seen.add(rule_id)
        patterns = entry.get("match")
        if not isinstance(patterns, list) or not patterns:
            raise ValueError(f"Rule {number} needs a non-empty match list")
        regexes = []
        for pattern in patterns:
            if not isinstance(pattern, str):
                raise ValueError(f"Rule {number} needs string regexes")
            try:
                regexes.append(re.compile(pattern, re.IGNORECASE))
            except re.error:
                raise ValueError(f"Invalid regex in rule {number}") from None
        answer = entry.get("answer")
        if not isinstance(answer, str) or not answer.strip() or "\x00" in answer:
            raise ValueError(f"Rule {number} needs a non-empty answer without NUL")
        if "note" in entry and not isinstance(entry["note"], str):
            raise ValueError(f"Rule {number} note must be a string")
        compiled.append((rule_id, regexes, answer))
    return compiled


def command_arg(value):
    return (isinstance(value, str) and bool(value.strip()) and not value.startswith("-")
            and not any(char in value for char in "\x00\r\n"))


def double_quote(text):
    # Double quotes match the documented command shape, but must not expand shell
    # variables, command substitutions, backticks, quotes or backslashes on paste.
    for char in ("\\", '"', "$", "`"):
        text = text.replace(char, "\\" + char)
    return '"' + text + '"'


def suggestions(data, rules, board, author):
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("Needs JSON must be an object with an items list")
    if not command_arg(board) or not command_arg(author):
        raise ValueError("Board and author must be non-empty single-line command arguments")
    results = []
    for item in data["items"]:
        if (not isinstance(item, dict) or not command_arg(item.get("task"))
                or not isinstance(item.get("text"), str)):
            raise ValueError("Every needs item must have a task and string text")
        matches = [(rule_id, answer) for rule_id, patterns, answer in rules
                   if all(pattern.search(item["text"]) for pattern in patterns)]
        result = {"task": item["task"], "rule_id": None, "answer": None, "comment_cmd": None}
        if len(matches) == 1:
            result["rule_id"], result["answer"] = matches[0]
            prefix = shlex.join(["hermes", "kanban", "--board", board, "comment", item["task"]])
            result["comment_cmd"] = f"{prefix} {double_quote(result['answer'])} --author {shlex.quote(author)}"
        elif len(matches) > 1:
            result["rule_id"] = "AMBIGUOUS"
            result["rule_ids"] = [rule_id for rule_id, _ in matches]
        results.append(result)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="Validate rules without posting")
    check.add_argument("--answers", required=True)
    suggest = commands.add_parser("suggest", help="Print suggestions, never post them")
    suggest.add_argument("--answers", required=True)
    suggest.add_argument("--needs-json", default="-", metavar="FILE|-", help="Needs JSON (default: stdin)")
    suggest.add_argument("--board", default="default", help="Board for printed commands (default: default)")
    suggest.add_argument("--author", required=True, help="Author for printed comments, never inferred")
    args = parser.parse_args(argv)
    try:
        rules = load_rules(args.answers)
        if args.command == "check":
            print(json.dumps({"valid": True, "rules": len(rules)}))
            return 0
        if args.needs_json == "-":
            data = json.load(sys.stdin)
        else:
            data = json.loads(Path(args.needs_json).read_text(encoding="utf-8"))
        results = suggestions(data, rules, args.board, args.author)
    except (OSError, UnicodeError, ValueError) as exc:
        if isinstance(exc, json.JSONDecodeError):
            message = "Invalid needs JSON"
        elif type(exc) is ValueError:
            message = str(exc)
        else:
            message = "Cannot read input file"
        print(f"answers: {message}", file=sys.stderr)
        return 1
    for result in results:
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
