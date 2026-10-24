# Verify the families that actually ran

```sh
hermes-kanban-live verify-pair --board demo --worker t_1234abcd --verifier t_11111111
hermes-kanban-live verify-pair --board demo --worker t_1234abcd --verifier t_11111111 --families /path/to/routes.yaml
```

The JSON result names each card's latest **finished** run (`ended_at` is set),
its `run_id`, `model_used`, `provider_used` and configured `family`, plus
`different_families`. A newer unfinished run is not a finished review. No profile
config or model pin is read. The actual run's `provider_used` is looked up in the
routes file's `providers` mapping; this is a configured model-family assertion,
not a guess from an API hostname or model-name prefix. Map hosts serving the same
family alike, and use family-specific identifiers for multi-family hosts.

File precedence is `--families`, `KANBAN_WATCH_ROUTES`, the user routes file, then
the shipped template, exactly as for `route.py`. YAML/JSON parsing is shared with
the router; only a non-empty, valid `providers` mapping is required here.
Exit 0 means different known families, 1 means the same family **or classification
failed**, 3 means either card has no finished run (including an absent id), and 5
means unreadable board. On classification failure `different_families` is null
and `error` explains the failure; never treat that as independence. A family
comparison does not prove the verifier actually inspected the deliverable.
