#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
    printf 'Usage: bash tests/smoke/install_smoke.sh REPO_DIR\n' >&2
    exit 2
fi
REPO=$(cd -- "$1" && pwd)
TMP=$(mktemp -d)
trap 'rm -rf -- "$TMP"' EXIT
mkdir -p "$TMP/user" "$TMP/hermes" "$TMP/work"

# No inherited credentials, profile selection, XDG config or board routing.
# The CLI's executable remains on PATH, but it sees only disposable user data.
cd -- "$TMP/work"
env -i PATH="$PATH" HOME="$TMP/user" HERMES_HOME="$TMP/hermes" \
    XDG_CONFIG_HOME="$TMP/user/.config" XDG_CACHE_HOME="$TMP/user/.cache" \
    XDG_DATA_HOME="$TMP/user/.local/share" TMPDIR="$TMP" \
    INSTALL_SMOKE_CANARY_CONTROL="${INSTALL_SMOKE_CANARY_CONTROL:-0}" \
    python3 "$REPO/tests/smoke/install_helpers.py" "$REPO"
printf 'INSTALL SMOKE PASS\n'
