#!/bin/bash
# Push the latest local DB snapshot to a private GitHub repo so the
# value_bets + clv_snapshots history survives the loss of the VPS itself.
# Reads GITHUB_USER / GITHUB_TOKEN / BACKUP_REPO_NAME from .env (the same
# file scan and close-lines already use), so cron can invoke this directly
# without any wrapper.
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-/home/ubuntu/Projet-Perso}"
BACKUP_DIR="${BACKUP_DIR:-/home/ubuntu/backups}"
REPO_DIR="${REPO_DIR:-/home/ubuntu/valuebet-backups}"
KEEP_DAYS_REMOTE="${KEEP_DAYS_REMOTE:-30}"

# Self-load the project .env so cron sees GITHUB_TOKEN et al. — runscan.sh
# only sources it for python; we mirror the same idiom here.
if [[ -f "$PROJECT_DIR/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source "$PROJECT_DIR/.env"
    set +a
fi

: "${GITHUB_USER:?GITHUB_USER not set in .env}"
: "${GITHUB_TOKEN:?GITHUB_TOKEN not set in .env}"
: "${BACKUP_REPO_NAME:?BACKUP_REPO_NAME not set in .env}"

REPO_URL="https://${GITHUB_USER}:${GITHUB_TOKEN}@github.com/${GITHUB_USER}/${BACKUP_REPO_NAME}.git"

latest=$(ls -1t "$BACKUP_DIR"/valuebet-*.db.gz 2>/dev/null | head -n 1 || true)
if [[ -z "$latest" ]]; then
    echo "$(date -Is) push-backups: no local snapshot to push"
    exit 0
fi

# First-time clone vs subsequent runs.
if [[ ! -d "$REPO_DIR/.git" ]]; then
    git clone --quiet "$REPO_URL" "$REPO_DIR"
    cd "$REPO_DIR"
    git config user.email "vps@valuebet.local"
    git config user.name "Valuebet VPS"
else
    cd "$REPO_DIR"
    # Refresh the URL in case the token was rotated since the last push.
    git remote set-url origin "$REPO_URL"
    git pull --quiet --rebase || true
fi

cp "$latest" "$REPO_DIR/$(basename "$latest")"

# Rotate: stop tracking snapshots older than KEEP_DAYS_REMOTE days.
find . -maxdepth 1 -name 'valuebet-*.db.gz' -mtime +"$KEEP_DAYS_REMOTE" \
    -exec git rm -f {} \; > /dev/null 2>&1 || true

git add -A
if git diff --cached --quiet; then
    echo "$(date -Is) push-backups: nothing new to commit"
    exit 0
fi
git commit -m "Backup $(date +%Y-%m-%d)" --quiet
git push --quiet origin HEAD
echo "$(date -Is) push-backups: pushed $(basename "$latest") to $BACKUP_REPO_NAME"
