#!/bin/bash
# Identify which sportsbook platform a site runs on.
#
# Worth doing before writing any scraper: platforms are shared. Unibet, 711,
# Bingoal and Scooore are all Kambi and share one scraper; Golden Palace and
# StarCasino are both Altenar. A new operator on a platform already supported
# costs an operator id, not an integration — so this check decides whether the
# work is ten minutes or a day.
#
# Usage: ./tools/detect-platform.sh https://www.bet777.be/fr https://www.magicbetting.be
set -uo pipefail

UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36'

for url in "$@"; do
    echo "══ $url"
    body=$(curl -sL --max-time 20 -A "$UA" "$url" 2>/dev/null)
    code=$(curl -sL -o /dev/null -w '%{http_code}' --max-time 20 -A "$UA" "$url" 2>/dev/null)
    echo "   HTTP $code, ${#body} octets"
    if [ "${#body}" -lt 500 ]; then
        echo "   ⚠ réponse vide ou bloquée depuis cette IP"
        echo
        continue
    fi

    found=0
    # Signatures are matched against the delivered HTML/JS: platforms leave
    # their name in asset paths and widget bootstraps even when the brand is
    # white-labelled.
    while IFS='|' read -r name pattern note; do
        if printf '%s' "$body" | grep -qiE "$pattern"; then
            echo "   ✅ $name — $note"
            found=1
        fi
    done <<'SIGS'
Kambi|kambi|scraper existant (unibet.py) — ajouter l'operateur suffit
Altenar|altenar|scraper existant (goldenpalace.py) — ajouter l'operateur suffit
Danae / Kaizen|danae-webapi|meme plateforme que Betano (betano.py)
Superbet|superbet|meme plateforme que Napoleon (napoleon.py)
Eurobet|eurobet|meme plateforme que Ladbrokes (ladbrokes.py)
Entain|entain|meme plateforme que BetFirst (betfirst.py)
BetConstruct|betconstruct|nouvelle plateforme, scraper a ecrire
Playtech|playtech|nouvelle plateforme, scraper a ecrire
Cashpoint / Merkur|cashpoint|meme plateforme que Betcenter (betcenter.py)
SIGS

    [ "$found" -eq 0 ] && echo "   ❓ aucune signature connue — utiliser l'espion pour trouver son API"

    # Anti-bot matters as much as the platform: it decides whether the VM can
    # fetch directly or whether this needs the browser-push route.
    printf '%s' "$body" | grep -qi 'datadome'   && echo "   ⚠ DataDome détecté → push navigateur nécessaire"
    printf '%s' "$body" | grep -qi 'cf-browser\|cloudflare' && echo "   ⚠ Cloudflare détecté"
    echo
done
