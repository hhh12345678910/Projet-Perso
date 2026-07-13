#!/bin/bash
# Reactivate / refresh Betano by pasting its browser cookie once.
#
# Betano's cf_clearance + datadome tokens expire every few hours and are bound
# to the browser + IP, so they can't be fetched from the (datacenter) VM. This
# helper makes the manual refresh a 10-second job: paste the cookie, it rewrites
# BETANO_COOKIE in .env and restarts the daemon.
#
# Usage:
#   ./scripts/set-betano-cookie.sh
#   → paste the cookie (DevTools → Network → any /danae-webapi/ request →
#     Copy as cURL → the value after `-b '…'`), press Enter then Ctrl+D.
set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_DIR"

echo "Colle ton cookie Betano (cf_clearance + datadome), puis Entrée et Ctrl+D :"
COOKIE="$(cat)"
COOKIE="${COOKIE//$'\n'/}"          # strip any newlines from the paste
COOKIE="${COOKIE#"${COOKIE%%[![:space:]]*}"}"   # trim leading space
COOKIE="${COOKIE%"${COOKIE##*[![:space:]]}"}"   # trim trailing space

if [ -z "$COOKIE" ]; then
    echo "✗ Cookie vide — rien fait." >&2
    exit 1
fi
if ! printf '%s' "$COOKIE" | grep -q "datadome"; then
    echo "⚠  Le cookie ne contient pas 'datadome' — es-tu sûr de l'avoir copié en entier ?"
    echo "   (je l'enregistre quand même)"
fi

# Rewrite the BETANO_COOKIE line in .env (single-quoted; cookies never contain
# single quotes, so this is safe). Idempotent — replaces any existing line.
touch .env
grep -v '^BETANO_COOKIE=' .env > .env.tmp || true
printf "BETANO_COOKIE='%s'\n" "$COOKIE" >> .env.tmp
mv .env.tmp .env
echo "✓ BETANO_COOKIE enregistré dans .env."

echo "Redémarrage du daemon…"
if sudo systemctl restart valuebet-daemon.service; then
    echo "✓ Daemon redémarré — Betano réactivé. Vérifie dans ~1 min :"
    echo "    grep -i 'betano' valuebet.log | tail -3"
else
    echo "✗ Le redémarrage a échoué — lance-le à la main :" >&2
    echo "    sudo systemctl restart valuebet-daemon.service" >&2
    exit 1
fi
