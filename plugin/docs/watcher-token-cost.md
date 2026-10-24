# Watcher token cost

Inspect Claude Code watcher usage separately from Hermes worker elapsed time:

```sh
python3 <plugin-root>/scripts/watch_cost.py --run <workflow-id>
python3 <plugin-root>/scripts/watch_cost.py --root /path/to/transcripts --run <workflow-id> --json
python3 <plugin-root>/scripts/watch_cost.py --root /path/to/transcripts --workflow-run project/session/subagents/workflows/<workflow-id>
```

The default transcript root is `~/.claude/projects`; `--root DIR` replaces it.
`--run ID` is repeatable and scans the normal project/session workflow layout.
`--workflow-run DIR` selects one directory within that root (absolute or
root-relative) and prints one counts-only line: agents, turns, input/output,
cache reads, 5-minute/1-hour cache writes and cold wakes. It cannot be combined
with `--run` or `--json`. `--since YYYY-MM-DD` retains whole agents by file mtime
or first transcript timestamp, not individual turns.

The legacy table and JSON retain run/role/model breakdowns and weighted
input-equivalent tokens, not dollars. The weights are explicit assumptions in
`--help`, not vendor prices; weighted totals are never summed across models.
Distinct assistant message IDs count as turns, with the last cumulative usage
winning. Cold wakes mean API calls more than 300 seconds apart, a timing proxy,
not proof of cache expiry. Namespaced plugin and legacy watcher sidecars are
recognised; without one, a recognised reader command in the computed task selects
the watcher. Other agent types and bare script mentions are excluded.

This stdlib-only POSIX reader opens only regular transcript/journal/sidecar files
through directory descriptors anchored inside the root. Symlinks and paths
escaping the root are refused, including sidecars and journal links; journal
agent IDs never become paths. It never opens the board or credential files,
prints message text, changes cards, calls a model or writes data. Malformed
journals warn while transcripts are still counted; corrupt selected watcher
transcripts fail with exit 2 rather than print partial totals. A zero-watcher
explicit selection warns rather than pretending a watch was free.
