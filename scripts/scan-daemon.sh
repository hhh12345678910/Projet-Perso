#!/bin/bash
# Continuous-mode wrapper: runs `scan` back-to-back forever. systemd keeps
# the service alive; this loop handles the "next cycle starts immediately
# after the previous one returns" semantics inside the process so we don't
# rely on a fixed cron cadence.
set -uo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/Projet-Perso}"
SPORT_LIST="${SPORT_LIST:-soccer,tennis,basketball,hockey}"
BREATHER_SECONDS="${BREATHER_SECONDS:-5}"
LOG_FILE="${LOG_FILE:-/home/ubuntu/valuebet.log}"

# Funnel everything to the same log cron already writes to, so `tail -f` keeps
# showing one ordered timeline.
exec >> "$LOG_FILE" 2>&1

cd "$PROJECT_DIR"

while true; do
    echo "$(date -Is) daemon: starting scan cycle"
    if "$PROJECT_DIR/runscan.sh" scan --sport "$SPORT_LIST"; then
        echo "$(date -Is) daemon: scan cycle completed"
    else
        rc=$?
        echo "$(date -Is) daemon: scan exited with $rc — sleeping before retry"
    fi
    sleep "$BREATHER_SECONDS"
done
