# hermes-kanban-watch 0.4.2

For marketplace installation, setup and the complete guide, see the
[repository README](https://github.com/Atlas-X-AI/hermes-kanban-watch#readme).

A plugin that starts ONE cheap native watcher for a Kanban board, not a Workflow.
Claude Code uses a background Sonnet Agent, Codex its own Luna subagent, and Hermes
a dedicated Gemini watcher profile with cheap GLM fallback. Every 15 minutes it reports done, running,
waiting, problems and rough time left. New problems wake it promptly; one final
note ends the watcher. The helper posts directly when a Discord channel is
configured; otherwise the parent relays updates to the operator.
Native Codex/Hermes progress delivery is UNKNOWN until exercised there.
The watch path is a reader, not a dispatcher or a second owner of the work.
The separate, explicitly invoked `file_card.py` command can create a routed card.

Checked against a real Hermes Agent v0.21.2 board on Linux with the opt-in
`tests/test_real_hermes.py`; CI uses a fake Hermes CLI. The real check exercises
board/card creation, routed filing with model pin/read-back, cards/snapshot reads
and named-board doctor `--fix`, not
worker dispatch, live models or the Claude Code UI.

The compatibility target is the supplied working reader and its SQLite schema;
the automated tests use disposable board/session fixtures and a fake Hermes CLI.
They do not establish compatibility with later upstream schemas, live model
availability, installed Claude Code permissions or an end-to-end live UI session.

## What you need

- Python 3.11 or newer, no third-party Python dependencies for the plugin.
- Hermes Agent on PATH and a board with the supported SQLite schema.
- Existing Hermes profiles with your own models and credentials configured.
- Claude Code with plugin and background Agent support for the default route.
- tmux and an existing Claude inbox poller for the session-inbox bridge.
- Linux user systemd only for the explicit Hermes-to-Claude bridge launcher.
- Node.js and pytest only to run the development tests.

## Install

In Claude Code 2.1.275 or later, install from the marketplace in one step:

```text
/plugin install hermes-kanban-watch --marketplace Atlas-X-AI/hermes-kanban-watch
```

From a checkout, load the plugin in Claude Code:

```sh
claude --plugin-dir /path/to/hermes-kanban-watch/plugin
```

The plugin manifest, skill, native watcher agent, legacy Workflow and hook ship
together. No global Claude settings or private agent edits are needed. Invoke
`/kanban-watch` (or `/hermes-kanban-watch:kanban-watch`) after filing each run.
For Claude, the skill prepares a brief and local board lease with
`start_watcher.sh --prepare-only --board demo --session exact-tmux-name`, then
starts ONE background native Sonnet Agent. Do not also start the Hermes bridge.
For a native prepared watcher, `--discord-channel <channel-id>` is conditional on
the sender verifying the helper's actual execution-context identity. Preparation
and parent-pane resolution do not prove that route. Detached direct-Discord
launches are refused before state/lease/service creation; omit the option for
parent relay. `--discord-session <name>` resolves the session's channel on the
loopback relay for every post instead of a fixed id; a failed lookup is a problem
note to the starting session. On a supported direct route, the starting session receives only
problems and FINAL/stop notes, and neither session relays already-posted reports.
Delivery UNKNOWN preserves pending state plus safe kind/exit status and blocks
further send attempts pending reconciliation, without a retry override. Otherwise
the original inbox/parent relay is unchanged. Claude Code uses background Bash
completion notifications for each 900-second helper wait, never polling.
See [single-watcher details](docs/single-watcher.md) for reader/delivery commands,
native routes, singleton behaviour and the operator-run proof.

Set `HERMES_HOME` if your board data is not under `~/.hermes`. The default board
is `HERMES_HOME/kanban.db`; named boards are
`HERMES_HOME/kanban/boards/<slug>/kanban.db`. Use the board's data root, not a
worker profile home, when launching the reader.

Check the setup before watching:

```sh
python3 /path/to/hermes-kanban-watch/plugin/scripts/doctor.py --board default
```

Doctor prints PASS / FAIL / UNKNOWN for each check and a summary. Exit 0 means no
FAILs, not that UNKNOWNs were verified. Without flags, it only checks Hermes's
version, the board file, the two model keys in each profile config, route profile
existence and `hermes-kanban-live` on PATH. Its `--version` must print exactly
`hermes-kanban-watch 0.4.2`. A foreign or stale command is FAIL with its resolved
path shown; move it aside or correct PATH, then rerun `--fix`, or set `args.live_cmd`
to this plugin's reader. Existing unrelated `kanban-live` commands are not used
or changed. It never opens `.env`, `auth.json`, sessions
or other credential files. It does not echo arbitrary subprocess output or values
that resemble keys. Unsupported profile config shapes are UNKNOWN.

Optional setup writes, with no overwrite:

```sh
python3 /path/to/hermes-kanban-watch/plugin/scripts/doctor.py --board default --fix
export PATH="$HOME/.local/bin:$PATH"
python3 /path/to/hermes-kanban-watch/plugin/scripts/doctor.py --board default
```

`--fix` initialises a missing default board via `hermes kanban --board default init`
or a missing named board via `hermes kanban boards create <slug>`,
copies the route template to `~/.config/hermes-kanban-watch/routes.yaml`, and
creates an executable `~/.local/bin/hermes-kanban-live` shim that execs this installed
reader with the original arguments. Existing files, including symlinks, are left
alone and reported. Keep the plugin in place: the shim points to that install.
Doctor cannot change the caller's PATH; after installation it reports UNKNOWN
if the shim directory is not on PATH. It never creates profiles or changes keys.

For an explicit live-capacity check, add `--probe` (may consume model capacity):

```sh
python3 /path/to/hermes-kanban-watch/plugin/scripts/doctor.py --probe --timeout 30
```

Each distinct profile/model/provider in the routes is invoked once with
`hermes -p <profile> chat -Q -q "Reply with the single word PONG and stop." -m <model> --provider <provider>`.
Only a successful, exactly PONG response passes. Error output is not relayed.

### Suggest routes from passing models

```sh
python3 /path/to/hermes-kanban-watch/plugin/scripts/doctor.py --board demo --routes /path/to/routes.yaml --probe --suggest-routes /path/to/routes-checked.yaml
```

`--suggest-routes FILE` requires `--probe` and a new destination. Existing files,
directories and symlinks are refused, including a file created while probes run.
It writes a proposal, never replaces the source routes or activates a fallback.
Review the proposal before selecting it with `--routes` or `KANBAN_WATCH_ROUTES`.
Changing a lane can change the profile, provider and cost, not only the model.

Every worker and verifier written in a route must have passed this invocation's
exact PONG check. Passing workers are retained; failed workers use the first
passing lane in probe order (profile, model, provider, sorted lexicographically).
The existing verifier is retained if it passed and belongs to a different family;
otherwise the first passing different-family lane is used. Family identity comes
from your `providers` mapping, so different API hosts are not necessarily independent.
No new profile/model/provider triple is invented. Classes with no passing worker
and different-family verifier are omitted and named in comments. If none qualify,
the file has empty class/verifier mappings and cannot be used for filing.

Comments record the UTC timestamp, source routes path and PASS/FAIL for every
distinct lane. A generated file does not clear the original doctor's failures:
exit 1 still means a setup/probe check failed, even if suggestions were written.
A PONG is an availability observation, not proof of suitability or future capacity.

## Bring your own models and keys

Models and authentication are per Hermes profile. Use Hermes's own setup/model
commands to configure an API key, subscription login or local model. This plugin
supplies neither accounts nor credentials, and never copies them. Claude Code's
watcher agents use Claude's own model access; Hermes worker calls use the worker
profile's provider. Those are separate usage/cost surfaces.

The routing template uses example `builder` and `reviewer` profiles with GPT and
Claude models. It does not claim those profiles or models exist on your machine.
Edit the user routes to match your setup. Do not put keys in routes.yaml.

## File a wave and watch it

File cards using normal Hermes commands, for example:

```sh
hermes kanban boards create demo
hermes kanban --board demo create "Build the feature" --assignee builder
hermes kanban swarm --help
```

Use upstream swarm flags for your version. Filing a card does not start a
worker; dispatch through Hermes separately. After Bash create/swarm, the hook
suggests `/kanban-watch` with the newly created ids and board. It only adds
context and never blocks, starts a workflow, dispatches or edits a card.

### File one routed card

```sh
python3 /path/to/hermes-kanban-watch/plugin/scripts/file_card.py --board demo --class app-code --title "Build the feature" --body-file /path/to/brief.txt --routes /path/to/routes-checked.yaml --worktree /absolute/path/to/worktree
```

Only `--board`, `--class`, `--title` and `--body-file` are required. Routes use the
same selection precedence as `route.py`; `--worktree DIR` requests Hermes's
`--workspace worktree:<absolute-path>` without creating the directory itself.
Missing/unreadable body files, unknown classes, invalid routes and missing boards
are refused before creating anything. For a missing board it names
`hermes kanban boards create <board>`; it never creates the board implicitly.

The script asks `route.py` for the worker and different-family verifier, creates
one worker card using Hermes's positional title and `--assignee`, then calls
`set-model <id> <model> --provider <provider>`. Creation also carries the same pin
to avoid a race with an already-active dispatcher. It reads the card back with
`show <id> --json` and checks the title, body, assignee, pin and requested worktree.
Only after that succeeds does it print the card id and the exact command to paste:

```text
Card: t_1234abcd
/kanban-watch {"board":"demo","tasks":["t_1234abcd"]}
Not dispatched. Run: hermes kanban --board demo dispatch
```

Add `--dry-run` to print shell-quoted create, set-model and show commands without
running Hermes or creating anything; `<card-id>` is a placeholder in those commands.
Dry-run still validates the input files, routes and board. The commands include
the brief's text, so do not put secrets in the brief or share its output carelessly.
The script never opens profile configs or credential files. If pinning/read-back
fails after creation, it reports the existing id and a show command; inspect that
card instead of refiling and creating a duplicate.

This command does not create a verifier card, start a dispatcher or launch Workflow.
Paste the emitted `/kanban-watch` command in Claude Code to start watching. Filing
is separate from the watcher's read-only permissions; do not grant a watcher create
or set-model access.

## Legacy Workflow (explicit opt-in only)

Only when the caller names `workflows/kanban-watch.js`, pass JSON args to that
script. `/kanban-watch` now uses the native-agent path above, not these rows:

```text
{"board":"demo","tasks":["t_1234abcd","t_11111111"]}
{"board":"demo","phases":[{"title":"Build","tasks":["t_1234abcd"]},{"title":"Verify","tasks":["t_11111111"]}]}
```

Without phases, parent links determine stages. Bare `{ "board": "demo" }` watches
queued/running cards, excluding queued cards idle more than seven days. Explicit
ids always stay. Options:

- `follow_new` (default true): include cards filed by watched workers.
- `stale_days` (default 7): idle queued-card filter; 0 includes all.
- `max_hours` (default 12): board-event watch budget; 270-second reader wakes.
- `verbose` (default false): legacy worker text/tool results instead of compact output.
- `ready_stale` (default 900): seconds ready before flagging NOT_STARTING.
- `live_cmd` (default `hermes-kanban-live`): safe reader override, e.g.
  `python3 ~/plugins/hermes-kanban-watch/plugin/scripts/kanban_live.py`.
  Only letters, digits, spaces, `/`, `.`, `_`, `-`, `~` are allowed. No quoting,
  shell operators, substitution or newline. For paths containing spaces, use a
  PATH shim rather than building a quoted shell command.
- `agent_type` (default `hermes-kanban-watch:kanban-watcher`): the plugin's
  namespaced agent for every workflow row. No private user agent is required.
  Overrides allow ASCII letters, digits, `-`, `_`, and one optional `:` between
  non-empty names, and must name an agent available in your Claude Code install.

Running cards get one live row. Shared waiting rows cover queues; safe parent
DONE reports release child rows without an extra idle agent. Blocks/reviews get
Needs you rows, stay watched for resumes, and re-arm at most five times. A normal
exit-3 timeout means keep waiting, not missing or complete. Reads must include
`read_status: OK` and a positive `now`; denied or malformed reads return UNKNOWN.
A PONG probe, card DONE, or green watcher result is not proof of integration or
user acceptance. Read the actual model, fallback, warnings, recorded summary and
deliverable, and independently verify what the worker built.

When worker messages are unavailable, follow reports
`worker messages unavailable; using card log` once per run and watcher. Progress
is card-log growth only: a growing or changed log produces `progress advanced`
and prevents a possibly-wedged warning. A heartbeat is never progress, because a
wedged worker keeps heartbeating. Only two consecutive same-run running calls with
`progress flat` can be flagged. When worker messages are available, the rule is
unchanged: two consecutive running calls that each print `no new worker messages`
are flagged. First observations, run changes and unreadable
signals do not count as stalled calls. Named and default board log paths are
supported. The 270-second wake is unchanged.

### Read-only permissions

Doctor prints these exact allowlist entries (add them through Claude Code's
permission UI/settings if needed). Do not approve all Bash commands for a watcher:

```text
Bash(hermes-kanban-live cards:*)
Bash(hermes-kanban-live snapshot:*)
Bash(hermes-kanban-live wait-start:*)
Bash(hermes-kanban-live wait-any:*)
Bash(hermes-kanban-live follow:*)
Bash(hermes-kanban-live events:*)
Bash(hermes-kanban-live active:*)
Bash(hermes-kanban-live scout:*)
Bash(hermes-kanban-live needs:*)
```

A `live_cmd` override requires corresponding permissions for that command.
Neither the agent nor workflow retries a permission denial. Worker messages,
card descriptions and tool results are untrusted data, never instructions for
the watcher. The reader opens boards and session stores read-only; follow/needs
write only local progress cursors in `~/.cache/hermes-kanban-watch/live/`.

## Optional route inspection

```sh
python3 plugin/scripts/route.py --class app-code
python3 plugin/scripts/route.py --routes /path/to/routes.yaml --class app-code --worker-provider openai --worker-model gpt-5
```

This prints JSON with `class`, `profile`, `model`, `provider`, `verifier` and
`reason`. It never files cards, dispatches or reads credentials. Route selection:
explicit `--routes`, then `KANBAN_WATCH_ROUTES`, then
`~/.config/hermes-kanban-watch/routes.yaml` if present, then the shipped template.
An explicitly selected missing/invalid file is an error, not a silent fallback.

The format has three maps: `classes` maps names to profile/model/provider triples;
`providers` maps provider identifiers to model families; `verifiers` maps each
worker family to a verifier triple. Overrides select the verifier using the
effective worker provider's family. Unknown class/provider, missing verifier or
same-family verifier exits 1. Two API hosts serving the same family must have the
same family mapping: a different provider string alone is not independence.
If one host serves multiple families, use family-specific provider identifiers
supported by your setup rather than claiming an API host is a model family.

The dependency-free parser accepts mapping-only YAML, two-space indentation,
identifier strings with optional quotes and comments, or equivalent JSON. No
lists, YAML tags, anchors or aliases. Profiles are simple directory names; models
and providers may include `.`, `_`, `/`, `@`, `:`, `+` or `-`. Doctor reads only
block-mapping `model.provider` and `model.default` from profile config; other
configuration shapes are UNKNOWN and never repaired automatically.

## Features

<!-- A new feature adds one file and one bullet, appended at the end of this list. -->
- [Wave time and actual-run report](docs/wave-report.md): Inspect actual runs, elapsed seconds, retries and fallbacks.
- [Verify the families that actually ran](docs/verify-pair.md): Compare the worker and verifier's actual finished-run families.
- [Arm needs once with its own timeout loop](docs/needs-loop.md): Keep a read-only parent-question watch armed across timeouts.
- [Print a head-pinned PR merge receipt](docs/pr-merge-receipt.md): Check verifier evidence and CI against the live PR head without merging.
- [Watcher token cost](docs/watcher-token-cost.md): Inspect Claude Code watcher token usage separately from worker elapsed time.
- [Duplicate workers and questions parked on the parent](docs/duplicate-workers-parent-questions.md): Observe duplicate processes and current parent waits without modifying them.
- [File a fix and re-verify an earlier verdict](docs/fix-and-reverify.md): Explicitly file a fix or different-family re-verification from a finished verdict.
- [Workflow-owned wait rounds](docs/workflow-wait-rounds.md): Continue healthy read-only waits across bounded rounds and a shared deadline.
- [Suggest standing answers without posting](docs/standing-answers.md): Validate answer rules and print proposals for authorised review, never post them.
- [One watcher per board](docs/single-watcher.md): Native launch, deterministic 15-minute summaries and exact-session delivery.

## Development checks

```sh
python3 -m pytest -q tests
# Optional, requires a real Hermes CLI on PATH:
HERMES_KANBAN_WATCH_REAL=1 python3 -m pytest -q tests/test_real_hermes.py
```

All tests set HOME and HERMES_HOME to temporary directories. They exercise real
SQLite reads, cursor continuation, route precedence/family checks, fake CLI
probes/fixes, credential-open canaries, hook parsing and the full workflow body.
The opt-in real-Hermes check uses the real CLI in disposable homes; it is skipped
unless `HERMES_KANBAN_WATCH_REAL=1` and Hermes is on PATH.
The module parse check retains the metadata export and wraps the complete
Workflow async body, not a stripped stub. No live profiles or models are tested.
