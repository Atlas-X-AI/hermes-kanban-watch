#!/usr/bin/env python3
"""PostToolUse(Bash) reminder only. Never blocks or dispatches a card."""
import json
import os
from pathlib import Path
import re
import shlex
import sys

CARD = re.compile(r"\bt_[0-9a-f]{8}\b")
CARD_ID = re.compile(r"t_[0-9a-f]{8}")
CREATED = re.compile(r"^Created\s+(t_[0-9a-f]{8})\b", re.MULTILINE)
BARE_ID = re.compile(r"^t_[0-9a-f]{8}$", re.MULTILINE)
SWARM_LINE = re.compile(
    r"^(?:Swarm root|Workers|Verifier|Synthesizer):[ \t]*(t_[0-9a-f]{8}(?:,[ \t]*t_[0-9a-f]{8})*)",
    re.MULTILINE,
)
JQ_NEXT = re.compile(r"^\s*(?:[A-Za-z_]\w*=\S*\s+)*(?:\S*/)?jq(?:\s|$)")
ASSIGN = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.DOTALL)
HEREDOC_WORD = re.compile(r"""(?:'[^']*'|"[^"]*"|\\.|[^\s|;&<>])+""")
PIN_VAR = "HERMES_KANBAN_BOARD"
# Words that only introduce a command (a shell keyword never runs anything itself).
LEADING_KEYWORDS = {"do", "then", "else", "elif", "if", "while", "until", "!", "{", "time"}
UNFOLLOWABLE = re.compile(r"HERMES_(?:KANBAN_DB|KANBAN_HOME|HOME)\b")
SUBST = "$(subst)"
UNKNOWN = "unknown board"


class Segment:
    __slots__ = ("text", "op", "piped")

    def __init__(self, text: str, op: str) -> None:
        self.text = text
        self.op = op
        self.piped = ""  # text of the segment this one pipes into


def _match_paren(command: str, i: int) -> int:
    """Index of the ')' closing a '$(' whose body starts at i, or -1."""
    depth, quote = 1, None
    while i < len(command):
        char = command[i]
        if char == "\\" and quote != "'":
            i += 2
            continue
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif command.startswith("$(", i):
            depth += 1
            i += 1
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _match_backtick(command: str, i: int) -> int:
    while i < len(command):
        if command[i] == "\\":
            i += 2
            continue
        if command[i] == "`":
            return i
        i += 1
    return -1


