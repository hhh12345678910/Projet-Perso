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


# Un exemple collé tel quel produit un « Invalid userid or password »
# incompréhensible, alors que la vraie cause est un copier-coller. La
# première version listait des chaînes exactes : elle a laissé passer
# `ton_identifiant_asianodds` parce qu'elle ne connaissait que
# `ton_identifiant`. Une liste ne rattrapera jamais toutes les variantes —
# c'est le PRÉFIXE possessif qui est le signal, pas la chaîne entière.
_PREFIXES_EXEMPLE = ("ton_", "ton ", "ta_", "ta ", "tes_", "votre_", "votre ",
                     "vos_", "mon_", "mon ", "ma_", "mes_",
                     "your_", "your ", "my_", "my ")
_VALEURS_EXEMPLE = {
    "mot_de_passe", "mot de passe", "motdepasse", "mdp", "identifiant",
    "login", "user", "username", "password", "passwd", "pass",
    "le_vrai", "xxx", "xxxx", "...", "changeme", "todo",
}


def est_un_exemple(valeur: str) -> bool:
    """La valeur est-elle un placeholder plutôt qu'un vrai identifiant ?

    Faux positif possible : quelqu'un dont le mot de passe commencerait par
    « ton_ ». Le coût est un message d'erreur explicite ; le coût de l'inverse
    est un « Invalid userid or password » qu'on met dix minutes à comprendre.
    """
    v = valeur.strip().lower()
    if not v:
        return True
    if valeur.startswith("<") and valeur.endswith(">"):
        return True
    return v in _VALEURS_EXEMPLE or v.startswith(_PREFIXES_EXEMPLE)


#: Rien à remplacer dedans : c'est tout l'intérêt. `-s` sur le mot de passe
#: pour qu'il ne s'affiche pas et n'entre pas dans ~/.bash_history.
INVITE_SAISIE = (
    "  read -rp  'Identifiant AsianOdds : ' AO_USER && export AO_USER\n"
    "  read -rsp 'Mot de passe AsianOdds : ' AO_PASS && export AO_PASS && echo"
)


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
    p.add_argument("--diagnostic", metavar="FICHIER",
                   help="écrit les DEUX listes COMPLETES dans ce fichier. "
                        "Le résumé console est tronqué à 15 lignes, ce qui "
                        "suffit pour trancher « couverture ou rapprochement » "
                        "mais pas pour vérifier un match précis.")
    a = p.parse_args()

    user, pwd = os.environ.get("AO_USER"), os.environ.get("AO_PASS")
    if not user or not pwd:
        print(f"ERREUR : AO_USER et AO_PASS ne sont pas définis.\n"
              f"{INVITE_SAISIE}", file=sys.stderr)
        return 2
    for nom, valeur in (("AO_USER", user), ("AO_PASS", pwd)):
        if est_un_exemple(valeur):
            print(f"ERREUR : {nom} vaut {valeur!r}, qui est un exemple et non "
                  f"ta vraie valeur.\n"
                  f"Saisis les deux sans rien avoir à remplacer :\n"
                  f"{INVITE_SAISIE}", file=sys.stderr)
            return 2

    debut = datetime.now(timezone.utc)
    print(f"[ao] démarrage {debut.isoformat()} "
          f"({'À BLANC' if a.dry_run else 'ÉCRITURE'}, {a.minutes:.0f} min)")

    stats = collect(Storage(a.db), user, pwd,
                    duration_sec=a.minutes * 60,
                    sport=a.sport, dry_run=a.dry_run)

    duree = (datetime.now(timezone.utc) - debut).total_seconds()
    demande = a.minutes * 60
    print(f"[ao] terminé en {duree:.0f} s")
    print(f"[ao] {stats.resume()}")
    print(f"[ao] {stats.couverture()}")
    from src.asianodds_live import diagnostic_appariement
    if a.dry_run:
        print(diagnostic_appariement(stats))
    if a.diagnostic:
        with open(a.diagnostic, "w", encoding="utf-8") as f:
            f.write(diagnostic_appariement(stats, limite=None) + "\n")
        print(f"[ao] listes complètes écrites dans {a.diagnostic}")
    # Un run vide n'est pas un run reussi. Il sortait en code 0, avec un
    # « 0.0 % » qui accusait la couverture d'AsianOdds alors que le flux
    # n'avait rien envoye du tout.
    if stats.evf == 0:
        print(f"[ao] ✖ ÉCHEC : aucune cote reçue en {duree:.0f} s.\n"
              f"[ao]   Ce n'est PAS un résultat de couverture — le flux n'a "
              f"rien coté.\n"
              f"[ao]   fin : {stats.fin_raison}\n"
              f"[ao]   types reçus : {stats.types_recus()}", file=sys.stderr)
        return 1
    if demande and duree < 0.8 * demande:
        print(f"[ao] ⚠ arrêt après {duree:.0f} s sur {demande:.0f} s "
              f"demandées — {stats.fin_raison}. Les chiffres ci-dessus ne "
              f"portent que sur cette fraction.", file=sys.stderr)

    # L'avertissement portait sur le taux BRUT, dont le dénominateur contient
    # des matchs terminés : il criait « moins de la moitié » alors que le taux
    # honnête était de 74 %. Il ne se déclenche plus que sur ce dernier.
    from src.asianodds_live import plausiblement_en_jeu
    en_jeu = {c.event_key for c in plausiblement_en_jeu(
        stats.derniers_candidats, datetime.now(timezone.utc))}
    if en_jeu:
        couv = len(en_jeu & stats.evenements_couverts)
        if couv / len(en_jeu) < 0.5:
            print(f"[ao] ⚠ AsianOdds ne couvre que {couv}/{len(en_jeu)} de nos "
                  f"matchs réellement en jeu : c'est ce taux-là qui limite le "
                  f"moteur LIVE.")
    if stats.reconnexions:
        print(f"[ao] {stats.reconnexions} reprise(s) après coupure du flux.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
