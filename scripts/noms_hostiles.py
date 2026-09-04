#!/usr/bin/env python3
"""Quels noms auraient cassé le HTML des alertes Telegram ?

POURQUOI CETTE SONDE EXISTE
---------------------------
Le 04/09, les cinq formateurs d'alerte interpolaient des noms venus des flux
DIRECTEMENT dans du HTML, avec `parse_mode=HTML`. Un seul `&` nu fait refuser
le message ENTIER par Telegram (`400 can't parse entities`) — et l'échec est
PERMANENT : `send_value_bet` ne marque un pari notifié que si l'envoi réussit,
donc un pari au nom cassé est réessayé à chaque cycle, indéfiniment, en payant
sa pause de `min_send_interval_s`. Ces paris s'accumulent pendant que les
envois valides sortent de la file.

L'échappement corrige la classe entière. Cette sonde répond à l'autre
question, celle que le correctif ne répond pas : **QUEL nom, et venu d'OÙ ?**
C'est ce qui dit si rallumer un book est sûr, et ce qui date le début de la
panne.

⚠️ ELLE NE PROUVE PAS LA CAUSE. Un nom hostile en base n'a cassé une alerte
que si un pari a réellement été routé sur ce match. Elle donne les suspects,
pas le coupable — et zéro suspect est une réfutation, elle.

Usage :
    .venv/bin/python -m scripts.noms_hostiles
    .venv/bin/python -m scripts.noms_hostiles --base data/valuebet.db
"""
from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
BASE = RACINE / "data" / "valuebet.db"

# Ce que Telegram refuse : un `&` qui n'ouvre pas une entité connue, ou un `<`.
RE_AMP = re.compile(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)")

#: (table, colonne à examiner, colonne de contexte)
CIBLES = [
    ("events", "league", "sport"),
    ("events", "home", "league"),
    ("events", "away", "league"),
    ("teams", "display_name", "last_seen_at"),
]


def hostile(v: str | None) -> str | None:
    """Le motif qui casse, ou None. Rend le CARACTÈRE fautif, pas un booléen :
    savoir que c'est un `&` ou un `<` change le diagnostic."""
    if not v:
        return None
    if RE_AMP.search(v):
        return "& nu"
    if "<" in v:
        return "<"
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default=str(BASE), help=f"Défaut : {BASE}")
    ap.add_argument("--max", type=int, default=15, metavar="N",
                    help="Exemples affichés par colonne (défaut 15).")
    a = ap.parse_args()

    chemin = Path(a.base)
    if not chemin.exists():
        raise SystemExit(f"Base introuvable : {chemin}")

    con = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    print(f"\nNOMS QUI AURAIENT CASSÉ LE HTML DES ALERTES — {chemin}")
    total = 0
    for table, col, ctx in CIBLES:
        try:
            rows = con.execute(
                f"SELECT DISTINCT {col}, {ctx} FROM {table}").fetchall()
        except sqlite3.Error as e:
            print(f"\n  {table}.{col} : illisible ({e})")
            continue
        mauvais = [(v, c, m) for v, c in rows if (m := hostile(v))]
        total += len(mauvais)
        print(f"\n  {table}.{col} : {len(mauvais)} sur {len(rows)} valeurs "
              f"distinctes")
        for v, c, motif in sorted(mauvais)[:a.max]:
            print(f"    [{motif:5}] {v}   ({ctx}={c})")
        if len(mauvais) > a.max:
            print(f"    … et {len(mauvais) - a.max} autres")
    con.close()

    print()
    if total:
        print(f"  {total} valeur(s) hostiles. Chacune aurait fait refuser TOUT "
              f"message la citant,\n  aussi longtemps qu'elle serait restée "
              f"dans la file — l'échappement les rend\n  inoffensives, mais "
              f"c'est bien de ce vivier que la panne est sortie.")
    else:
        print("  Aucune. Le vivier est vide AUJOURD'HUI, ce qui ne dit rien de "
              "ce qu'il\n  contenait pendant la panne : `events` ne garde que "
              "les matchs connus, et\n  un match joué en sort. Réfutation "
              "partielle, pas preuve d'innocence.")
    print("\nLecture seule — base ouverte en mode read-only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
