# Print a head-pinned PR merge receipt

```sh
python3 <plugin-root>/scripts/pr_gate.py --board demo --worker t_1234abcd --verifier t_11111111 --repo OWNER/NAME --pr 7
python3 <plugin-root>/scripts/pr_gate.py --board demo --worker t_1234abcd --verifier t_11111111 --repo OWNER/NAME --pr 7 --routes /path/to/routes.yaml --format json
```

This is a read-only pre-merge check, not a merger or permission grant. It imports
`kanban_live.py`'s `verify-pair` implementation to compare actual finished runs,
with `--routes` selecting its family mapping (otherwise the normal route-file
precedence applies). It also requires the verifier card to be `done` and its
newest run to be the finished run that was compared, with a summary starting
with literal `PASS`. An older PASS cannot authorise a newer unfinished attempt.

The reviewed SHA is `head_sha` in that run's JSON metadata, when the key exists;
otherwise it is the first standalone full 40-hex SHA in the complete summary.
Older boards without a metadata column use the summary. An invalid metadata
value is UNKNOWN, never a reason to silently fall back to another SHA.

One `gh pr view N --repo OWNER/NAME --json headRefOid,statusCheckRollup,mergeStateStatus`
reads the live head, CI and merge state together. The recorded SHA must equal that head. Every CheckRun
must be completed, and every check conclusion or legacy commit-status state must
be `SUCCESS`, `NEUTRAL` or `SKIPPED`. Pending/failed checks refuse the receipt;
missing, malformed or empty CI evidence is UNKNOWN, not a vacuous pass.

`mergeStateStatus` must be `CLEAN` by default. A refusal names the observed state
and prints no merge command. Missing or unreadable merge-state evidence is
UNKNOWN. To accept a specific exceptional state, supply
`--allow-merge-state HAS_HOOKS` or `--allow-merge-state UNSTABLE`; the flag is
repeatable and does not permit `DIRTY` or any other state. This overrides only
the merge-state check, not the exact-head, CI or independent-verifier checks.

Exit 0 prints the verifier card, both runs' actual models/providers/families,
verdict, head SHA, CI lines and this exact head-pinned command:
`gh pr merge N --repo OWNER/NAME --squash --match-head-commit <sha>`.
The default format is Markdown; `--format json` returns one object containing
`worker`, `verifier`, `verdict`, `head_sha`, `ci` and `merge_command`.
Exit 1 reports a failed check; exit 3 reports an unreadable/unknown observation.
Both omit the merge command and name `failed_check` and `reason` in JSON.

The script never executes the printed command, comments, marks ready, writes to
a board, reads profile configs or opens credentials. `gh` must already be set up
by its owner; `PR_GATE_GH` may select an alternative executable (not a shell
command), principally for testing. Run the printed command only through the
repository's independently authorised merge lane. Re-run the gate if evidence
changes; `--match-head-commit` protects the command against a moved PR head.
