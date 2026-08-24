"""Collecteur LIVE AsianOdds — lanceur autonome. §PHASE 4

DÉLIBÉRÉMENT HORS DE `main.py` ET HORS DE systemd. Tant que ce collecteur
n'a pas tourné assez longtemps pour qu'on connaisse son taux d'appariement et
son impact réel sur SQLite, il ne doit pas pouvoir démarrer par accident avec
le daemon prématch.

    export AO_USER=... AO_PASS=...

    # 1. À blanc : mesure le taux d'appariement, n'écrit RIEN.
    .venv/bin/python -m scripts.live_asianodds --minutes 5 --dry-run

    # 2. Écriture réelle, une fois le taux jugé acceptable.
    .venv/bin/python -m scripts.live_asianodds --minutes 30

Le mode --dry-run est le mode par défaut de la première utilisation : il
répond à « combien de matchs AsianOdds retrouve-t-on chez nous », qui est la
seule question qui décide si ce flux vaut quelque chose pour EQUODDS.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from src.asianodds_live import SPORT_FOOTBALL, collect
from src.storage import Storage


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--minutes", type=float, default=5.0,
                   help="durée de collecte (défaut : 5)")
    p.add_argument("--db", default="data/valuebet.db")
    p.add_argument("--sport", type=int, default=SPORT_FOOTBALL,
                   help="1=foot 2=basket 3=tennis (défaut : 1)")
    p.add_argument("--dry-run", action="store_true",
                   help="normalise et rapproche sans écrire une seule ligne")
    a = p.parse_args()

    user, pwd = os.environ.get("AO_USER"), os.environ.get("AO_PASS")
    if not user or not pwd:
        print("ERREUR : exporte AO_USER et AO_PASS.", file=sys.stderr)
        return 2
    # Un exemple collé tel quel produit un « Invalid userid or password »
    # incompréhensible, alors que la vraie cause est un copier-coller. Le
    # motif générique <...> attrape n'importe quel placeholder ; la liste ne
    # sert que pour ceux qui n'en portent pas.
    for nom, valeur in (("AO_USER", user), ("AO_PASS", pwd)):
        exemple = (valeur.startswith("<") and valeur.endswith(">")) or \
            valeur.strip().lower() in {
                "ton_mot_de_passe", "ton mot de passe", "mot_de_passe",
                "ton_nouveau_mot_de_passe", "le_vrai", "password", "xxxx",
                "...", "ton_identifiant", "ton_mdp"}
        if exemple:
            print(f"ERREUR : {nom} vaut {valeur!r}, qui est un exemple et non "
                  f"ta vraie valeur.\n"
                  f"Pour éviter de le retaper en clair :\n"
                  f"  read -rsp 'Mot de passe AsianOdds : ' AO_PASS && "
                  f"export AO_PASS && echo", file=sys.stderr)
            return 2

    debut = datetime.now(timezone.utc)
    print(f"[ao] démarrage {debut.isoformat()} "
          f"({'À BLANC' if a.dry_run else 'ÉCRITURE'}, {a.minutes:.0f} min)")

    stats = collect(Storage(a.db), user, pwd,
                    duration_sec=a.minutes * 60,
                    sport=a.sport, dry_run=a.dry_run)

    duree = (datetime.now(timezone.utc) - debut).total_seconds()
    print(f"[ao] terminé en {duree:.0f} s")
    print(f"[ao] {stats.resume()}")
    print(f"[ao] {stats.couverture()}")
    if a.dry_run:
        from src.asianodds_live import diagnostic_appariement
        print(diagnostic_appariement(stats))
    couv, cand = len(stats.evenements_couverts), stats.candidats_connus
    if cand and couv / cand < 0.5:
        print("[ao] ⚠ AsianOdds couvre moins de la moitié de NOS événements "
              "en cours : c'est ce taux-là qui limite le moteur LIVE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
