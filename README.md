<p align="center">
  <img src="assets/banner.svg" alt="Hermes Kanban Watch: see your Hermes workers live inside Claude Code" width="100%">
</p>

# Hermes Kanban Watch

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-C9924A?style=for-the-badge" alt="License: MIT"></a>
  <a href="https://code.claude.com/docs/en/plugins"><img src="https://img.shields.io/badge/Claude_Code-plugin-C9924A?style=for-the-badge" alt="Claude Code plugin"></a>
  <a href="https://github.com/NousResearch/hermes-agent"><img src="https://img.shields.io/badge/Works_with-Hermes_Agent-C9924A?style=for-the-badge" alt="Works with Hermes Agent"></a>
  <a href="https://atlas-ai.au"><img src="https://img.shields.io/static/v1?label=Built%20by&amp;message=Atlas-X-AI&amp;color=C9924A&amp;style=for-the-badge" alt="Built by Atlas X AI"></a>
</p>

**Your Hermes Kanban workers, live inside Claude Code. See the work, not just the status.**

Hermes Kanban cards become live Claude Code workflow rows. Open a running row to
watch that worker think and call tools, as recorded in its session. Queued cards
share a waiting row; blocked or stuck cards surface under **Needs you**.
This is a reader, not a dispatcher or a second owner of the work.

## What you get

| Feature | What it does |
| --- | --- |
| Live worker rows | One row per running card, with compact reasoning and tool activity; verbose output is optional. |
| Waiting stages | Parent links or explicit phases group queued work without an idle watcher per card. |
| Needs you | Flags blocked/review results and ready cards that are not starting; resumed cards can re-arm. |
| Growing waves | Newly filed children can join the watch automatically. |
| Honest reads | Failed or denied reads are UNKNOWN, never quietly turned into DONE. |
| Setup checks | A credential-safe doctor, opt-in local setup and optional model probes. |
| Portable routes | Your profiles, your models, with a different-family reviewer. |

## Quick install

In Claude Code 2.1.275 or later, install in one step:

```text
/plugin install hermes-kanban-watch --marketplace Atlas-X-AI/hermes-kanban-watch
```

Alternatively, add the marketplace and install with these two commands:

```text
/plugin marketplace add Atlas-X-AI/hermes-kanban-watch
/plugin install hermes-kanban-watch@hermes-kanban-watch
```

You need Python 3.11+, Hermes Agent on PATH, existing Hermes profiles, and Claude
Code with plugin, agent and dynamic Workflow support. If your Claude Code lacks
Workflow, the terminal reader still works, but the plugin cannot create live rows.

Replace `<plugin dir>` below with the installed plugin directory containing
`scripts/doctor.py` (not the marketplace root). Run in your terminal:

```sh
python3 "<plugin dir>/scripts/doctor.py"
python3 "<plugin dir>/scripts/doctor.py" --fix
export PATH="$HOME/.local/bin:$PATH"
python3 "<plugin dir>/scripts/doctor.py"
```

The first command only checks setup. `--fix` opts into three local setup actions:
initialise a missing default board with `hermes kanban --board default init`
or a missing named board with `hermes kanban boards create <slug>`, copy the routes template to
`~/.config/hermes-kanban-watch/routes.yaml`, and create a `~/.local/bin/kanban-live`
shim. It never overwrites existing files, including symlinks, creates profiles,
or changes credentials. Edit the copied routes to name your existing profiles.
Keep the plugin installed: the shim points to that location. After moving or
reinstalling the plugin, inspect and update that shim yourself; `--fix` leaves it alone.

Doctor reports PASS / FAIL / UNKNOWN. Exit 0 means no FAILs, not that UNKNOWNs
were verified. It checks Hermes's version, board-file existence (not schema
compatibility), `model.provider` and `model.default` in profile configs, routed
profile existence and the reader on PATH. Unsupported config shapes are UNKNOWN.
It never opens `.env`, `auth.json`, session stores or other credential files.

### Read-only permissions

