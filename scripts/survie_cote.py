#!/usr/bin/env python3
"""Combien de temps un prix annoncé survit-il avant de bouger ?

POURQUOI CETTE SONDE
--------------------
« Je ne prends la cote Ladbrokes qu'une fois sur cinq » : le prix annoncé dans
l'alerte n'est plus là quand on va le jouer. Toute la question est de savoir
DANS QUELLE FENÊTRE il meurt, parce que c'est elle qui décide si accélérer le
daemon sert à quelque chose.

La chaîne complète, mesurée le 21/09 sur la production :

    attente du prochain fetch   0 → 18 s   (cycle 8,0 s + breather 10 s)
    âge de la cote à l'alerte   3 → 8 s    (le fetch Ladbrokes est séquentiel)
    réaction humaine            ~20 s
    ────────────────────────────────────
    total                       ~34 s en moyenne, ~56 s au pire

Retirer le breather ramènerait la moyenne à ~29 s ; paralléliser en plus le
fetch la ramènerait à ~25 s. **Le plafond technique est donc de l'ordre de
neuf secondes sur trente-quatre.** Ces neuf secondes ne valent quelque chose
que si les prix meurent précisément dans cette bande — et c'est ce que cette
sonde répond, au lieu de le supposer.

CE QU'ELLE LIT, ET CE QU'ELLE NE PEUT PAS DIRE
----------------------------------------------
`odds_history` enregistre la trajectoire d'une sélection suivie : une ligne
par changement de prix, jamais une par cycle (écriture parcimonieuse). La
survie mesurée est donc le délai jusqu'au PREMIER prix strictement inférieur
à celui annoncé.

⚠️ Elle mesure le prix AFFICHÉ, pas le prix OBTENU. Rien n'enregistre ce que
le bookmaker a réellement accordé (§25.11 pt 1) : un pari refusé ou une cote
qui change à la validation ne se voient pas ici.

⚠️ Un pari SANS trajectoire est exclu, jamais compté comme survivant. Le
compter gonflerait le taux de survie de tous les paris qu'on n'a pas suivis.

⚠️ La baisse observée peut être postérieure au pari. La sonde dit quand le
prix a bougé, pas si l'utilisateur était encore en train de le saisir.

Usage :
    .venv/bin/python -m scripts.survie_cote
    .venv/bin/python -m scripts.survie_cote --book unibet_be --jours 14
    .venv/bin/python -m scripts.survie_cote --tous
"""
from __future__ import annotations

import argparse
import sqlite3
import sys

#: Les paliers imprimés. Choisis autour de la chaîne mesurée ci-dessus :
#: 30 s est le total actuel moins la réaction, 60 s le pire cas réaliste.
PALIERS = (5, 10, 15, 30, 45, 60, 120, 300)

REQUETE = """
WITH vb AS (
    -- ⚠️ `book` est porté PAR la CTE. Le rejoindre depuis `value_bets` plus
    -- bas marcherait aussi, mais `--tous` compare des books ligne par ligne :
    -- la moindre jointure de trop y devient une occasion de comparer la
    -- trajectoire d'un book au prix d'un autre.
    SELECT id, book, odd_taken, detected_at
    FROM value_bets
    WHERE detected_at >= datetime('now', ?)
      {filtre_book}
),
chute AS (
    -- Le PREMIER instant où la cote suivie passe SOUS le prix annoncé.
    SELECT vb.id,
           (julianday(MIN(oh.seen_at)) - julianday(vb.detected_at)) * 86400.0
               AS survie_s
    FROM vb
    JOIN odds_history oh
      ON oh.value_bet_id = vb.id
     AND oh.book = vb.book
     AND oh.odd < vb.odd_taken
    GROUP BY vb.id
),
suivis AS (
    SELECT DISTINCT vb.id
    FROM vb
    JOIN odds_history oh
      ON oh.value_bet_id = vb.id AND oh.book = vb.book
)
SELECT (SELECT COUNT(*) FROM suivis),
       (SELECT COUNT(*) FROM chute),
       (SELECT COUNT(*) FROM vb)
"""


