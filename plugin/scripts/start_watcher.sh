#!/usr/bin/env bash
# systemd/Hermes launcher, or prepare one brief for the starting harness's native agent.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$SCRIPT_DIR/watcher_launch.py" "$@"