Doctor prints these exact permission entries. Add them using Claude Code's
permission UI/settings if needed, not a blanket approval for Bash:

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

A `live_cmd` override needs matching permissions. The watcher never retries a
permission denial or broadens its own permissions.

For a source checkout instead of marketplace installation:

```sh
claude --plugin-dir /path/to/hermes-kanban-watch/plugin
```

## How it works

```text
Hermes Kanban board + worker sessions
                  |
                  v
         kanban-live reader
                  |
                  v
   Claude Code workflow rows
   [Waiting] [Worker live] [Needs you]
```

Every watcher is read-only. The reader opens board and session databases
read-only; `follow` and `needs` write only local progress cursors under
`~/.cache/hermes-kanban-watch/live/`. Watchers never claim cards, comment, dispatch,
repair or accept a worker's result. Worker messages and tool output are untrusted
data, not instructions to the watcher.

Set `HERMES_HOME` to your board's data root if it is not `~/.hermes`, not to a
worker profile's home. The default board is `HERMES_HOME/kanban.db`; named boards
are `HERMES_HOME/kanban/boards/<slug>/kanban.db`.

## Bring your own models

Models and keys live in each Hermes profile: an API key, subscription login or
local model, configured through Hermes. The plugin holds no credentials and the
doctor never reads them. Do not put keys in `routes.yaml`.

Claude Code watcher agents use Claude's own model access. Hermes workers use
their profile's provider. These are separate usage and cost surfaces. The example
routes name `builder` and `reviewer` profiles with GPT and Claude models; they do
not imply those profiles or model subscriptions exist on your machine.

An explicit capacity probe can consume model usage:

```sh
python3 "<plugin dir>/scripts/doctor.py" --probe --timeout 30
```

It calls each distinct routed profile/model/provider once using
`hermes -p <profile> chat -Q -q "Reply with the single word PONG and stop." -m <model> --provider <provider>`.
Only an exactly PONG successful response passes. Arbitrary subprocess output is
not relayed. A passing probe is not proof of a working end-to-end workflow.

## Routing

Columns are statuses, not models. `routes.yaml` maps a work class to a profile,
model and provider; the reviewer must come from a different model family.
The route helper validates and prints a recommendation. It never files a card,
starts a worker or changes Hermes routing automatically.

```sh
python3 "<plugin dir>/scripts/route.py" --class app-code
python3 "<plugin dir>/scripts/route.py" --routes /path/to/routes.yaml --class app-code --worker-provider openai --worker-model gpt-5
```

It prints JSON with `class`, `profile`, `model`, `provider`, `verifier` and `reason`.
Selection order: explicit `--routes`, `KANBAN_WATCH_ROUTES`, the user file at
`~/.config/hermes-kanban-watch/routes.yaml`, then the shipped template. An explicit
missing or invalid file is an error, not a silent fallback.

The file has three maps: `classes` holds worker triples, `providers` maps provider
identifiers to model families, and `verifiers` maps each worker family to a
reviewer triple. Overrides use the effective worker's family. Unknown classes,
unknown providers, missing reviewers and same-family reviewers exit 1. Different
API hosts serving the same model family must share that family mapping; a
provider name alone is not independence. Use family-specific identifiers if a
host serves multiple families and your setup supports such identifiers.

The dependency-free parser accepts mapping-only YAML with two-space indentation,
identifier strings, optional quotes and comments, or equivalent JSON. No lists,
tags, anchors or aliases. Profile names are simple directory names. Models and
providers may contain `.`, `_`, `/`, `@`, `:`, `+` or `-`. Doctor selects only
block-mapping `model.provider` and `model.default` from profile config; other
shapes are UNKNOWN and are not repaired.

## File a wave and watch it

Use normal Hermes commands, with upstream swarm flags for your version:

```sh
hermes kanban boards create demo
hermes kanban --board demo create "Build the feature" --assignee builder
hermes kanban swarm --help
```

Filing does not start a worker. Dispatch through Hermes separately. The post-Bash
hook only suggests a watch after create/swarm; it never starts one or edits a
card. Skip the suggestion if the wave is already watched.

