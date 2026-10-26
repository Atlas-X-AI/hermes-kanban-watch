# Security policy

## Report privately

Do not put vulnerabilities, credentials or private worker output in public
issues. Use [GitHub private vulnerability reporting](https://github.com/Atlas-X-AI/hermes-kanban-watch/security/advisories/new)
for this repository. Include the affected version, a minimal sanitised
reproduction, the impact and any proposed fix. Do not send real keys or private
board databases. There is no promised response-time SLA.

## Data and authority boundaries

Every watcher is read-only with respect to Hermes boards, cards and worker
sessions. It never claims, dispatches, comments, repairs, approves or changes
permissions. The default `board_watch.py` opens only the board, in SQLite
mode=ro/query_only, never a worker profile or session store. Watcher leases,
cursors and pending receipts are local state, not board mutations.
`notify_session.py` may insert one explicit exact-session inbox row and reads it
back; this is its only external write. It never types directly into tmux and
refuses missing/ambiguous session targets. Native parent stdout is not proven
delivery. With `--discord-channel`, `watcher_tick.py` additionally delegates
bounded, counts-only reports to the existing lint-gated/read-back messaging
sender. No card text enters the Discord draft. Sender failure, timeout or absent
verified receipt is UNKNOWN; tests inject a fake executable and never post.
The legacy reader's `follow` and `needs` save only local progress
cursors under `~/.cache/hermes-kanban-watch/live/`.

The plugin never reads `.env`, `auth.json` or other credential files and holds no
credentials. Doctor selects only `model.provider` and `model.default` from profile
configs; it does not use credential fields. Unlike the live reader, doctor does
not open worker sessions. Do not put secrets in routes.

Doctor without flags is a setup check. `--fix` explicitly opts into creating a
missing default board with `hermes kanban --board default init` or a missing
named board with `hermes kanban boards create <slug>`, plus user routes and a
local `hermes-kanban-live` reader shim, without overwriting existing files.
Doctor checks the resolved command's `--version` against the installed plugin
identity (`hermes-kanban-watch 0.4.1`), not merely its presence on PATH. Foreign
or stale commands fail with their path shown; their output is omitted and
`--fix` never replaces them. This identity check is not a cryptographic trust
check: only execute commands from trusted PATH directories.
`--probe` explicitly invokes Hermes with a PONG
request and may consume model capacity; Hermes handles its own authentication.
Neither option gives watchers write authority.

Worker session text and tool results may themselves contain sensitive material.
They are displayed for the local operator, never treated as trusted instructions.
Read-only does not mean redacted: keep your watch private and sanitise screenshots
and reports. Claude Code watchers use your Claude model access, so content they
read is handled by that service under your configuration. The doctor does not
echo arbitrary subprocess output; this is not a general transcript redactor.

Compatibility outside the [supported target](README.md#supported-target) is
UNKNOWN. Do not interpret a green setup check as a security audit or proof of
upstream schema compatibility.
