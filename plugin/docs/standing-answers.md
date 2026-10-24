# Suggest standing answers without posting

Validate a user-edited rule file, then feed the reader's `{now, items}` JSON to
the standalone suggester. Replace `<plugin-root>` with the installed plugin:

```sh
python3 <plugin-root>/scripts/answers.py check --answers <plugin-root>/templates/answers.yaml
hermes-kanban-live needs --board demo --since 1792700000 | python3 <plugin-root>/scripts/answers.py suggest --answers <plugin-root>/templates/answers.yaml --board demo --author parent
python3 <plugin-root>/scripts/answers.py suggest --answers /path/to/answers.json --needs-json /path/to/needs.json --board demo --author parent
```

`--needs-json -` (the default) reads stdin. `--board` defaults to `default` and
must identify the board used by `needs`, whose output does not carry a board.
`--author` is required and used literally, never inferred from the environment.
The script never invokes Hermes, posts, unblocks, dispatches or changes a card.
Only a human or authorised parent may review and run the printed command; these
are proposals, not permission to act or evidence that capacity is available.

Rules are a top-level JSON/YAML list of `{id, match: [regex, ...], answer, note}`.
All regexes in a rule must match the item's `text`, case-insensitively. Use one
regex with `|` for alternatives. The shipped `capacity` example matches
`EVO.*BUSY|load .* limit` and suggests `commit, push and let PR CI gate`.
Edit your own copy to add rules. The dependency-free YAML subset supports
two-space rule mappings, inline or block match lists (also indentationless),
single-line plain/single-/double-quoted strings and comments. Single-quoted
regexes preserve backslashes; doubled apostrophes escape single quotes, and
double-quoted strings use JSON escapes. Use JSON for multiline answers. YAML
tags, anchors, aliases and multiline scalars are refused.

Each input item produces one JSON line with `task`, `rule_id`, `answer` and
`comment_cmd`. A unique match prints a shell-escaped command of the form
`hermes kanban --board B comment TASK "ANSWER" --author A`. No match leaves
the last three fields null. Multiple matches set `rule_id: "AMBIGUOUS"`, add
all matching ids as `rule_ids`, and leave `answer` and `comment_cmd` null; rule
order never breaks a tie. `AMBIGUOUS` is a reserved id. Review every proposal
against the card's current state and scope before running it. Shell quoting
preserves literal quotes, substitutions and newlines in answers.

`check` validates unique non-empty ids, non-empty match lists, compilable regexes
and non-blank string answers (without NUL); an optional note must be a string.
It exits 0 with `{"valid": true, "rules": N}`. Malformed files/items exit 1 with
no suggestions, even if earlier items matched; missing CLI options exit 2.
An empty items/rules list is valid and never triggers a post.