def command_segments(command: str) -> list[Segment]:
    """Split unquoted shell operators, never treating heredoc data as commands.

    This is deliberately not a shell interpreter. The body of a `$(...)` or
    backtick substitution becomes segments of its own; the outer segment keeps a
    placeholder. Anything it cannot follow (unbalanced quotes, an unterminated
    heredoc) returns [] so the hook stays silent rather than guessing.
    """
    result: list[Segment] = []
    inner: list[Segment] = []
    heredocs = []
    parts: list[str] = []
    prev = None
    chunk = i = 0
    quote = None

    def close(end: int, op: str) -> None:
        nonlocal prev, parts
        text = "".join(parts) + command[chunk:end]
        parts = []
        result.extend(inner)
        inner.clear()
        segment = Segment(text, op)
        if prev is not None and prev.op in ("|", "|&"):
            prev.piped = text
        prev = segment
        result.append(segment)

    while i < len(command):
        char = command[i]
        if quote == "'":
            if char == "'":
                quote = None
            i += 1
            continue
        if char == "\\":
            i += 2
            continue
        if char == "`" or command.startswith("$(", i):
            if char == "`":
                body_start, end = i + 1, _match_backtick(command, i + 1)
            else:
                body_start, end = i + 2, _match_paren(command, i + 2)
            if end == -1:
                return []
            body = command_segments(command[body_start:end])
            if not body and command[body_start:end].strip():
                return []
            inner.extend(body)
            parts.append(command[chunk:i])
            parts.append(SUBST)
            chunk = i = end + 1
            continue
        if quote:
            if char == quote:
                quote = None
            i += 1
            continue
        if char in "'\"":
            quote = char
        elif char == "#" and (i == 0 or command[i - 1] in " \t\n|;&"):
            end = command.find("\n", i)
            close(i, "")
            i = chunk = len(command) if end == -1 else end
            continue
        elif command.startswith("<<<", i):
            i += 3  # a here-string, not a heredoc
            continue
        elif command.startswith("<<", i):
            # Queue delimiters now; their bodies begin after the next newline.
            strip_tabs = command.startswith("<<-", i)
            word_start = i + (3 if strip_tabs else 2)
            while word_start < len(command) and command[word_start] in " \t":
                word_start += 1
            match = HEREDOC_WORD.match(command, word_start)
            if not match:
                return []
            try:
                delimiter, = shlex.split(match.group())
            except ValueError:
                return []
            heredocs.append((delimiter, strip_tabs))
            i = match.end()
            continue
        elif char in "|;&\n":
            if char == "&" and ((i and command[i - 1] == ">") or command.startswith("&>", i)):
                i += 1  # a redirection (2>&1, &>file), not a separator
                continue
            op = command[i:i + 2] if command[i:i + 2] in ("&&", "||", "|&", ";;") else char
            close(i, op)
            i += len(op)
            chunk = i
            if char == "\n":
                for delimiter, strip_tabs in heredocs:
                    while chunk < len(command):
                        end = command.find("\n", chunk)
                        end = len(command) if end == -1 else end + 1
                        line = command[chunk:end].rstrip("\n")
                        chunk = end
                        if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                            break
                    else:
                        return []
                heredocs.clear()
                i = chunk
            continue
        i += 1
    if quote or heredocs:
        return []
    close(len(command), "")
    return result


def _pin_value(value: str) -> str:
    """A board pin as the CLI would see it; UNKNOWN when it hangs on the shell's expansion."""
    if "$" in value or "`" in value:
        return UNKNOWN
    return value.strip().lower()


def strip_prefix(words: list[str]):
    """Drop leading keywords, VAR=val words and env/timeout/nice/command/exec wrappers.

    Returns (words, pins, unknown): pins maps variable -> value set for this command;
    unknown is True when a wrapper (env -i, env -S) changes the environment in a way we do not model.
    """
    pins: dict[str, str] = {}
    unknown = False
    while words:
        word = words[0]
        if word.startswith("("):
            rest = word.lstrip("(")
            words = ([rest] if rest else []) + words[1:]
            continue
        match = ASSIGN.match(word)
        if match:
            pins[match.group(1)] = match.group(2)
            words = words[1:]
            continue
        if word in LEADING_KEYWORDS:
            words = words[1:]
            continue
        name = Path(word).name
        if name == "env":
            words = words[1:]
            while words:
                arg = words[0]
                match = ASSIGN.match(arg)
                if match:
                    pins[match.group(1)] = match.group(2)
                    words = words[1:]
                elif arg in ("-u", "--unset") and len(words) > 1:
                    pins[words[1]] = ""
                    words = words[2:]
                elif arg.startswith("--unset="):
                    pins[arg.partition("=")[2]] = ""
                    words = words[1:]
                elif arg in ("-C", "--chdir") and len(words) > 1:
                    words = words[2:]
                elif arg == "--":
                    words = words[1:]
                    break
                elif arg.startswith("-"):
                    unknown = unknown or arg in ("-i", "-", "--ignore-environment") or arg.startswith(("-S", "--split-string"))
                    words = words[1:]
                else:
                    break
        elif name == "timeout":
            words = words[1:]
            while words and words[0].startswith("-"):
                words = words[2 if words[0] in ("-s", "-k", "--signal", "--kill-after") else 1:]
            words = words[1:]  # the duration
        elif name == "nice":
            words = words[1:]
            if words and words[0] in ("-n", "--adjustment"):
                words = words[2:]
            elif words and words[0].startswith("-"):
                words = words[1:]
        elif name in ("command", "exec"):
            words = words[1:]
            while words and words[0].startswith("-"):
                if words[0] in ("-v", "-V"):
                    return [], pins, unknown  # a lookup, not an execution
                words = words[2 if (name == "exec" and words[0] == "-a") else 1:]
        else:
            break
    return words, pins, unknown


