# One watcher per board

The default watch is one cheap native background agent, not the retained
`workflows/kanban-watch.js`. The starting session launches it right after filing
and relays every 15-minute summary, immediate problem note and final note to the
operator in their current channel. The watcher never messages a human directly.

## Choose the starting harness, not a second model host

| Starting harness | ONE watcher | Parent delivery |
| --- | --- | --- |
| Claude Code | Plugin `kanban-watcher` native background Agent, Sonnet | Exact-session inbox row via `notify_session.py`; final native Agent return |
| Codex | Its own native background subagent, Luna (`gpt-5.6-luna` or newest account-listed Luna) | Native child-to-parent reporting; periodic route UNKNOWN until exercised |
| Hermes | Dedicated `kanban-watcher` profile, Gemini Argon with cheap GLM fallback | Native child-to-parent reporting; periodic route UNKNOWN until exercised |

The Hermes launcher inherits that profile's model/provider. The profile name has
one configurable default, `templates/watcher.json`; this plugin never reads or
edits its configuration. Do not launch Hermes in addition to a Claude/Codex
native watcher. One local lease is shared across these launch paths.

## Prepare a native watcher

From the installed plugin root, for Claude Code:

```sh
bash scripts/start_watcher.sh --prepare-only --board demo --session exact-tmux-name --every 900 --max-hours 12
```

The JSON receipt names `brief`, `config` and the exact helper `command`. Give the
brief to ONE background Sonnet Agent of type `hermes-kanban-watch:kanban-watcher`.
This preparation starts no agent. The brief commands `watcher_tick.py --config`
once per wake, in the foreground, with a timeout large enough for the wait. Use
the tool's documented timeout units; a tool that cannot await that interval needs
its own supported asynchronous wait, not faster model polling. Such a route is
UNKNOWN until exercised, not silently shortened below the required cadence.

For Codex or Hermes native reporting:

```sh
bash scripts/start_watcher.sh --prepare-only --delivery native --board demo --every 900 --max-hours 12
```

Give the returned brief to the one native watcher described above. With `native`,
the helper prints the same summary without touching a Claude inbox; the agent
must forward it through the harness's supported parent-progress mechanism. It
returns `native_parent_delivery: UNKNOWN`. Do not treat captured stdout, a journal
entry or a final agent reply as proof of periodic progress delivery. This plugin
does not invent a transport or promise unsupported native background notifications.

## Explicit Hermes-to-Claude bridge and proof

This optional bridge starts a Hermes agent, not a Claude subagent:

```sh
bash scripts/start_watcher.sh --board demo --session exact-tmux-name --every 900 --max-hours 12
```

It submits `systemd-run --user --unit kanban-watcher-demo --collect` with
`hermes --profile kanban-watcher chat`, 1600 turns, a wall-clock budget and a
RuntimeMaxSec backstop. `launch: submitted` proves only command submission,
not model health. Inspect `systemctl --user status kanban-watcher-demo` and
`journalctl --user -u kanban-watcher-demo` for the actual run. No profile file is
changed. Use `--every 5 --max-hours 0.1` for a parent-authorised isolated proof;
never test by starting a second watcher against a live board.

`--cards t_one t_two` restricts membership to exactly those cards. Without it,
every snapshot reads the whole board, including new cards. Only done/archived
are terminal; blocked/review keep the watcher alive. A final summary ends the
agent. A max_hours stop explicitly says it is not completion.

## Reader and summary

```sh
python3 scripts/board_watch.py snapshot --board demo --cards t_one t_two
python3 scripts/board_watch.py wait --board demo --cards t_one t_two --every 900 --state-file /path/to/local-cursor.json
python3 scripts/board_watch.py summary --snapshot-file /path/to/snapshot.json
```

`summary` also reads JSON from stdin and accepts a wait envelope. It produces at
most 12 plain-text lines: done / running / waiting / problems / rough time left,
plus bounded problem details. Archived is counted as finished. Waiting includes
blocked/review; UNKNOWN is never promoted to a pass. Actual model/provider come
from the current run pointer, or the latest run when the pointer is null. No
worker profile, session store or log is opened. Runtime caps are not ETAs, so
rough time left is UNKNOWN until all watched cards finish.

`wait` polls every 60 seconds, or sooner at a summary/deadline boundary. Existing
problems wake its first call; later calls wake for NEW problem IDs only. Alerts
do not postpone the summary deadline. Fixed-state repeats do not spin or notify
continuously. A problem can re-alert after recovery and recurrence.
Problems are blocked, ready over 600 seconds with no recorded worker, unresolved
respawn_guarded/spawn_failed events, a current dependency_wait parent question,
latest `ASK PARENT:` / `ASK OPERATOR:` comment, or a live run beyond its recorded
max runtime. Unheaded prose is not a question; a later answer/comment or resumed
run clears the old ask. There is no natural-language question inference.

`--home` selects the board-data root explicitly. Otherwise use `HERMES_HOME`,
default `~/.hermes`. A worker profile's HERMES_HOME is not the shared board root.
Default board: `kanban.db`; named boards: `kanban/boards/<slug>/kanban.db`.
Missing cards, unreadable/schema-invalid databases and invalid scopes fail
closed with non-zero/UNKNOWN, never a final note. SQLite uses mode=ro/query_only.
The reader does not invoke any board-mutation CLI.

## Delivery and local state

```sh
python3 scripts/notify_session.py --session exact-tmux-name --text-file /path/to/summary.txt
```

It uses tmux's exact `=<session>:` target for `list-panes`, refusing missing,
prefix-only or multi-pane targets without inserting a row. The message target
is `=<session>:.%N`, so moving the pane to another session invalidates delivery.
One `project_channel_message` row carries the text/render shape of the existing
session-channel writer. It never types directly into tmux or creates an inbox.
The receipt reads the exact row back: `notified: 0` queued, `2` held, `1` notified.
Poller health, idle-prompt behaviour and human receipt require a live proof.

State is private under `XDG_STATE_HOME/hermes-kanban-watch` (default
`~/.local/state/hermes-kanban-watch`). A board lease prevents a second native or
bridge watcher; systemd's unit name is a second bridge-only singleton gate.
Final/max_hours/UNKNOWN-read stops release the local lease. A killed watcher or
failed delivery keeps it until its deadline; inspect it rather than silently
starting a duplicate. No state directory is automatically removed.
Pending delivery is durable and has a stable dedupe key. An authorised retry
uses the SAME config, not another watch. The helper verifies lease ownership
again before delivery, so a superseded watcher cannot send into a newer run.

## Run policy and legacy tools

Read `doctrine/running-a-kanban.md` for reviews last, end-of-run CI (large-change
exception), the review lane and repo memory after every run. The starting session
owns that policy and the human relay; this feature never creates review cards,
runs CI, merges, updates memory or dispatches work. The note is a separate change;
if not yet available, its load is UNKNOWN.

The legacy Workflow and `kanban_live.py` tools remain available only on explicit
request. Their old per-row agents, session-store reads and options are NOT part
of the default board-only watcher. Tests use disposable homes, real temporary SQL
and fake tmux/systemd/Hermes commands; no live profile, model or inbox is exercised.
