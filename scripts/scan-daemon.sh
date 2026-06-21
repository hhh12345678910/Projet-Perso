#!/bin/bash
# Wrapper that launches the Python daemon command under systemd.
# The Python loop handles retries and sleeps internally — this script
# is just the process entry point that systemd monitors and restarts
# if the Python process ever hard-crashes.
set -uo pipefail

# Derive the project dir from this script's own location (scripts/..), so the
# wrapper is portable across hosts/users with no hard-coded path.
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
SPORT_LIST="${SPORT_LIST:-soccer,tennis,basketball,hockey,volleyball}"
BREATHER="${BREATHER:-10}"
MIN_EV="${MIN_EV:-5}"
LOG_FILE="${LOG_FILE:-$PROJECT_DIR/valuebet.log}"

exec >> "$LOG_FILE" 2>&1

cd "$PROJECT_DIR"

exec "$PROJECT_DIR/runscan.sh" daemon \
    --sport "$SPORT_LIST" \
    --breather "$BREATHER" \
    --min-ev "$MIN_EV"
