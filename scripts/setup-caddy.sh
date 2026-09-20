#!/bin/bash
# Installe la configuration Caddy qui expose l'Analytics — et RIEN d'autre.
#
#     sudo bash scripts/setup-caddy.sh analytics.exemple.com dylan
#     sudo bash scripts/setup-caddy.sh --check analytics.exemple.com dylan
#
# POURQUOI CE SCRIPT PLUTÔT QU'UN FICHIER COLLÉ À LA MAIN
# -------------------------------------------------------
# `scripts/valuebet-analytics.service` a été écrit à la main sur la VM, et
# `setup.sh --check` ne le verra donc jamais dériver. On ne refait pas la même
# chose : le Caddyfile vit dans le dépôt, ce script le RÉALISE, et `--check`
# compare ce qui tourne à ce que le dépôt produirait.
#
# ⚠️ CE SCRIPT NE TOUCHE À RIEN D'AUTRE. Ni le daemon, ni le listener, ni
# l'Analytics, ni la base. Il n'installe pas Caddy non plus — cette décision
# reste manuelle et documentée.
set -euo pipefail

MODE=install
if [ "${1:-}" = "--check" ]; then MODE=check; shift; fi

DOMAINE="${1:-}"
UTILISATEUR="${2:-}"
if [ -z "$DOMAINE" ] || [ -z "$UTILISATEUR" ]; then
    echo "usage: $0 [--check] <domaine> <utilisateur>" >&2
    exit 2
fi

PROJET="$(cd "$(dirname "$0")/.." && pwd)"
GABARIT="$PROJET/scripts/Caddyfile.in"
CIBLE=/etc/caddy/Caddyfile

command -v caddy >/dev/null || {
    echo "Caddy n'est pas installé. Voir la procédure — ce script ne l'installe pas." >&2
    exit 3
}

rendre() {   # rendre <hash> -> stdout
    sed -e "s|__DOMAINE__|$DOMAINE|g" \
        -e "s|__UTILISATEUR__|$UTILISATEUR|g" \
        -e "s|__HASH__|$1|g" \
        -e '/^### Gabarit/d' -e '/^### Ne pas copier/d' \
        -e "/^### l'installation/d" "$GABARIT"
}

if [ "$MODE" = check ]; then
    echo "==> Comparaison de $CIBLE avec ce que le dépôt produirait"
    [ -f "$CIBLE" ] || { echo "   ✗ $CIBLE ABSENT"; exit 1; }
    # Le hachage ne peut pas être recalculé (bcrypt est salé) : on le reprend
    # de la cible pour ne comparer que ce qui doit être identique.
    HASH="$(awk -v u="$UTILISATEUR" '$1 == u {print $2}' "$CIBLE" | head -1)"
    [ -n "$HASH" ] || { echo "   ✗ aucun hachage pour $UTILISATEUR"; exit 1; }
    tmp="$(mktemp)"; rendre "$HASH" > "$tmp"
    if diff -q "$CIBLE" "$tmp" >/dev/null; then
        echo "   ✓ conforme au dépôt"
    else
        echo "   ⚠ diffère de ce que le dépôt produirait :"
        { diff -u "$CIBLE" "$tmp" | tail -n +3 | sed 's/^/     /'; } || true
        rm -f "$tmp"; exit 1
    fi
    rm -f "$tmp"
    echo "==> Journal"
    if [ -d /var/log/caddy ]; then
        etranger="$(find /var/log/caddy ! -user caddy -print -quit)"
        if [ -n "$etranger" ]; then
            echo "   ⚠ $etranger n'appartient pas à caddy — le service ne pourra pas y écrire"
        else
            echo "   ✓ /var/log/caddy appartient bien à caddy"
        fi
    else
        echo "   ⚠ /var/log/caddy absent"
    fi
    echo "==> État du service"
    systemctl is-enabled caddy 2>/dev/null || true
    systemctl is-active  caddy 2>/dev/null || true
    exit 0
fi

# ⚠️ LE MOT DE PASSE NE PASSE PAS PAR LA LIGNE DE COMMANDE. Il finirait dans
# l'historique du shell, et dans `ps` le temps de l'exécution.
echo "Mot de passe pour « $UTILISATEUR » (rien ne s'affiche) :"
HASH="$(caddy hash-password)"

# Le service tourne sous cet utilisateur-là. S'il n'existe pas, tout ce qui
# suit produirait une configuration que personne ne peut lire — autant le
# dire maintenant, et fort.
id caddy >/dev/null 2>&1 || {
    echo "L'utilisateur « caddy » n'existe pas : Caddy n'a pas été posé par son paquet." >&2
    exit 4
}

mkdir -p /var/log/caddy
if [ -f "$CIBLE" ]; then cp -a "$CIBLE" "$CIBLE.avant-$(date +%Y%m%d%H%M%S)"; fi
rendre "$HASH" > "$CIBLE"
# ⚠️ AUCUN `|| true` ICI. Un chown qui échoue en silence laisse un fichier
# illisible par le service et un diagnostic impossible : on l'a vécu.
chown root:caddy "$CIBLE"
chmod 640 "$CIBLE"

echo "==> Validation"
caddy validate --config "$CIBLE" --adapter caddyfile

# ⚠️ NE REMONTE PAS CETTE LIGNE AU-DESSUS DE LA VALIDATION. `caddy validate`
# ne se contente pas de lire le fichier : il PROVISIONNE les modules, donc il
# crée /var/log/caddy/analytics.log — appartenant à root, puisque ce script
# tourne sous sudo. Le service, lui, tourne sous `caddy` : il meurt alors sur
# « open /var/log/caddy/analytics.log: permission denied » avec une
# configuration pourtant déclarée valide deux lignes plus haut.
chown -R caddy:caddy /var/log/caddy

echo "==> Rechargement"
if ! systemctl reload caddy && ! systemctl restart caddy; then
    echo "   ✗ Caddy refuse de démarrer avec cette configuration :" >&2
    journalctl -u caddy -n 15 --no-pager >&2
    echo "   La configuration précédente est dans $CIBLE.avant-*" >&2
    exit 5
fi
sleep 2
systemctl is-active caddy
echo "✓ https://$DOMAINE — le certificat peut prendre une minute à arriver."
