#!/bin/bash
# Installateur des unités systemd du projet, pour l'utilisateur et le chemin
# COURANTS — aucun /home/ubuntu codé en dur nulle part.
#
#     bash scripts/setup.sh            # installe dépendances + venv + unités
#     bash scripts/setup.sh --check    # ne touche à RIEN : compare seulement
#     bash scripts/setup.sh --units    # unités seules, sans apt ni venv
#
# Pourquoi les unités sont des gabarits (scripts/*.service.in)
# ------------------------------------------------------------
# Elles portaient auparavant `User=ubuntu` et `/home/ubuntu/Projet-Perso` en
# dur. Sur un déploiement qui n'est ni l'un ni l'autre — le cas réel — les
# copier revenait à casser le daemon, et les unités installées à la main
# divergeaient silencieusement du dépôt. `--check` sert exactement à voir
# cette dérive sans rien modifier.
#
# Et surtout : cet installateur ne posait QUE valuebet-daemon. Ni l'ingest
# navigateur, ni la purge, ni close-lines. Une installation neuve suivie à la
# lettre tournait donc sans capture de ligne de clôture — c'est-à-dire en
# perdant définitivement la seule mesure d'edge du système, sans le moindre
# signe extérieur. Toutes les unités sont posées ici désormais.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
USER_NAME="$(whoami)"
UNIT_DIR=/etc/systemd/system
cd "$PROJECT_DIR"

MODE="${1:-full}"
case "$MODE" in
    --check) MODE=check ;;
    --units) MODE=units ;;
    full|"") MODE=full ;;
    *) echo "Option inconnue : $MODE (attendu: --check, --units)" >&2; exit 2 ;;
esac

# Unités posées, et lesquelles activer. Les deux .service en oneshot sont
# déclenchés par leur timer : les activer les ferait tourner au démarrage.
UNITS=(
    valuebet-daemon.service
    betano-ingest.service
    valuebet-prune.service
    valuebet-close-lines.service
    valuebet-prune.timer
    valuebet-close-lines.timer
)
ENABLE=(
    valuebet-daemon.service
    betano-ingest.service
    valuebet-prune.timer
    valuebet-close-lines.timer
)

render() {   # render <nom-d-unité> -> stdout
    local src="scripts/$1.in"
    [ -f "$src" ] || src="scripts/$1"          # les timers n'ont pas de gabarit
    sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
        -e "s|__USER__|$USER_NAME|g" \
        -e '/^### Gabarit/d' -e '/^### Ne pas copier/d' "$src"
}

echo "==> Projet : $PROJECT_DIR  (utilisateur: $USER_NAME)"

if [ "$MODE" = check ]; then
    echo "==> Comparaison des unités installées avec ce que le dépôt produirait"
    drift=0
    for unit in "${UNITS[@]}"; do
        tmp="$(mktemp)"; render "$unit" > "$tmp"
        if [ ! -f "$UNIT_DIR/$unit" ]; then
            echo "   ✗ $unit — ABSENTE de $UNIT_DIR"; drift=1
        elif diff -q "$UNIT_DIR/$unit" "$tmp" > /dev/null; then
            echo "   ✓ $unit"
        else
            echo "   ⚠ $unit — diffère de ce que le dépôt produirait :"
            # `|| true` obligatoire : diff sort en 1 quand les fichiers
            # diffèrent, ce qui est précisément le cas ici, et `set -e` +
            # `pipefail` tuaient le script juste après avoir affiché le
            # premier diff — on ne voyait qu'une unité sur six, sans erreur.
            { diff -u "$UNIT_DIR/$unit" "$tmp" | tail -n +3 | sed 's/^/       /'; } || true
            drift=1
        fi
        rm -f "$tmp"
    done
    echo ""
    [ "$drift" -eq 0 ] && echo "✅ Aucune dérive." \
        || echo "⚠️  Dérive détectée. Pour aligner : bash scripts/setup.sh --units"
    exit 0
fi

if [ "$MODE" = full ]; then
    echo "==> 1/3 Dépendances système"
    sudo apt-get update -y
    sudo apt-get install -y python3 python3-venv python3-pip git

    echo "==> 2/3 Environnement Python (.venv) + dépendances"
    [ -d .venv ] || python3 -m venv .venv
    .venv/bin/pip install --upgrade pip
    .venv/bin/pip install -r requirements.txt

    if [ ! -f .env ]; then
        echo "   ⚠️  Aucun .env. Crée-le AVANT de démarrer :  nano $PROJECT_DIR/.env"
    fi
fi

echo "==> Unités systemd"
chmod +x runscan.sh scripts/scan-daemon.sh
for unit in "${UNITS[@]}"; do
    tmp="$(mktemp)"; render "$unit" > "$tmp"
    sudo install -m 644 "$tmp" "$UNIT_DIR/$unit"
    rm -f "$tmp"
    echo "   posée : $unit"
done

sudo systemctl daemon-reload
for unit in "${ENABLE[@]}"; do
    sudo systemctl enable "$unit" > /dev/null
    echo "   activée : $unit"
done

echo ""
echo "✅ Unités installées et activées (pas démarrées — à toi de jouer)."
echo "   sudo systemctl restart valuebet-daemon betano-ingest"
echo "   systemctl list-timers 'valuebet-*'"
echo ""
echo "   ⚠️  valuebet-close-lines.timer doit tourner plus souvent que la"
echo "       rétention de la purge : le prix de clôture n'existe que dans"
echo "       notre propre capture, une fois purgé le CLV est perdu."
