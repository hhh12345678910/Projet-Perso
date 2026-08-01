#!/bin/bash
# Rotate-and-snapshot SQLite backups so a VPS crash doesn't wipe out the
# value-bet + CLV history we're collecting. Run via cron once a day.
set -euo pipefail

# Chemin déduit de l'emplacement du script, comme scan-daemon.sh : les défauts
# en dur sur /home/ubuntu ne valaient que pour la toute première machine.
PROJECT_DIR="${PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
DB_FILE="$PROJECT_DIR/data/valuebet.db"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups}"
KEEP_DAYS=14

if [[ ! -f "$DB_FILE" ]]; then
    # SQLite file isn't created until scan runs at least once — silently
    # accept the no-op rather than spamming cron mail.
    exit 0
fi

mkdir -p "$BACKUP_DIR"
stamp=$(date +%Y%m%d-%H%M%S)
out="$BACKUP_DIR/valuebet-$stamp.db.gz"

# Use SQLite's online .backup command so we don't corrupt anything while a
# scan is mid-write, then gzip outside the lock.
sqlite3 "$DB_FILE" ".backup '$BACKUP_DIR/.staging-$stamp.db'"
gzip -9 -c "$BACKUP_DIR/.staging-$stamp.db" > "$out"
rm -f "$BACKUP_DIR/.staging-$stamp.db"

# Rotate: drop any compressed snapshot older than KEEP_DAYS days.
find "$BACKUP_DIR" -maxdepth 1 -name 'valuebet-*.db.gz' -mtime +$KEEP_DAYS -delete

echo "$(date -Is) backup ok: $out ($(stat -c%s "$out") bytes)"
