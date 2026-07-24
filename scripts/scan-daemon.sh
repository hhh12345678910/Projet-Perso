#!/bin/bash
# Wrapper that launches the Python daemon command under systemd.
# The Python loop handles retries and sleeps internally — this script
# is just the process entry point that systemd monitors and restarts
# if the Python process ever hard-crashes.
set -uo pipefail

# Derive the project dir from this script's own location (scripts/..), so the
# wrapper is portable across hosts/users with no hard-coded path.
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"

# Load .env so SPORT_LIST / MIN_EV / BREATHER can be tuned there without editing
# this file or the systemd unit. systemd doesn't source .env itself, and
# runscan.sh only sources it for the Python process *after* these args are
# built — so we source it here too, before reading the vars below. Set e.g.
# SPORT_LIST=soccer,tennis,hockey,volleyball in .env to drop a sport (basket).
set -a; [ -f "$PROJECT_DIR/.env" ] && . "$PROJECT_DIR/.env"; set +a

SPORT_LIST="${SPORT_LIST:-soccer,tennis,basketball,hockey,volleyball}"
BREATHER="${BREATHER:-10}"
MIN_EV="${MIN_EV:-5}"
LOG_FILE="${LOG_FILE:-$PROJECT_DIR/valuebet.log}"
# Betano is fed by the browser userscript -> betano_ingest_server.py, which
# writes this JSON file. Default matches the ingest server's default output.
# Set BETANO_FILE= (empty) in .env to fall back to the live cookie path.
BETANO_FILE="${BETANO_FILE:-$PROJECT_DIR/data/betano.json}"

exec >> "$LOG_FILE" 2>&1

cd "$PROJECT_DIR"

# Only pass --betano-file when the file actually exists, so a fresh install
# (server not started yet) doesn't wedge Betano on a missing path — the daemon
# then just skips Betano until the first push lands.
BETANO_ARGS=()
if [ -n "$BETANO_FILE" ] && [ -f "$BETANO_FILE" ]; then
    BETANO_ARGS=(--betano-file "$BETANO_FILE")
fi

exec "$PROJECT_DIR/runscan.sh" daemon \
    --sport "$SPORT_LIST" \
    --breather "$BREATHER" \
    --min-ev "$MIN_EV" \
    "${BETANO_ARGS[@]}"
