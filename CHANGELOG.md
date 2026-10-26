# Changelog

## 0.4.1

- Use Claude Code background Bash waits with completion notifications, not a
  synchronous 960-second wait. Keep the 900-second cadence and Hermes timeout.
- Isolate all four XDG roots per test, including inherited XDG_STATE_HOME leases.
- Add optional `--discord-channel` direct helper reports through the lint-gated,
  read-back-verified messaging sender. Render at most eight safe state-marked
  lines with the starting-session source footer; never copy card text to Discord.
- In Discord mode, queue only problems and final/stop notes to the starting
  session, not routine summaries. Sender failures remain UNKNOWN and pending;
  preserve a verified send receipt across an inbox retry to avoid reposting.
- Document direct helper delivery as the default for operators working in Discord.
  Tests use an injected fake sender and disposable boards/inboxes only.

## 0.4.0

- Replace the default multi-row Workflow with ONE cheap native watcher per board:
  Claude Code Sonnet, Codex-native Luna, Hermes dedicated Gemini/GLM profile.
- Add a board-only SQLite read-only snapshot/wait reader and a common plain-text
  summary, at most 12 lines. Actual models/providers come from runs, not pins.
- Wake every 15 minutes, immediately on new problems (within the 60-second poll),
  and once all watched cards are done/archived. Blocked/review are not completion.
- Queue exact-session Claude inbox messages with row read-back, pane validation,
  input-hold compatibility and stable dedupe keys. Never type directly into tmux.
- Add native-agent brief preparation, a shared local singleton lease, a bounded
  Hermes/systemd bridge, durable delivery retries and a 12-hour default limit.
- Point run-policy decisions at `doctrine/running-a-kanban.md`; the starting
  session owns the human relay, review/CI timing and repo memory.
- Retain the old Workflow as explicit opt-in only. No global settings or profile
  edits, dispatch or board writes. Native Codex/Hermes progress delivery, live
  model/poller delivery and the parent-run proof remain UNKNOWN until exercised.

## 0.3.0

- Previous public release: live Workflow rows, portable routes, read-only reports,
  parent-question waits and optional authoring/setup helpers.
