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
and a `hermes-kanban-live` shim, without overwriting existing files.
Add `~/.local/bin` to PATH and rerun doctor.
Doctor requires `hermes-kanban-live --version` to print exactly
`hermes-kanban-watch 0.2.0`. A foreign/stale command is FAIL with its resolved path;
move it aside or correct PATH before rerunning `--fix`, which never overwrites it.
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

### File one routed, pinned card

Only on an explicit filing request: `python3 <plugin-root>/scripts/file_card.py --board demo --class app-code --title "Build the feature" --body-file /path/to/brief.txt --routes /path/to/routes.yaml --worktree /absolute/path/to/worktree`.
`--routes` and `--worktree` are optional. The command resolves a worker/different-family verifier via `route.py`, creates one pinned worker, runs `set-model`, verifies `show --json`, then prints its id and exact `/kanban-watch` args. It creates no verifier, dispatcher, Workflow or worktree and opens no profile configs or credentials. Never grant this authoring command to a watcher.
`--dry-run` validates files/routes/board and prints commands without running Hermes; the brief is printed, so keep secrets out. Missing boards/invalid routes are refused. After partial failure inspect the returned id, never blindly refile. See [full filing details](../../README.md#file-one-routed-card); launch a watch only if requested.

### Suggest routes after PONG checks

Run `python3 <plugin-root>/scripts/doctor.py --board demo --routes /path/to/routes.yaml --probe --suggest-routes /path/to/routes-checked.yaml` only with explicit capacity approval. It requires `--probe`, refuses existing destinations (including directories/symlinks), and never changes source routes or activates the proposal. Review profile/provider/cost changes before selecting it.
Both lanes must pass this run's PONG and have different declared families, not merely different hosts. Passing workers/verifiers are retained; replacements use lexicographic lane order. Classes without a pair are omitted with diagnostic comments; no pairs means unusable routes. Original failures still exit 1. PONG is not suitability or lasting capacity. See [selection and audit details](../../README.md#suggest-routes-from-passing-models).

## Launch

Treat `$ARGUMENTS` as a JSON args object. Resolve the installed plugin's
`workflows/kanban-watch.js`, then invoke
`Workflow({scriptPath: "<plugin-root>/workflows/kanban-watch.js", args: <args>})`.
If Workflow is unavailable, report that limitation and run `hermes-kanban-live` read-only
commands directly, never pretend live workflow rows were created.

Examples of args:
- `{"board":"default","tasks":["t_1234abcd"]}`
- `{"board":"demo","phases":[{"title":"Build","tasks":["t_1234abcd"]},{"title":"Verify","tasks":["t_11111111"]}]}`
- `{"board":"demo","follow_new":false,"stale_days":7,"max_hours":12}`

A bare board watch includes queued/running cards, skipping queued cards idle for
more than `stale_days` (7 by default). `stale_days: 0` disables filtering; explicit
ids always stay. Use `verbose: true` for legacy worker text and tool results.
The reader is `hermes-kanban-live` on PATH; `live_cmd` can override it with a safe command
such as `python3 ~/plugins/hermes-kanban-watch/plugin/scripts/kanban_live.py`.
Only letters, numbers, spaces, `/`, `.`, `_`, `-`, `~` are accepted. No shell operators.
Every row uses the plugin's `hermes-kanban-watch:kanban-watcher` agent by default;
no private user agent is needed. `agent_type` may name another available agent,
using ASCII letters, digits, `-`, `_` and one optional `:` between non-empty names.

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

For `hermes-kanban-live needs`, start a comment line with `ASK OPERATOR:` followed by the
question. A mention in prose such as "no ASK OPERATOR yet" is not an ask. Block or
dependency-wait reasons must say `waiting on operator`, `waiting for operator` or
`needs operator` to match a current human wait. These are explicit text conventions,
not natural-language intent detection. Needs you workflow rows separately flag
blocked/review results and cards that are ready but not starting.

### Report a wave's actual runs and elapsed time

Run `hermes-kanban-live report --board demo`, optionally repeating `--task T` or
adding `--since EPOCH`. Without task ids this includes all statuses, including
done/archived. `--since` selects card creation time; it intersects task selection
and does not clip individual runs. JSON has one object per card in `cards`, plus
`totals`, `read_status`, `now` and `missing`. Each card contains id/title/status,
assignee, actual last-run `model_used`, `provider_used`, `fallback_from`, `runs`,
`retried`, `duration_s` and last-run warning counts. Unknown run data stays null.

`totals.by_status` counts cards; `run_seconds` sums all known run durations of
selected cards (running runs measured at `now`); `fell_back` counts cards that
fell back on any attempt; `retried` counts cards with the conservative retry
guard (multiple runs or a retry/block-history event). These are elapsed seconds,
not billed token/currency costs. Add `--format text` for aligned plain-text
columns and totals, without Markdown. Exit 4 reports absent explicit ids; exit 5
is an unreadable board. A card merely excluded by `--since` is not missing.

### Check actual worker/verifier family independence

Run `hermes-kanban-live verify-pair --board demo --worker T --verifier V`, with
optional `--families FILE`. It uses each card's newest finished run (non-null
`ended_at`, newest run id), not a model pin or a newer unfinished attempt. JSON
includes each actual `model_used`, `provider_used`, `run_id`, mapped `family` and
`different_families`. The run's actual provider maps to its model family through
`providers` in the routes file. Keep same-family hosts mapped alike; multi-family
hosts need family-specific identifiers. This does not infer family from a model
prefix or prove that review work happened.

Lookup order matches `route.py`: explicit `--families`, `KANBAN_WATCH_ROUTES`,
`~/.config/hermes-kanban-watch/routes.yaml`, shipped template. Only the valid,
non-empty `providers` map is needed. Exit 0 means different families, 1 means
same family or invalid/unknown classification, 3 means no finished run for either
id, and 5 means unreadable board. Classification errors set `different_families`
to null and include `error`; never convert UNKNOWN to a successful check.
For direct Claude Code calls request only `Bash(hermes-kanban-live report:*)` and
`Bash(hermes-kanban-live verify-pair:*)`, not unrestricted Bash permission.

### Keep a parent-question watch armed across timeouts

Run `hermes-kanban-live needs --board demo --loop --created-by PARENT --cursor-key WAVE`
once, rather than rebuilding a command after each exit 3. In loop mode `--since`
is optional, captured once at startup when absent, and never advances across
timeout windows. Its first poll includes pre-existing waits still in force,
including a creator's older parent question. Answered/resumed/superseded old
waits stay excluded and existing seen-cursor deduplication still applies.

Loop mode sleeps at `--interval` and retries only normal timeouts, with no timeout
JSON churn. It exits 0 on the first non-empty items result, or fails on an error
without retrying it. `--max-seconds` bounds each window, not the command's lifetime;
cancel the caller to stop a quiet watch. Without `--loop`, the required `--since`,
exit-3 timeout and output behaviour are unchanged. This is still a reader, never
permission to answer, dispatch or mutate a card.