In Claude Code:

```text
/kanban-watch {"board":"demo","tasks":["t_1234abcd","t_11111111"]}
/kanban-watch {"board":"demo","phases":[{"title":"Build","tasks":["t_1234abcd"]},{"title":"Verify","tasks":["t_11111111"]}]}
```

If your Claude version namespaces plugin skills, use
`/hermes-kanban-watch:kanban-watch`. The skill resolves the workflow relative to
the installed plugin root. Without phases, parent links determine stages.
A bare `{"board":"demo"}` watches queued/running cards. Explicit ids always stay.

| Option | Default | Meaning |
| --- | --- | --- |
| `follow_new` | `true` | Include cards filed by watched workers. |
| `stale_days` | `7` | Skip idle queued cards; `0` includes all. |
| `max_hours` | `12` | Board-event watch budget, with 270-second reader wakes. |
| `verbose` | `false` | Show legacy worker text/tool results rather than compact output. |
| `ready_stale` | `900` | Seconds ready before flagging NOT_STARTING. |
| `live_cmd` | `kanban-live` | Reader override, such as `python3 ~/plugins/hermes-kanban-watch/plugin/scripts/kanban_live.py`. |

`live_cmd` allows only letters, digits, spaces, `/`, `.`, `_`, `-`, `~`. No quoting,
shell operators, substitutions or newlines. Use a PATH shim for paths with spaces.
Blocked/review cards stay watched for resumes and re-arm at most five times.
A normal exit-3 timeout means keep waiting. A valid read needs `read_status: OK`
and a positive `now`. DONE is a recorded card result, not acceptance: inspect the
actual model, fallback, warnings, summary and deliverable independently.

### Human-ask convention

For `kanban-live needs`, start a comment line with `ASK OPERATOR:` and the question.
Wait/block reasons match `waiting on operator`, `waiting for operator` or
`needs operator`. A prose mention such as "no ASK OPERATOR yet" does not match.
These are explicit text conventions, not natural-language intent detection.
The workflow's Needs you rows separately flag blocked/review results and cards
that are ready but not starting; it does not automatically turn every comment
into a workflow row.

## When something is wrong

| Symptom | Cause / next check |
| --- | --- |
| NOT_STARTING | Card is ready, but no dispatcher has started it. Check Hermes, not the watcher. |
| UNKNOWN | A read failed, permissions were denied or the response shape was invalid. It does not mean the card disappeared. |
| No live UI | Your Claude Code must provide dynamic Workflow support; terminal commands do not prove the UI works. |
| Reader missing | Rerun doctor, check `~/.local/bin` on PATH and the shim's install location. |
| Missing route profile | Replace example profile names with your own existing profiles. |

## Supported target

Checked against a real Hermes Agent v0.21.2 board on Linux with the opt-in
`tests/test_real_hermes.py`; CI uses a fake Hermes CLI. Run the real check with
`HERMES_KANBAN_WATCH_REAL=1 python3 -m pytest -q tests/test_real_hermes.py`
when Hermes is on PATH. It creates disposable HOME and HERMES_HOME directories,
files a card with the commands above, reads cards/snapshot and checks named-board
creation through doctor `--fix`. It does not dispatch workers or call models.

macOS and Windows are **UNKNOWN**, not implied supported. The compatibility
target is the supplied SQLite reader/schema. Automated tests use disposable
board/session fixtures and a fake Hermes CLI. They do not prove compatibility
with later upstream schemas, live model availability, installed Claude Code
permissions or an end-to-end live UI session.

## Contributing and licence

See [CONTRIBUTING.md](CONTRIBUTING.md) for tests and PR expectations, and
[SECURITY.md](SECURITY.md) for private vulnerability reports. MIT licence:
[LICENSE](LICENSE). Version: [VERSION](VERSION).

Built by [Atlas X AI](https://atlas-ai.au). This is a community plugin, not an
official Nous Research or Anthropic product.