class Filed:
    __slots__ = ("argv", "pin", "kind", "jq")

    def __init__(self, argv, pin, kind, jq):
        self.argv, self.pin, self.kind, self.jq = argv, pin, kind, jq


def filed_commands(command: str) -> list[Filed]:
    """Every `hermes kanban create|swarm` run by the command, with the board pin in force."""
    found = []
    state = None  # what earlier segments exported for HERMES_KANBAN_BOARD (None: nothing)
    shell_vars: dict[str, str] = {}
    for segment in command_segments(command):
        text = segment.text
        has_hermes = "hermes" in text
        has_var = PIN_VAR in text
        if not has_hermes and not has_var:
            continue
        try:
            words = shlex.split(text)
        except ValueError:
            if has_hermes:
                return []
            state = UNKNOWN
            continue
        words, pins, env_unknown = strip_prefix(words)
        if not words:
            if PIN_VAR in pins:
                shell_vars[PIN_VAR] = _pin_value(pins[PIN_VAR])
            continue
        head = Path(words[0]).name
        if head != "hermes":
            if has_var:
                if head == "export":
                    for word in words[1:]:
                        name, equals, value = word.partition("=")
                        if name == PIN_VAR:
                            if equals:
                                state = _pin_value(value)
                            elif PIN_VAR in shell_vars:
                                state = shell_vars[PIN_VAR]
                elif head == "unset":
                    if PIN_VAR in words[1:]:
                        state = ""
                elif head in ("declare", "typeset", "eval", "source", ".") or PIN_VAR + "=" in text:
                    state = UNKNOWN
            continue
        if "--help" in words or "kanban" not in words:
            continue
        index = words.index("kanban") + 1
        # --board is also accepted before the create/swarm subcommand.
        while index < len(words):
            if words[index] == "--board":
                index += 2
            elif words[index].startswith("--board="):
                index += 1
            else:
                break
        if index < len(words) and words[index] in {"create", "swarm"}:
            pin = _pin_value(pins[PIN_VAR]) if PIN_VAR in pins else state
            if env_unknown:
                pin = UNKNOWN
            found.append(Filed(words, pin, words[index], bool(JQ_NEXT.match(segment.piped))
                               and segment.op in ("|", "|&")))
    return found


def _documents(output: str) -> list:
    """Every JSON document in the text, in order, stopping at the first thing that is not one."""
    decoder = json.JSONDecoder()
    docs, index, end = [], 0, len(output)
    while index < end:
        while index < end and output[index].isspace():
            index += 1
        if index >= end:
            break
        try:
            doc, index = decoder.raw_decode(output, index)
        except (ValueError, RecursionError):
            break
        docs.append(doc)
    return docs


def _id_fields(value):
    if isinstance(value, dict):
        card = value.get("id")
        if isinstance(card, str) and CARD_ID.fullmatch(card):
            yield card
        for child in value.values():
            yield from _id_fields(child)
    elif isinstance(value, list):
        for child in value:
            yield from _id_fields(child)


def _swarm_fields(value):
    """The ids `hermes kanban swarm --json` prints: root_id, worker_ids[], verifier_id, synthesizer_id."""
    docs = value if isinstance(value, list) else [value]
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        singles = [doc.get("root_id")]
        workers = doc.get("worker_ids")
        singles.extend(workers if isinstance(workers, list) else [])
        singles.extend([doc.get("verifier_id"), doc.get("synthesizer_id")])
        for card in singles:
            if isinstance(card, str) and CARD_ID.fullmatch(card):
                yield card


