# hermes-kanban-watch 0.1.0

For marketplace installation, setup and the complete guide, see the
[repository README](https://github.com/Atlas-X-AI/hermes-kanban-watch#readme).

A Claude Code plugin that shows Hermes Kanban cards as live workflow rows. One
row follows each running worker, queued cards share a waiting row per stage, and
blocked, review or otherwise unfinished results appear under Needs you. Newly
filed children can join the watch; blocked/review cards can resume and re-arm.
It is a reader, not a dispatcher or a second owner of the work.

Checked against a real Hermes Agent v0.21.2 board on Linux with the opt-in
`tests/test_real_hermes.py`; CI uses a fake Hermes CLI. The real check exercises
board/card creation, cards/snapshot reads and named-board doctor `--fix`, not
worker dispatch, live models or the Claude Code UI.

The compatibility target is the supplied working reader and its SQLite schema;
the automated tests use disposable board/session fixtures and a fake Hermes CLI.
They do not establish compatibility with later upstream schemas, live model
availability, installed Claude Code permissions or an end-to-end live UI session.

## What you need

- Python 3.11 or newer, no third-party Python dependencies for the plugin.
- Hermes Agent on PATH and a board with the supported SQLite schema.
- Existing Hermes profiles with your own models and credentials configured.
- Claude Code with plugin, agent and dynamic Workflow support. If Workflow is
  unavailable in your version, the reader still works in the terminal, but this
  plugin cannot invent a live workflow UI.
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

The plugin manifest, skill, watcher agent, workflow and hook are shipped together.
No changes to global Claude settings or private agent files are needed. Invoke
`/kanban-watch` (or the namespaced `/hermes-kanban-watch:kanban-watch` if your
Claude version namespaces plugin skills). The skill resolves the installed
workflow file relative to the plugin root and calls Workflow with that path.

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
existence and `kanban-live` on PATH. It never opens `.env`, `auth.json`, sessions
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
creates an executable `~/.local/bin/kanban-live` shim that execs this installed
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

In Claude Code, provide JSON args to the skill:

```text
/kanban-watch {"board":"demo","tasks":["t_1234abcd","t_11111111"]}
/kanban-watch {"board":"demo","phases":[{"title":"Build","tasks":["t_1234abcd"]},{"title":"Verify","tasks":["t_11111111"]}]}
```

Without phases, parent links determine stages. Bare `{ "board": "demo" }` watches
queued/running cards, excluding queued cards idle more than seven days. Explicit
ids always stay. Options:

- `follow_new` (default true): include cards filed by watched workers.
- `stale_days` (default 7): idle queued-card filter; 0 includes all.
- `max_hours` (default 12): board-event watch budget; 270-second reader wakes.
- `verbose` (default false): legacy worker text/tool results instead of compact output.
- `ready_stale` (default 900): seconds ready before flagging NOT_STARTING.
- `live_cmd` (default `kanban-live`): safe reader override, e.g.
  `python3 ~/plugins/hermes-kanban-watch/plugin/scripts/kanban_live.py`.
  Only letters, digits, spaces, `/`, `.`, `_`, `-`, `~` are allowed. No quoting,
  shell operators, substitution or newline. For paths containing spaces, use a
  PATH shim rather than building a quoted shell command.

Running cards get one live row. Shared waiting rows cover queues; safe parent
DONE reports release child rows without an extra idle agent. Blocks/reviews get
Needs you rows, stay watched for resumes, and re-arm at most five times. A normal
exit-3 timeout means keep waiting, not missing or complete. Reads must include
`read_status: OK` and a positive `now`; denied or malformed reads return UNKNOWN.
A PONG probe, card DONE, or green watcher result is not proof of integration or
user acceptance. Read the actual model, fallback, warnings, recorded summary and
deliverable, and independently verify what the worker built.

### Read-only permissions

Doctor prints these exact allowlist entries (add them through Claude Code's
permission UI/settings if needed). Do not approve all Bash commands for a watcher:

```text
Bash(kanban-live cards:*)
Bash(kanban-live snapshot:*)
Bash(kanban-live wait-start:*)
Bash(kanban-live wait-any:*)
Bash(kanban-live follow:*)
Bash(kanban-live events:*)
Bash(kanban-live active:*)
Bash(kanban-live scout:*)
Bash(kanban-live needs:*)
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
