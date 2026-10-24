# File a fix and re-verify an earlier verdict

On an explicit authoring request, `file_card.py` can reuse a finished verifier's
handoff instead of requiring a copied brief:

```sh
python3 <plugin-root>/scripts/file_card.py --board demo --class app-code --from-verdict t_1234abcd --body-file /path/to/fix-gates.txt --created-by parent-wave --max-runtime 3600 --dry-run
python3 <plugin-root>/scripts/file_card.py --board demo --class app-code --reverify t_1234abcd --head 0123456789abcdef0123456789abcdef01234567 --created-by parent-wave --dry-run
```

Remove `--dry-run` to file one card through the normal create, model pin and
read-back path. Both modes read only the explicitly named board via `show --json`
before filing; dry-run performs that read but never runs create or set-model.
It prints shell-quoted commands containing the complete generated body. Keep
secrets out of summaries, fix lists and body files, just as for normal filing.

`--from-verdict V` requires V to be `done` with its latest run summary starting
`FAIL`. It generates `fix: <V title>`, using V's absolute workspace path, failing
head and summary. The failing head comes from that run's metadata `head_sha`, or
one unambiguous full SHA in its summary; absent/malformed evidence is refused,
not inferred from a checkout that may already have changed. Metadata `fix_list`
may be text (preserved verbatim) or a list of strings (numbered once); if absent,
the summary is the fix list. A non-empty `--body-file` containing the new fix's
allowlist and gates is required and appended without rewriting it.

`--reverify V --head SHA` generates `V2: <V title>` and reuses the earlier verifier
card's `assignee`, `model_override` and `provider_override` from `show --json`.
The supplied class does not choose the re-verifier's assignee or model/provider;
existing class/routes validation still applies. Missing or invalid source
assignee/model/provider pins are refused before creating a card, never inferred
from the class route. The new top section names the
new head, earlier card and summary; V's original body is preserved under
`## Earlier review`. V must be `done`; a finished PASS is allowed for re-review,
but not for filing a fix. `--head` must be exactly 40 hexadecimal characters.
Run history is selected by greatest run id, not array order; an unfinished latest
attempt is refused. Only cards with no runs use the legacy task result summary.
These modes generate their titles and inherit V's workspace, so omit `--title`
and `--worktree`. Re-verification preserves the original body. An optional
`--body-file` adds its UTF-8 contents under `## Parent note` directly after the
generated header (the whole earlier summary, however many paragraphs it has),
retaining the earlier review under `## Earlier review`.
`--board` and `--class` remain required in every mode.

Successful non-dry-run filing ends with
`Not dispatched. Run: hermes kanban --board <board> dispatch`, using the selected
board. This is an instruction only: filing creates, pins and verifies the card,
but never dispatches it. Dry runs do not print that line.

Filing options also apply to normal cards:

- `--created-by PARENT` passes Hermes's `create --created-by` and verifies its
  read-back, so `needs --created-by PARENT` can find the parent's cards.
- `--max-runtime SECONDS` passes a positive integer runtime cap to Hermes.
  Hermes stores it but omits `max_runtime_seconds` from `show --json`, so the
  cap is not checked on read-back.
- `--workspace-kind dir|worktree` explicitly selects the workspace kind. With no
  override, an existing `--worktree DIR` (or inherited workspace directory) uses
  `dir`, while a not-yet-existing path uses `worktree`. Output names the actual
  kind and absolute path. No directory or git worktree is created by this script.
  This supersedes the earlier normal-filing example's always-`worktree` default.

The earlier "creates no verifier" and "dry-run does not run Hermes" descriptions
apply to normal filing only: reverify creates one verifier, and verdict dry-runs
must read the earlier card. None of these commands dispatch workers, review code,
prove the configured families actually ran, or grant authoring access to a watcher.
