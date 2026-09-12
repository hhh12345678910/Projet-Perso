#!/usr/bin/env python3
"""Lance l'API Analytics. Lecture seule, et sur l'interface de bouclage.

⚠️ LE GARDE-FOU CENTRAL DE CE FICHIER : `exiger_acces` est vide, donc l'API
n'a AUCUN contrôle d'accès. Écouter sur 0.0.0.0 exposerait au réseau, sans
authentification, l'historique complet des détections, des paris joués et des
résultats. Ce script REFUSE donc de se lier à autre chose que l'interface de
bouclage, sauf `--public` explicite — et il dit alors ce qu'il expose.

Le défaut n'est pas une commodité, c'est la seule configuration sûre tant que
l'authentification n'existe pas. Le jour où elle existera, ce garde devra être
desserré EN MÊME TEMPS, pas avant.

Usage :
    .venv/bin/python -m scripts.analytics_serve
    .venv/bin/python -m scripts.analytics_serve --port 8900
    .venv/bin/python -m scripts.analytics_serve --db data/valuebet.db

Depuis une autre machine, SANS exposer quoi que ce soit :
    ssh -N -L 8899:127.0.0.1:8899 utilisateur@la-vm
    puis http://127.0.0.1:8899/docs dans le navigateur.
"""
from __future__ import annotations

import argparse
import ipaddress
import os
import sys
from pathlib import Path

RACINE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RACINE))

from src.analytics.service import DB_DEFAUT  # noqa: E402
from src.analytics_api.app import VAR_CORS, VAR_DB  # noqa: E402

HOTE_DEFAUT = "127.0.0.1"
PORT_DEFAUT = 8899


def _est_bouclage(hote: str) -> bool:
    """Vrai pour 127.0.0.1, ::1 et « localhost ». Tout le reste est du réseau.

    On passe par `ipaddress` plutôt que par une comparaison de chaînes :
    « 127.0.0.2 » et « 127.1 » sont aussi du bouclage, et un test de chaîne
    les refuserait pour rien tout en laissant passer des écritures exotiques
    de 0.0.0.0."""
    if hote.lower() in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(hote).is_loopback
    except ValueError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default=HOTE_DEFAUT,
                    help=f"Adresse d'écoute (défaut {HOTE_DEFAUT}).")
    ap.add_argument("--port", type=int, default=PORT_DEFAUT)
    ap.add_argument("--db", default=None,
                    help=f"Chemin de la base (défaut : ${VAR_DB} ou {DB_DEFAUT}).")
    ap.add_argument("--public", action="store_true",
                    help="Autoriser une écoute HORS bouclage. À n'utiliser "
                         "que derrière un reverse proxy qui, lui, authentifie.")
    ap.add_argument("--reload", action="store_true",
                    help="Rechargement à chaud (développement).")
    a = ap.parse_args()

    if not _est_bouclage(a.host) and not a.public:
        ap.error(
            f"Refus d'écouter sur {a.host} : l'API n'a AUCUNE "
            f"authentification aujourd'hui, et cette adresse l'exposerait au "
            f"réseau avec tout l'historique des détections, des paris joués "
            f"et des résultats.\n"
            f"  • Pour y accéder depuis une autre machine sans rien exposer :\n"
            f"      ssh -N -L {a.port}:127.0.0.1:{a.port} utilisateur@cette-vm\n"
            f"  • Si un reverse proxy authentifie déjà devant, "
            f"ajoute --public.")

    base = a.db or os.getenv(VAR_DB) or DB_DEFAUT
    chemin = Path(base)
    if not chemin.exists():
        # ⚠️ Échouer ICI plutôt qu'à la première requête. Une API qui démarre
        # et rend « 0 opportunité » se lit comme « aucun pari », pas comme
        # « mauvais chemin ».
        ap.error(f"Base introuvable : {chemin.resolve()}. "
                 f"Lance depuis la racine du projet, ou nomme-la avec --db.")
    os.environ[VAR_DB] = str(chemin)

    print(f"Valuebet Analytics — http://{a.host}:{a.port}")
    print(f"  base       : {chemin}  (LECTURE SEULE, mode=ro)")
    print(f"  docs       : http://{a.host}:{a.port}/docs")
    if not _est_bouclage(a.host):
        print("  ⚠️ ÉCOUTE PUBLIQUE, SANS AUTHENTIFICATION — "
              "vérifie qu'un proxy authentifie devant.")
    if os.getenv(VAR_CORS):
        print(f"  CORS       : {os.environ[VAR_CORS]}")

    import uvicorn
    uvicorn.run("src.analytics_api.app:app", host=a.host, port=a.port,
                reload=a.reload, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
