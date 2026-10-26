#!/usr/bin/env bash
# systemd/Hermes launcher, or prepare one brief for the starting harness's native agent.
# Optional --discord-channel <id>: helper posts reports; the parent receives alerts/final only.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/watcher_launch.py" "$@"
