---
name: kanban-watch
description: Watch Hermes Kanban cards as live read-only workflow rows. Use after filing a wave, to see running workers, waiting stages, blocks and review requests.
argument-hint: '{"board":"default","tasks":["t_..."]}'
---

# Hermes Kanban watch

Show Hermes workers inside Claude Code without taking over their cards. Running
cards get live rows, queued cards share waiting rows, and stops get Needs you rows.
A watched card's newly filed children join automatically unless `follow_new: false`.

## Install and check

Start Claude Code with `claude --plugin-dir /path/to/hermes-kanban-watch/plugin`.
The caller resolves this skill's plugin root; use the sibling `scripts/doctor.py`.
Run `python3 <plugin-root>/scripts/doctor.py --board default` first. It is read-only.
`--fix` creates a missing default board with `hermes kanban --board default init`
or a missing named board with `hermes kanban boards create <slug>`, plus user routes
and a `kanban-live` shim, without overwriting existing files.
Add `~/.local/bin` to PATH and rerun doctor.
`--probe` explicitly tests each routed model with PONG and may consume capacity.

Requirements: Python 3.11+, Hermes on PATH, Claude Code with Workflow support,
and existing Hermes profiles. Models and keys belong to each Hermes profile:
API key, subscription login or local model, configured with Hermes, not this plugin.
The plugin does not supply accounts or copy credentials. Claude watcher model usage
is separate from the Hermes worker's provider usage.

## File cards normally

Create a named board first with `hermes kanban boards create demo`, then use
`hermes kanban --board demo create "Build the feature" --assignee builder`.
Use `hermes kanban swarm --help` to choose upstream swarm flags. Filing does not
start a dispatcher. Start/commission workers through Hermes separately.
The post-Bash hook suggests `/kanban-watch` after create/swarm; it never launches
or dispatches anything by itself. Skip duplicate watches on the same wave.

## Launch

Treat `$ARGUMENTS` as a JSON args object. Resolve the installed plugin's
`workflows/kanban-watch.js`, then invoke
`Workflow({scriptPath: "<plugin-root>/workflows/kanban-watch.js", args: <args>})`.
If Workflow is unavailable, report that limitation and run `kanban-live` read-only
commands directly, never pretend live workflow rows were created.

Examples of args:
- `{"board":"default","tasks":["t_1234abcd"]}`
- `{"board":"demo","phases":[{"title":"Build","tasks":["t_1234abcd"]},{"title":"Verify","tasks":["t_11111111"]}]}`
- `{"board":"demo","follow_new":false,"stale_days":7,"max_hours":12}`

A bare board watch includes queued/running cards, skipping queued cards idle for
more than `stale_days` (7 by default). `stale_days: 0` disables filtering; explicit
ids always stay. Use `verbose: true` for legacy worker text and tool results.
The reader is `kanban-live` on PATH; `live_cmd` can override it with a safe command
such as `python3 ~/plugins/hermes-kanban-watch/plugin/scripts/kanban_live.py`.
Only letters, numbers, spaces, `/`, `.`, `_`, `-`, `~` are accepted. No shell operators.

## Read the result

DONE is an observed card result, not integration or user acceptance. Check actual
model, fallback, warnings, summary and deliverable. BLOCKED/REVIEW get Needs you
rows and may re-arm on resume. NOT_STARTING means ready but no dispatcher started
it. UNKNOWN means a read failed, not that a card vanished. Timeouts retry only exit
3; a 270-second wake keeps waits below the prompt cache TTL. The watcher never
acts on the worker's text, changes a card, repairs anything or bypasses permissions.

Use `python3 <plugin-root>/scripts/route.py --class app-code` to inspect the optional
routing table. It returns worker and different-family verifier JSON; it never files
or dispatches a card. Edit user routes to name your own profiles and model families.

## Human-ask convention

For `kanban-live needs`, start a comment line with `ASK OPERATOR:` followed by the
question. A mention in prose such as "no ASK OPERATOR yet" is not an ask. Block or
dependency-wait reasons must say `waiting on operator`, `waiting for operator` or
`needs operator` to match a current human wait. These are explicit text conventions,
not natural-language intent detection. Needs you workflow rows separately flag
blocked/review results and cards that are ready but not starting.