def created_cards(output, filed: list[Filed]) -> list[str]:
    if not isinstance(output, str):
        return []
    kinds = {item.kind for item in filed}
    docs = _documents(output)
    cards: list[str] = []
    if "create" in kinds:
        if docs:
            cards.extend(_id_fields(docs))
        else:
            cards.extend(CREATED.findall(output))
            if any(item.jq for item in filed if item.kind == "create"):
                cards.extend(BARE_ID.findall(output))
    if "swarm" in kinds:
        if docs:
            cards.extend(_swarm_fields(docs))
        else:
            for line in SWARM_LINE.findall(output):
                cards.extend(CARD_ID.findall(line))
    return list(dict.fromkeys(cards))


def _hermes_root() -> Path:
    """Where the CLI keeps kanban state: HERMES_KANBAN_HOME, else the default Hermes root."""
    override = os.environ.get("HERMES_KANBAN_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    native = Path.home() / ".hermes"
    env_home = os.environ.get("HERMES_HOME", "")
    if env_home:
        env_path = Path(env_home)
        try:
            env_path.resolve().relative_to(native.resolve())
        except ValueError:
            return env_path.parent.parent if env_path.parent.name == "profiles" else env_path
    return native


def board_for(argv: list[str], pin=None, opaque: bool = False) -> str:
    for index, arg in enumerate(argv):
        value = None
        if arg == "--board" and index + 1 < len(argv):
            value = argv[index + 1]
        elif arg.startswith("--board="):
            value = arg.partition("=")[2]
        if value is not None:
            value = value.strip()
            return value if "$" in value else value.lower()
    if opaque or os.environ.get("HERMES_KANBAN_DB") or pin == UNKNOWN or os.environ.get("HOME") == "":
        return UNKNOWN
    try:
        root = _hermes_root()
    except (OSError, RuntimeError, ValueError):
        return UNKNOWN

    def exists(slug: str) -> bool:
        if slug == "default":
            return True
        if "/" in slug or slug in (".", ".."):
            return False
        return (root / "kanban" / "boards" / slug).is_dir()

    candidate = (os.environ.get("HERMES_KANBAN_BOARD", "") if pin is None else pin).strip().lower()
    if candidate and exists(candidate):
        return candidate
    try:
        current = (root / "kanban" / "current").read_text().strip().lower()
    except FileNotFoundError:
        return "default"
    except (OSError, UnicodeError):
        return UNKNOWN
    return current if current and exists(current) else "default"


def run() -> int:
    event = json.load(sys.stdin)
    if not isinstance(event, dict) or event.get("tool_name") != "Bash":
        return 0
    tool_input = event.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str) or "hermes" not in command or "kanban" not in command:
        return 0
    filed = filed_commands(command)
    if not filed:
        return 0
    response = event.get("tool_response")
    output = response if isinstance(response, str) else response.get("stdout") if isinstance(response, dict) else None
    cards = created_cards(output, filed)
    if not cards:
        return 0
    opaque = bool(UNFOLLOWABLE.search(command))
    boards = {board_for(item.argv, item.pin, opaque) for item in filed}
    board = boards.pop() if len(boards) == 1 else UNKNOWN
    if SUBST in board:
        board = UNKNOWN
    if board.startswith("$"):
        # A shell variable: the hook cannot see its value, so name it rather than guess.
        board = f"<the board in {board}>"
    note = (
        f"Kanban card(s) were just filed on board {board}: {', '.join(cards)}. "
        f"After filing the wave, run /kanban-watch with args "
        + json.dumps({"board": board, "tasks": cards})
        + ". Use one watcher for the whole wave; add phases when its stages are known. "
        "Skip this if a watcher already covers these cards, or a watched card filed them. "
        "Watchers only read; they never dispatch."
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note}}))
    return 0


def main() -> int:
    try:
        return run()
    except Exception:  # a PostToolUse nudge must never fail the tool call (RecursionError included)
        return 0


if __name__ == "__main__":
    sys.exit(main())
