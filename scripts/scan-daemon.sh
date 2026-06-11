#!/bin/bash
# Wrapper that launches the Python daemon command under systemd.
# The Python loop handles retries and sleeps internally — this script
# is just the process entry point that systemd monitors and restarts
# if the Python process ever hard-crashes.
set -uo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/Projet-Perso}"
SPORT_LIST="${SPORT_LIST:-soccer,tennis,basketball,hockey}"
BREATHER="${BREATHER:-10}"
LOG_FILE="${LOG_FILE:-/home/ubuntu/valuebet.log}"

exec >> "$LOG_FILE" 2>&1

cd "$PROJECT_DIR"

exec "$PROJECT_DIR/runscan.sh" daemon \
    --sport "$SPORT_LIST" \
    --breather "$BREATHER"
