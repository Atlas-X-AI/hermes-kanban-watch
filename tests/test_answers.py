"""Standing answers are proposals, never board writes or automatic replies."""
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys

import pytest

from conftest import PLUGIN


# The red probe points the same unchanged test at a plain origin/main export.
ANSWERS_PLUGIN = Path(os.environ.get("ANSWERS_TEST_PLUGIN_ROOT", PLUGIN))
SCRIPT = ANSWERS_PLUGIN / "scripts/answers.py"
TEMPLATE = ANSWERS_PLUGIN / "templates/answers.yaml"


def answers(*args, needs=None):
    return subprocess.run([sys.executable, str(SCRIPT), *args],
                          input=json.dumps(needs) if needs is not None else None,
                          capture_output=True, text=True, timeout=10)


def rule(rule_id="capacity", **changes):
    return {"id": rule_id, "match": ["EVO.*BUSY|load .* limit"],
            "answer": "commit, push and let PR CI gate", "note": "Capacity is constrained",
            **changes}


def rules_file(tmp_path, rules):
    path = tmp_path / "answers.json"
    path.write_text(json.dumps(rules))
    return path


def suggest(path, items, *args):
    return answers("suggest", "--answers", str(path), "--board", "demo",
                   "--author", "parent", *args, needs={"now": 123, "items": items})


def test_template_capacity_known_red():
    out = suggest(TEMPLATE, [{"task": "t_capacity", "text":
                  "EVO is BUSY and the box is over its load capacity limit; cannot run the full suite"}])
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    assert result == {"task": "t_capacity", "rule_id": "capacity",
                      "answer": "commit, push and let PR CI gate",
                      "comment_cmd": 'hermes kanban --board demo comment t_capacity "commit, push and let PR CI gate" --author parent'}


@pytest.mark.parametrize("text", ["evo is busy", "load exceeds limit", "Load ABOVE Limit"])
def test_template_capacity_alternatives(text):
    out = suggest(TEMPLATE, [{"task": "t_capacity", "text": text}])
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["rule_id"] == "capacity"


def test_all_patterns_must_match_case_insensitively(tmp_path):
    path = rules_file(tmp_path, [rule(match=[r"\bEVO\b", "busy"])])
    out = suggest(path, [{"task": "t_yes", "text": "evo IS bUsY"},
                         {"task": "t_no", "text": "EVO is available"}])
    assert out.returncode == 0, out.stderr
    results = [json.loads(line) for line in out.stdout.splitlines()]
    assert results[0]["rule_id"] == "capacity"
    assert results[1] == {"task": "t_no", "rule_id": None, "answer": None, "comment_cmd": None}


def test_ambiguous_reports_every_matching_id_without_answer(tmp_path):
    path = rules_file(tmp_path, [rule("one"), rule("two"), rule("three")])
    out = suggest(path, [{"task": "t_ambiguous", "text": "EVO BUSY"}])
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    assert result == {"task": "t_ambiguous", "rule_id": "AMBIGUOUS",
                      "rule_ids": ["one", "two", "three"], "answer": None, "comment_cmd": None}


def test_needs_file_and_default_board(tmp_path):
    path = rules_file(tmp_path, [rule()])
    needs = tmp_path / "needs.json"
    needs.write_text(json.dumps({"items": [{"task": "t_file", "text": "EVO BUSY"}]}))
    out = answers("suggest", "--answers", str(path), "--author", "chair",
                  "--needs-json", str(needs))
    assert out.returncode == 0, out.stderr
    assert shlex.split(json.loads(out.stdout)["comment_cmd"]) == [
        "hermes", "kanban", "--board", "default", "comment", "t_file",
        "commit, push and let PR CI gate", "--author", "chair"]


def test_explicit_stdin_and_empty_items(tmp_path):
    path = rules_file(tmp_path, [rule()])
    out = suggest(path, [], "--needs-json", "-")
    assert out.returncode == 0, out.stderr
    assert out.stdout == ""