def _colonne_palier(seuil: int) -> str:
    return (f"(SELECT COUNT(*) FROM chute WHERE survie_s <= {int(seuil)})")


def mesurer(con: sqlite3.Connection, book: str | None, jours: float) -> dict:
    filtre = "AND book = ?" if book else ""
    sql = REQUETE.format(filtre_book=filtre)
    sql += "".join(f",\n       {_colonne_palier(p)}" for p in PALIERS)
    params: list = [f"-{jours} days"]
    if book:
        params.append(book)
    ligne = con.execute(sql, params).fetchone()
    suivis, ont_baisse, total = ligne[0], ligne[1], ligne[2]
    return {
        "book": book or "tous",
        "total": total, "suivis": suivis, "ont_baisse": ont_baisse,
        "paliers": dict(zip(PALIERS, ligne[3:])),
    }


def afficher(r: dict) -> None:
    print(f"\n  {r['book']}")
    print(f"    {r['total']} détections · {r['suivis']} avec trajectoire "
          f"· {r['ont_baisse']} ont baissé")
    if not r["suivis"]:
        # ⚠️ Dire POURQUOI il n'y a rien. « 0 » tout seul se lit « le prix ne
        # bouge jamais », l'exact contraire de « on n'a rien suivi ».
        print("    Aucune trajectoire suivie — rien de mesurable ici, et ce "
              "n'est PAS\n    un signe que les prix tiennent.")
        return
    print()
    print("      palier   perdus   part      lecture")
    print("      " + "-" * 56)
    for seuil, n in r["paliers"].items():
        part = 100.0 * n / r["suivis"]
        barre = "█" * int(round(part / 4))
        print(f"      {seuil:4d} s   {n:6d}   {part:5.1f} %   {barre}")


def main() -> int:
    # ⚠️ `--help` AVANT toute ouverture de base : la sonde doit répondre même
    # sans base, sur une machine de développement (§4.1, tests/test_sondes_help).
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--book", default="ladbrokes_be", metavar="NOM",
                    help="Book mesuré. Défaut : ladbrokes_be.")
    ap.add_argument("--tous", action="store_true",
                    help="Tous les books d'un coup, pour comparer.")
    ap.add_argument("--jours", type=float, default=7, metavar="N",
                    help="Fenêtre d'analyse en jours. Défaut : 7.")
    a = ap.parse_args()

    try:
        con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
        con.execute("SELECT 1 FROM odds_history LIMIT 1")
    except sqlite3.Error as e:
        print(f"Base illisible ({a.db}) : {e}", file=sys.stderr)
        return 1

    print(f"SURVIE DU PRIX ANNONCÉ — {a.jours:g} derniers jours")
    print("Délai entre la détection et le premier prix INFÉRIEUR à celui "
          "annoncé.")

    if a.tous:
        books = [r[0] for r in con.execute(
            "SELECT DISTINCT book FROM value_bets "
            "WHERE detected_at >= datetime('now', ?) ORDER BY book",
            (f"-{a.jours} days",))]
        for b in books:
            afficher(mesurer(con, b, a.jours))
    else:
        afficher(mesurer(con, a.book, a.jours))

    print("\n  COMMENT LIRE")
    print("    Beaucoup de perdus dès 15-30 s → le prix meurt avant qu'on")
    print("      puisse le jouer ; accélérer le daemon n'y changera rien.")
    print("    Peu à 30 s, beaucoup à 120 s → on est dans la bonne fenêtre,")
    print("      et les ~9 s que le daemon peut gagner comptent vraiment.")
    print("    Peu partout → les prix tiennent, et le « 1 sur 5 » vient")
    print("      d'ailleurs que de la fraîcheur.")
    print("\n  Lecture seule — aucune écriture, aucun réglage modifié.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
