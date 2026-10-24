# Arm needs once with its own timeout loop

```sh
hermes-kanban-live needs --board demo --loop --created-by my-parent-session --cursor-key my-wave
hermes-kanban-live needs --board demo --loop --since 1792700000 --created-by my-parent-session
```

`--loop` consumes normal exit-3 timeout windows internally, sleeping at
`--interval` (default 15 seconds) and keeping the original `--since` throughout.
If omitted, `--since` is captured once when the command starts. The first poll
also considers older blocked/dependency-wait/park events whose wait is still in
force, so an already-waiting card from `--created-by` can immediately produce a
`parent_question`. Answered, resumed or superseded old waits are not replayed.
Existing seen-cursor deduplication still applies; use a distinct `--cursor-key`
for an independent caller.

The command exits 0 with the first non-empty `{now, items}` result. It does not
retry unreadable boards, permission failures or any other error, and it does not
dispatch, answer or modify a card. `--max-seconds` is a retry-window size, not an
overall lifetime limit in loop mode; stop the caller to cancel an uneventful
watch. Without `--loop`, `--since` remains required and timeout/output behaviour
is unchanged. Direct Claude Code use of the new report/pair commands needs narrow
`Bash(hermes-kanban-live report:*)` and `Bash(hermes-kanban-live verify-pair:*)`
permissions; the existing needs permission covers `--loop`.
