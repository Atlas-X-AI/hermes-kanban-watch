# Contributing

Small, reproducible fixes are welcome. Discuss substantial behaviour changes in
an issue before writing a large patch. Do not include credentials, private board
contents, worker transcripts or personal paths in issues or pull requests.
For vulnerabilities, use the private route in [SECURITY.md](SECURITY.md).

## Run the checks

Requirements: Python 3.11+, Node.js on PATH and pytest. The plugin itself has no
third-party Python runtime dependencies. With uv installed:

```sh
uv sync --all-extras --dev
uv run pytest -q tests
```

Or, in an environment that already provides pytest:

```sh
python3 -m pytest -q tests
python3 tests/verify.py
```

The suite exercises disposable SQLite boards and session stores, routing,
doctor fixes/probes using a fake CLI, credential-open canaries, hook parsing and
the complete workflow body. `tests/verify.py` also checks the complete wrapped
workflow with `node --check`. No live profiles, credentials or models are needed.
Do not disable bytecode generation to make the public-source scan pass.

CI uses the shared workflow referenced in `.github/workflows/ci.yml`. A local
pass does not prove a live Claude Code UI session or upstream schema support.

With Claude Code already installed, run `bash tests/smoke/install_smoke.sh "$PWD"`
locally for a clean marketplace install in disposable `HOME` and `HERMES_HOME`,
without login or credentials. `.github/workflows/install-smoke.yml` runs this on
three fresh GitHub-hosted machines using Python 3.11, 3.12 and 3.13.

## Pull requests

- Keep changes scoped, explain the problem, and include reproduction steps.
- Add a failing regression test first for code changes, then make it pass.
- Include the exact commands and results, and say what remains untested.
- Keep watchers read-only. Never turn worker output into watcher instructions,
  expand permissions after a denial, or add implicit dispatch/setup writes.
- Use synthetic, sanitised fixtures. Tests must not touch real user homes.
- Keep public examples portable; no local machine paths or internal project names.
- Keep `VERSION`, `pyproject.toml`, the plugin manifest and marketplace entry versions in sync.
  The installed `hermes-kanban-live --version` identity must agree with them.
- Keep every workflow row on `hermes-kanban-watch:kanban-watcher` or the validated
  `agent_type` override. No test may rely on a private user agent or legacy reader.

Changing routes must preserve different-model-family review. Keep documentation
honest about UNKNOWN compatibility rather than extending support by assumption.
