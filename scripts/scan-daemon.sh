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
# SPORT_LIST=... in .env to change the set without editing this file.
# Volleyball and basketball are out of the default: Pinnacle prices only 2
# volleyball events, so no fair line can be built for it at all.
set -a; [ -f "$PROJECT_DIR/.env" ] && . "$PROJECT_DIR/.env"; set +a

SPORT_LIST="${SPORT_LIST:-soccer,tennis,hockey}"
BREATHER="${BREATHER:-10}"
MIN_EV="${MIN_EV:-5}"
LOG_FILE="${LOG_FILE:-$PROJECT_DIR/valuebet.log}"
# Betano has two feeds, and the preferred one needs nothing here:
#   1. Cookie push (default). The userscript sends the session cookie, the
#      ingest server stores it, and BetanoScraper picks it up on every fetch —
#      no --betano-file involved. data/betano.json simply won't exist, so the
#      block below stays empty and the daemon uses the live path.
#   2. Odds-dump push (fallback). If something writes data/betano.json, it's
#      passed as --betano-file instead.
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
