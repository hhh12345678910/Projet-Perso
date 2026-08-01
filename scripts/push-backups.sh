#!/bin/bash
# Push the latest local DB snapshot to a private GitHub repo so the
# value_bets + clv_snapshots history survives the loss of the VPS itself.
# Reads GITHUB_USER / GITHUB_TOKEN / BACKUP_REPO_NAME from .env (the same
# file scan and close-lines already use), so cron can invoke this directly
# without any wrapper.
set -euo pipefail

# Chemins déduits, comme scan-daemon.sh — les défauts en dur sur /home/ubuntu
# ne valaient que pour la toute première machine.
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups}"
REPO_DIR="${REPO_DIR:-$HOME/valuebet-backups}"
KEEP_DAYS_REMOTE="${KEEP_DAYS_REMOTE:-30}"

# Ce script poussait une copie de la base ENTIÈRE, `quotes` compris — 20 Go de
# cotes brutes, plusieurs gigaoctets même compressées. GitHub refuse tout
# fichier de plus de 100 Mo : la sauvegarde ne pouvait plus aboutir depuis que
# la base a grossi. On pousse désormais l'export compact produit par
# `runscan.sh export-tracking`, qui ne contient que l'historique durable.
TRACKING_EXPORT="${TRACKING_EXPORT:-$PROJECT_DIR/data/tracking.db.gz}"

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

# L'export compact d'abord ; la sauvegarde complète en repli, pour les
# installations où la base est encore assez petite pour passer.
if [[ -f "$TRACKING_EXPORT" ]]; then
    latest="$TRACKING_EXPORT"
else
    latest=$(ls -1t "$BACKUP_DIR"/valuebet-*.db.gz 2>/dev/null | head -n 1 || true)
fi
if [[ -z "$latest" ]]; then
    echo "$(date -Is) push-backups: rien à pousser — lance d'abord" \
         "\`./runscan.sh export-tracking\`"
    exit 0
fi

size=$(stat -c%s "$latest")
if (( size > 95000000 )); then
    echo "$(date -Is) push-backups: $latest fait $((size/1000000)) Mo," \
         "au-delà de la limite GitHub de 100 Mo — abandon" >&2
    exit 1
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
