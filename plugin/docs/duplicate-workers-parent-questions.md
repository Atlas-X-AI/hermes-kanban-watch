# Duplicate workers and questions parked on the parent

`snapshot`, `report` (JSON or text) and `cards` scan `/proc/*/cmdline` once per
invocation. More than one argv containing the exact `work kanban task <id>`
query produces `warnings.duplicate_workers: [pid, ...]`, sorted numerically.
Zero or one matching process adds no duplicate warning. This is a point-in-time
observation across the visible proc tree, not a board/worktree ownership check.
The reader never signals or repairs workers. Set `KANBAN_LIVE_PROC_ROOT` to
override `/proc` (for example, with a disposable test tree). An unreadable root or
cmdline makes `warnings.duplicate_workers` the string `"UNKNOWN"`, not an empty
list; a process disappearing during the scan is skipped normally. These process
warnings are separate from, and do not replace, the report's run-event counts.

When a `todo` or `ready` card's latest event is `dependency_wait` and no later run
has started, `snapshot` adds `waiting_on: "parent"` and `waiting_text` containing
the full last-run summary (null if unavailable), even without a block reason.
`needs` reports that same current wait as a `parent_question`, without requiring
operator wording or `--created-by`. If supplied, `--created-by` still scopes parent
questions to that creator. The existing `--since` boundary, `--loop` first-poll
backlog and seen-cursor deduplication apply. A plain queued card, a later event
or a later run does not acquire this parent-wait label. Other states retain
their existing `needs` behaviour. No card, run or process is modified.
