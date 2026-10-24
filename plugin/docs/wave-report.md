# Wave time and actual-run report

Use the installed reader, or replace `hermes-kanban-live` with
`python3 <plugin-root>/scripts/kanban_live.py`:

```sh
hermes-kanban-live report --board demo
hermes-kanban-live report --board demo --task t_1234abcd --task t_11111111
hermes-kanban-live report --board demo --since 1792700000 --format text
```

JSON is one envelope with `read_status`, `now`, `cards`, `totals` and `missing`.
Each card object has `id`, `title`, `status`, `assignee`, `model_used`,
`provider_used`, `fallback_from`, `runs`, `retried`, `duration_s` and `warnings`.
Model/provider/fallback, duration and warning counts describe the newest run by
run id, never the card's pin. No run means null model/timing, zero runs and empty
warnings. `retried` uses the reader's conservative retry guard: multiple runs or
a blocked/unblocked/gave-up/timed-out/crashed/changes-requested event.

Without `--task`, all cards are included, even finished or archived ones.
Repeated `--task` ids select a set. `--since` selects cards created at or after
that epoch; combined filters intersect. Runs are not time-clipped: `totals`
contains `by_status`, `run_seconds` across every run of the selected cards,
`fell_back` (cards with a fallback on any run), and `retried` (cards, not attempts).
Running durations use the report's `now`; missing timing stays null and is
excluded from the seconds total. Durations are elapsed seconds, not token counts
or a currency estimate. `--format text` prints aligned columns and a totals line,
without Markdown. Exit 4 means an explicit id is absent from the board (not merely
filtered out by `--since`); exit 5 means the board could not be read.