def test_consumes_real_reader_needs_output_from_disposable_board(board):
    with sqlite3.connect(board) as con:
        con.execute("create table task_comments(id integer primary key, task_id text, body text, created_at int)")
        con.execute("insert into task_comments values(1, 't_run', 'ASK OPERATOR: EVO BUSY', 123)")
    needs = subprocess.run([sys.executable, str(PLUGIN / "scripts/kanban_live.py"), "needs",
                            "--board", "demo", "--since", "0", "--max-seconds", "1"],
                           capture_output=True, text=True, timeout=10)
    assert needs.returncode == 0, needs.stderr
    out = answers("suggest", "--answers", str(TEMPLATE), "--board", "demo",
                  "--author", "parent", needs=json.loads(needs.stdout))
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["rule_id"] == "capacity"


def test_command_preserves_shell_metacharacters_without_running_them(tmp_path):
    answer = 'It\'s "busy"; $HOME `whoami` $(touch marker) \\ done\nsecond line'
    path = rules_file(tmp_path, [rule(answer=answer)])
    out = answers("suggest", "--answers", str(path), "--board", "board with spaces; false",
                  "--author", "parent's seat", needs={"items": [{"task": "t_quoted", "text": "EVO BUSY"}]})
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    assert result["answer"] == answer
    # shlex is not a shell: it retains backslashes before $ and ` in double
    # quotes. Exercise the actual pasted command against a recording stub.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    captured = tmp_path / "argv.json"
    stub = bindir / "hermes"
    stub.write_text(f"#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n"
                    f"Path({str(captured)!r}).write_text(json.dumps(sys.argv))\n")
    stub.chmod(0o755)
    shell_script = tmp_path / "paste-command.sh"
    shell_script.write_text(result["comment_cmd"] + "\n")
    subprocess.run(["sh", str(shell_script)], cwd=tmp_path, check=True, timeout=10,
                   env={**os.environ, "PATH": str(bindir) + os.pathsep + os.environ["PATH"]})
    assert json.loads(captured.read_text())[1:] == [
        "kanban", "--board", "board with spaces; false", "comment", "t_quoted",
        answer, "--author", "parent's seat"]
    assert not (tmp_path / "marker").exists()
    assert '\\$HOME' in result["comment_cmd"] and '\\`whoami\\`' in result["comment_cmd"]


