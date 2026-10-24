# Workflow-owned wait rounds

`max_wait_seconds` is a positive safe integer, default 14400 (four hours),
bounding the board watch across all wait rounds; a shorter `max_hours` still
applies. Each `wait round N` is a new read-only agent call, capped at 13 exit-3
waits. Exhaustion returns the last timeout JSON, not UNKNOWN. The workflow then
takes one fresh snapshot of the watched ids before continuing. Running cards
must have a current run still running and unended, and a heartbeat from that
run no more than 3600 seconds old. Queued cards (triage, todo, ready, scheduled)
and parked blocked/review cards keep waiting for starts/resumes across rounds;
they and finished cards need no heartbeat. A failed/malformed read, missing
card, unknown status or unhealthy running card stops continuation as UNKNOWN
with the reason. Reaching the total deadline while cards remain unfinished is
UNKNOWN with a deadline reason,
never completion. Live/queued row prompts also snapshot before blocking calls
and shorten their final wait to the same deadline; their older retry caps are
not an additional budget. This does not dispatch, restart or edit any card.
