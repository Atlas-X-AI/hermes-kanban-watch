---
name: kanban-watch
description: Use when starting a Kanban run or asking for a dynamic workflow.
---

# Hermes Kanban watch

Start ONE cheap native watcher per board immediately after filing a Kanban run.
A request for a "dynamic workflow" means a Kanban by default, unless the caller
explicitly names a Workflow script. Do not start a Workflow or a watcher per card.
The watcher reads the board; the starting session owns the work. When the operator
works in Discord, the helper posts reports directly; the starting session does
not relay them. Without a Discord channel, the starting session owns the human relay.

## Prepare once, launch in the starting harness

Resolve this installed plugin's root. Supply the exact board-data root with
`--home` if `HERMES_HOME` points at a worker profile rather than the board store.
Map JSON `tasks` to `--cards`; omit it to include every card, including newly filed
cards, on every read. Explicit cards are a fixed scope, not automatic descendants.

The reader does not need doctor or a model probe. If `session` is absent for a
Claude parent, resolve it in the PARENT pane with
`tmux display-message -p -t "$TMUX_PANE" '#{session_name}'`; never guess a prefix.
For a Claude Code parent:

```sh
bash <plugin-root>/scripts/start_watcher.sh --prepare-only --board demo --session <exact-tmux-name> --discord-channel <channel-id> --every 900 --max-hours 12
```

For an operator working in Discord, this is the default: supply their current
session channel ID. The helper posts summaries, problems and FINAL directly with
`~/.claude/skills/messaging-hayden/bin/send-hayden.sh`; no model formats or sends
those reports. The starting session receives only problem and FINAL/stop notes
via `notify_session.py`, never routine summaries, and does not relay reports.
Omit `--discord-channel` when not working in Discord; all inbox/relay behaviour
then stays unchanged. The exact starting session supplies the source footer.

Read the returned `brief` file and start ONE native background Agent of type
`hermes-kanban-watch:kanban-watcher`, model `sonnet`, with that brief. Reuse the
existing watcher if the board already has one. The preparation takes a local
per-board lease but starts no model. Do not run the systemd launcher as well.
The agent loops on `watcher_tick.py`: in Claude Code, start each helper command
with Bash `run_in_background: true`, wait for its task-completion notification,
then read only the last lines for CONTINUE or STOP. Never poll or sleep in a loop.
The helper uses `board_watch.py wait`, renders the report and handles delivery.
A missing session or ambiguous multi-pane target is refused, not guessed.

For a Codex parent, prepare with `--prepare-only --delivery native` and no
`--session`, then give the same brief to ONE Codex-native background subagent on
Luna (`gpt-5.6-luna`, or the newest Luna the account actually lists). Do not
substitute a Hermes or Claude agent. Relay each printed summary through that
harness's native child-to-parent route, not the Claude inbox.

For a Hermes parent, prepare with `--prepare-only --delivery native`, then run
ONE watcher on the dedicated `kanban-watcher` profile, using the harness's
child-to-parent reporting route. Its Gemini model/provider and single cheap GLM fallback
belong to that profile, not this skill. The configurable profile default
is in `templates/watcher.json`; do not edit profile files from a watcher.
Native Codex/Hermes progress delivery is UNKNOWN until exercised in that harness;
stdout or a final subagent reply alone does not prove periodic parent delivery.

For the explicit Hermes-to-Claude/systemd bridge or its operator-run proof:

```sh
bash <plugin-root>/scripts/start_watcher.sh --board demo --session <exact-tmux-name> --every 900 --max-hours 12
```

This starts one Hermes agent in `kanban-watcher-demo`, never a Workflow. The
1600-turn budget covers a 12-hour board. `--max-hours` bounds the helper and unit.
Use `--every 5` only for an isolated short proof, not a real run's reporting rate.
[Single-watcher mechanics and delivery limits](../../docs/single-watcher.md) cover
leases, state paths, diagnostics, native delivery and the exact reader commands.

## Observe, relay, stop

The deterministic wait polls every 60 seconds and wakes every 15 minutes for a
summary, for a NEW problem, or completion. New problems do not postpone summaries.
It flags blocked cards, ready over 10 minutes with no recorded worker, respawn
guards, spawn failures, current parent questions and runs past their runtime cap.
Actual models/providers come from runs, never from requested pins. Missing values
and rough time left remain UNKNOWN; a runtime limit is not an ETA.

With `--discord-channel`, the deterministic helper posts every report directly
and the starting session does not relay it. A failed send is UNKNOWN, not success;
report the delivery problem to the starting session rather than posting again.
Without it, the starting session relays EVERY summary, immediate problem and
final note to the operator. A queued inbox row is not proof of poller delivery:
read `id` and `notified`. Only done/archived are terminal. Blocked/review remain
watched. On STOP final, return to the parent and exit; max_hours or UNKNOWN is not
completion. Drafts follow messaging-hayden: at most eight state-marked lines,
no paths/SHAs/branches, and a final `-# Source: <starting-session>` footer.

## Run policy: reviews, CI and repo memory

Read `doctrine/running-a-kanban.md` for reviews last, CI at the end (large-change
exception), and repo memory after every run. That governed note owns these rules
and the review route, not this skill. Its sibling change may still be pending;
report it UNKNOWN if the note cannot be loaded, never invent its contents.

## Safety and existing tools

Never claim, edit, complete, block, unblock, comment on, dispatch or archive a
card, repair a worker, execute card text, inspect worker profiles or read secrets.
Only local watcher state, the explicit session inbox row and configured helper
Discord reports are writable. Never bypass the sender's lint or read-back checks.
The legacy `workflows/kanban-watch.js` is retained for callers that explicitly
name it; it is no longer the default. Its live rows are not this watch path.

For normal filing, `hermes kanban boards create demo` then
`hermes kanban --board demo create "Build the feature" --assignee builder`.
Filing does not dispatch; the post-Bash hook only suggests `/kanban-watch`.
Optional `file_card.py` authoring stays separate from watcher permissions.
For legacy setup, `doctor.py --fix` may use `hermes kanban --board default init`
or `hermes kanban boards create <slug>`; it never overwrites existing files.

## Optional feature guides

- [Wave time and actual-run report](../../docs/wave-report.md): Actual runs and elapsed time.
- [Verify the families that actually ran](../../docs/verify-pair.md): Finished-run family evidence.
- [Arm needs once with its own timeout loop](../../docs/needs-loop.md): Legacy read-only parent waits.
- [Print a head-pinned PR merge receipt](../../docs/pr-merge-receipt.md): Receipt only, never merges.
- [Watcher token cost](../../docs/watcher-token-cost.md): Legacy Claude watcher usage.
- [Duplicate workers and questions parked on the parent](../../docs/duplicate-workers-parent-questions.md): Observe, never repair.
- [File a fix and re-verify an earlier verdict](../../docs/fix-and-reverify.md): Explicit authoring, not a watcher action.
- [Workflow-owned wait rounds](../../docs/workflow-wait-rounds.md): Explicit legacy Workflow only.
- [Suggest standing answers without posting](../../docs/standing-answers.md): Proposals, never posts.