def test_script_never_runs_hermes(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "hermes-was-run"
    stub = bindir / "hermes"
    stub.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nPath({str(marker)!r}).write_text('called')\n")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    # Positive control: the stub really is executable and records an invocation.
    subprocess.run([str(stub)], check=True, timeout=10)
    assert marker.read_text() == "called"
    marker.unlink()
    out = suggest(TEMPLATE, [{"task": "t_print_only", "text": "EVO BUSY"}])
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["comment_cmd"].startswith("hermes kanban ")
    check = answers("check", "--answers", str(TEMPLATE))
    assert check.returncode == 0, check.stderr
    assert not marker.exists()


@pytest.mark.parametrize("rules, error", [
    ([rule(match=["["])], "regex"),
    ([rule(), rule()], "unique"),
    ([rule(answer="")], "answer"),
    ([rule(answer="  \n ")], "answer"),
    ([rule(answer=42)], "answer"),
    ([rule(answer="NUL\x00cannot be pasted")], "answer"),
    ([rule(match=[])], "match"),
    ([rule(match="busy")], "match"),
    ([rule(match=[123])], "regex"),
    ([rule(rule_id="")], "id"),
    ([rule(id=42)], "id"),
    ([rule(rule_id="AMBIGUOUS")], "id"),
    ([rule(note=42)], "note"),
    ([{"id": "missing-fields"}], "match"),
    ({"rules": [rule()]}, "list"),
    (["not a mapping"], "mapping"),
])
def test_check_rejects_invalid_rules_without_partial_suggestions(tmp_path, rules, error):
    path = rules_file(tmp_path, rules)
    out = answers("check", "--answers", str(path))
    assert out.returncode == 1 and error in out.stderr.lower(), out.stderr
    assert not out.stdout
    out = suggest(path, [{"task": "t_invalid", "text": "EVO BUSY"}])
    assert out.returncode == 1 and not out.stdout
    assert "Traceback" not in out.stderr


def test_check_accepts_json_and_empty_rule_list(tmp_path):
    for rules in ([rule()], []):
        out = answers("check", "--answers", str(rules_file(tmp_path, rules)))
        assert out.returncode == 0, out.stderr


def test_yaml_indentationless_match_list(tmp_path):
    path = tmp_path / "answers.yaml"
    path.write_text("- id: capacity\n  match:\n  - 'EVO.*BUSY'\n  answer: use CI\n")
    out = suggest(path, [{"task": "t_yaml", "text": "EVO BUSY"}])
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["answer"] == "use CI"


def test_yaml_inline_regex_quotes_commas_and_brackets_in_comments(tmp_path):
    path = tmp_path / "answers.yaml"
    path.write_text('''- id: precise
  match: ['[a-z]{1,3}', "busy", 'it''s'] # ] is only a comment
  answer: use CI
''')
    out = suggest(path, [{"task": "t_yaml", "text": "it's busy"}])
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["rule_id"] == "precise"


def test_yaml_strings_regex_lists_comments_and_multiple_rules(tmp_path):
    path = tmp_path / "answers.yaml"
    path.write_text('''# A user-edited YAML rule list
- id: capacity
  match: ['\\bEVO\\b', "(?i:BUSY)"] # AND, not OR
  answer: 'It''s busy: use CI # not a comment'
  note: "capacity: checked"
- id: load
  match:
    - 'load \\d+ above limit'
  answer: commit and push # actual comment
  note: available capacity
''')
    out = suggest(path, [{"task": "t_yaml", "text": "evo is BUSY"},
                         {"task": "t_load", "text": "load 30 above limit"}])
    assert out.returncode == 0, out.stderr
    result = [json.loads(line) for line in out.stdout.splitlines()]
    assert result[0]["answer"] == "It's busy: use CI # not a comment"
    assert result[1]["rule_id"] == "load" and result[1]["answer"] == "commit and push"


@pytest.mark.parametrize("source", [
    "",
    "# Only a comment, not a rule list\n",
    '- id: duplicate\n  id: other\n  match: [busy]\n  answer: yes\n',
    '[{"id":"one","id":"two","match":["busy"],"answer":"yes"}]',
    '- id: bad\n  match: ["["]\n  answer: yes\n',
    '- id: bad\n  match: [busy\n  answer: yes\n',
    '- id: bad\n  match: [busy]]\n  answer: yes\n',
    '- id: bad\n  match: [busy]\n    - other\n  answer: yes\n',
    '- id: bad\n  match: [busy]\n  answer: "unterminated\n',
    '- id: bad\n  match: [busy]\n  answer: !!python/object:danger {}\n',
])
def test_malformed_files_fail_cleanly(tmp_path, source):
    path = tmp_path / "bad.yaml"
    path.write_text(source)
    out = answers("check", "--answers", str(path))
    assert out.returncode == 1, out.stdout
    assert not out.stdout and "Traceback" not in out.stderr


@pytest.mark.parametrize("needs", [[], {}, {"items": {}}, {"items": [None]},
                                  {"items": [{"task": "t_bad"}]},
                                  {"items": [{"task": "t_bad", "text": 5}]},
                                  {"items": [{"task": "--help", "text": "busy"}]},
                                  {"items": [{"task": "t_good", "text": "busy"}, {}]}])
def test_invalid_needs_envelopes_fail_without_partial_output(tmp_path, needs):
    path = rules_file(tmp_path, [rule(match=["busy"])])
    out = answers("suggest", "--answers", str(path), "--author", "parent", needs=needs)
    assert out.returncode == 1, out.stdout
    assert not out.stdout and "Traceback" not in out.stderr


def test_missing_and_malformed_input_files(tmp_path):
    missing = tmp_path / "missing"
    out = answers("check", "--answers", str(missing))
    assert out.returncode == 1 and not out.stdout
    path = rules_file(tmp_path, [rule()])
    out = answers("suggest", "--answers", str(path), "--author", "parent", "--needs-json", str(missing))
    assert out.returncode == 1 and not out.stdout
    missing.write_text("{broken JSON")
    out = answers("suggest", "--answers", str(path), "--author", "parent", "--needs-json", str(missing))
    assert out.returncode == 1 and not out.stdout and "Traceback" not in out.stderr


def test_author_is_explicit_not_inferred(tmp_path):
    path = rules_file(tmp_path, [rule()])
    out = answers("suggest", "--answers", str(path), needs={"items": []})
    assert out.returncode == 2 and "--author" in out.stderr
