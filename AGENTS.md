# AGENTS.md

Guidance for coding agents working in this repository.

## Layout

- `plugin/` is the Claude Code plugin that users install:
  - `.claude-plugin/plugin.json`: the plugin manifest.
  - `skills/kanban-watch/SKILL.md`: the user-facing skill.
  - `agents/kanban-watcher.md`: the read-only watcher agent.
  - `workflows/kanban-watch.js`: the watch workflow (`/kanban-watch`).
  - `hooks/`: the nudge hook that suggests `/kanban-watch` after cards are filed.
  - `scripts/`: `kanban_live.py` (board reader), `wave_status.py`, `route.py` (routing), `doctor.py` (setup check).
  - `templates/routes.yaml`: the default routing table.
- `.claude-plugin/marketplace.json` makes this repository installable as a plugin marketplace.
- `tests/` holds the pytest suite. It runs against temporary `HOME` and `HERMES_HOME` directories and never touches real ones.

## Running the tests

```sh
uv sync --dev
uv run pytest tests -v
```

or, without uv, `python3 -m pytest -q tests`.

## Rules that must hold

- Watchers are read-only. Nothing in the watch path may claim, complete, block, unblock, comment on, reassign, dispatch or archive a Kanban card.
- The plugin holds no credentials. `doctor.py` reads only `model.provider` and `model.default` from each profile's `config.yaml`, and never opens `.env`, `auth.json` or any other credential file.
- No machine-specific paths. Hermes data is found from `HERMES_HOME` (default `~/.hermes`). The test suite enforces this.
- Tests never use a real `~/.hermes` or `~/.claude`.
