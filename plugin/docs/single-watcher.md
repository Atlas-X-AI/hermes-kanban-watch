# One watcher per board

The default watch is one cheap native background agent, not the retained
`workflows/kanban-watch.js`. The starting session launches it right after filing.
When the operator works in Discord, configure `--discord-channel`: the deterministic
helper posts reports directly and the starting session does not relay them. Without
it, the starting session relays summaries, immediate problems and the final note.
The watcher agent never composes or sends a human message itself.

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
bash scripts/start_watcher.sh --prepare-only --board demo --session exact-tmux-name --discord-channel <channel-id> --every 900 --max-hours 12
```

Omit `--discord-channel` for the unchanged inbox-only parent relay.
The JSON receipt names `brief`, `config` and the exact helper `command`. Give the
brief to ONE background Sonnet Agent of type `hermes-kanban-watch:kanban-watcher`.
This preparation starts no agent. For Claude Code, run `watcher_tick.py --config`
with Bash `run_in_background: true`, wait for its task-completion notification,
then read only the last lines of that command's output for CONTINUE or STOP.
Never poll the background command and never sleep in a loop. The synchronous
Bash limit cannot cover 900 seconds; do not shorten the helper interval to fit it.
The Hermes bridge keeps its own foreground terminal-timeout instructions.

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

## Direct Discord reports

`--discord-channel <id>` is the default for a Claude Code operator working in
Discord. It requires `--session` for the exact starting session, validates a
numeric channel ID, and stores `discord_channel` in the watcher config. The helper
calls `~/.claude/skills/messaging-hayden/bin/send-hayden.sh <draft> --channel <id>`
once for each summary, problem and FINAL. It also reports read UNKNOWN and the
time-limit stop without claiming completion. No model formats or sends a report.

Drafts contain at most eight lines: done, running, waiting, problems, rough time
left and next-step context, each with one state marker. The final line is
`-# Source: <starting-session>`. Only counts and fixed prose enter the draft;
card titles, problem text, board names, paths, SHAs and branches are never copied.
The local plain-text summary still gives the parent the diagnostic detail.

The wrapper must lint, send and read the message back. Any non-zero exit,
missing final `ok <id> reactions=0/0` receipt, timeout or missing sender is
`delivery UNKNOWN`, never success. Its own live source-identity check must
succeed in the starting pane; a detached process without that identity cannot
claim delivery. There is no DM fallback or source-identity bypass.

With Discord enabled, `notify_session.py` queues only problem and FINAL/stop
notes, including UNKNOWN and time-limit stops, not routine summaries. Neither
parent nor watcher relays already-posted reports. Without the option, all
existing inbox/native relay behaviour is unchanged.

A failed send keeps the pending event and lease for parent investigation. Use the
same config for an explicitly authorised retry; UNKNOWN may mean sent but not
verified, so check before retrying. A verified send receipt is saved before the
inbox write so an inbox-only retry does not repost to Discord. A process killed
between sending and recording that receipt still has an uncertain outcome.

Tests inject `KANBAN_WATCH_DISCORD_SENDER` with an absolute fake executable; it is
never set from card text. They exercise its `--lint-only` mode without a real
send. Set `KANBAN_WATCH_FORMAT_LINT_DIR` to a checkout of `astra-context-layer`
to also run its real `format-lint --check --text-file` on each fake-send draft.
No real Discord or live inbox is used by tests.

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
owns that policy. The helper owns configured Discord reports; without that
option the starting session owns the human relay. This feature never creates review cards,
runs CI, merges, updates memory or dispatches work. The note is a separate change;
if not yet available, its load is UNKNOWN.

The legacy Workflow and `kanban_live.py` tools remain available only on explicit
request. Their old per-row agents, session-store reads and options are NOT part
of the default board-only watcher. Tests use disposable homes, real temporary SQL
and fake tmux/systemd/Hermes commands; no live profile, model or inbox is exercised.
