#!/usr/bin/env python3
"""Pourquoi la clôture manque, et est-ce que ça dépend du DÉLAI de détection ?

POURQUOI CETTE SONDE
--------------------
`clv_roi_matrix --axe delai` a mesuré que le taux de capture de la clôture
DÉPEND du délai : sur le football, canal premium, 69,6 % des opportunités
détectées à moins de 24 h ont une ligne de clôture, contre 63,2 % au-delà.
Six points d'écart. Si les clôtures manquantes ne sont pas un échantillon
aléatoire de leur bande, une partie du déficit de CLV des bandes lointaines
est un artefact de mesure et non un fait sur le marché.

L'HYPOTHÈSE, ET SON MÉCANISME
-----------------------------
`Storage.closing_group` (storage.py:1825) cherche la cote de clôture avec
`WHERE event_key = ?` — une égalité EXACTE. Or `event_key` vaut
`YYYYMMDDHHMM::home__vs__away` (matcher.py:190) : **la minute du coup d'envoi
est dans la clé**. La clé d'un pari est figée à sa détection ; celle des cotes
capturées près du coup d'envoi porte l'horaire révisé. Si Pinnacle a déplacé
l'heure entre les deux, l'égalité échoue et la clôture est perdue.

Ce n'est pas une hypothèse en l'air : `Storage._event_key_like`
(storage.py:1293) existe précisément pour ça — « Pinnacle sometimes adjusts a
match's start time by a few minutes » — et `matcher.py:213` admet **trois
heures** de tolérance parce qu'au tennis « un match commence quand le
précédent sur le court se termine ». Mais cette clé tolérante n'est utilisée
que dans les six fonctions de déduplication d'alertes. **Jamais dans la
capture de clôture.**

Et la probabilité qu'un horaire ait bougé CROÎT avec le délai : un match vu à
200 h a bien plus d'occasions d'être replacé qu'un match vu à 1 h. C'est
exactement la forme de l'écart observé.

CE QUE LA SONDE MESURE — ET SON TÉMOIN
--------------------------------------
Pour chaque opportunité : a-t-elle une clôture, et sa (date + équipes)
existe-t-elle dans `events` sous PLUSIEURS clés — la signature d'un horaire
déplacé ? Puis la comparaison qui décide :

    part des paris à horaire déplacé PARMI CEUX SANS clôture
    part des paris à horaire déplacé PARMI CEUX AVEC clôture

**Sans ce témoin la mesure ne vaut rien** : si les deux parts sont égales, le
déplacement d'horaire n'explique rien, quel que soit son niveau absolu.

⚠️ `events` n'est jamais purgé (contrairement à `quotes`, deux jours), donc ce
test porte sur TOUT l'historique. Mais il ne voit un déplacement que si le
daemon a bien créé une ligne `events` pour la nouvelle clé — un déplacement
survenu alors que l'événement n'était plus scanné reste invisible ici, et la
sonde SOUS-ESTIME donc le phénomène.

Usage :
    .venv/bin/python -m scripts.closing_gap --premium
    .venv/bin/python -m scripts.closing_gap --premium --sport soccer
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import load_env_file  # noqa: E402
from scripts.clv_roi_matrix import BANDES_DELAI, _bande_delai  # noqa: E402
from scripts.pnl_detections import porte_de_canal  # noqa: E402

ORDRE = ["< 0 (LIVE)"] + [l for l, _, _ in BANDES_DELAI] + ["? (sans horaire)"]


def _groupe_tolerant(cle: str) -> tuple:
    """(jour, équipes) — la clé tolérante de `_event_key_like`, en Python.

    `YYYYMMDDHHMM::home__vs__away` → `("YYYYMMDD", "home__vs__away")`. Deux
    clés du même groupe ne diffèrent que par la MINUTE du coup d'envoi : c'est
    la signature d'un horaire déplacé, pas de deux matchs différents (les
    mêmes équipes ne se rencontrent pas deux fois le même jour)."""
    if "::" not in cle:
        return (cle, "")
    tete, reste = cle.split("::", 1)
    return (tete[:8], reste)


def _prop_diff(a_ok: int, a_n: int, b_ok: int, b_n: int):
    """(part A, part B, écart A−B, z) — différence de deux proportions.

    ⚠️ L'ORDRE DES ARGUMENTS PORTE LE SENS DE L'HYPOTHÈSE. Les appelants
    passent le lot SANS clôture en premier, parce que l'hypothèse est « ceux
    qui n'ont pas de clôture sont PLUS souvent déplacés » : un écart positif la
    soutient. La première version passait le lot AVEC en premier tout en
    gardant les verdicts écrits pour l'autre sens — les deux branches de
    conclusion étaient donc INVERSÉES, et la sonde aurait annoncé « hypothèse
    écartée » précisément quand elle est confirmée. Trouvé en construisant un
    jeu d'essai où l'association est plantée."""
    if a_n == 0 or b_n == 0:
        return None, None, None, None
    pa, pb = a_ok / a_n, b_ok / b_n
    p = (a_ok + b_ok) / (a_n + b_n)
    v = p * (1 - p) * (1 / a_n + 1 / b_n)
    return pa, pb, pa - pb, ((pa - pb) / math.sqrt(v) if v > 0 else None)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--premium", action="store_true",
                    help="Restreindre à la porte RÉELLE du canal premium.")
    ap.add_argument("--canal", default=None, metavar="NOM",
                    help="Un autre canal, par son nom exact (implique --premium).")
    ap.add_argument("--sport", default=None,
                    help="Un seul sport (le football porte 74 %% du volume).")
    a = ap.parse_args()
    if a.canal:
        a.premium = True
    load_env_file()

    porte = None
    desc = "aucune — toutes les détections"
    if a.premium:
        porte, desc = porte_de_canal(a.db, a.canal)

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    # `closing_lost` est une colonne de migration (storage.py:403) : une base
    # ancienne ne l'a pas. Refuser de tourner serait absurde, l'inventer serait
    # pire — on la lit si elle existe et on le DIT sinon.
    colonnes = {r[1] for r in con.execute("PRAGMA table_info(value_bets)")}
    a_perdu = "closing_lost" in colonnes
    champ_perdu = ("COALESCE(vb.closing_lost, 0)" if a_perdu else "0")

    # Tous les groupes tolérants de `events`, avec leur nombre de clés
    # DISTINCTES. C'est la seule table qui garde l'historique complet.
    par_groupe: dict[tuple, set] = defaultdict(set)
    for (cle,) in con.execute("SELECT event_key FROM events"):
        par_groupe[_groupe_tolerant(cle)].add(cle)

    rows = list(con.execute(f"""
        SELECT vb.id, vb.event_key, vb.book, vb.market, vb.outcome_label,
               vb.line, vb.odd_taken, vb.fair_odd, vb.ev_pct, vb.detected_at,
               {champ_perdu} AS closing_lost,
               e.sport AS sport, e.league AS league, e.start_time AS start_time,
               (cs.id IS NOT NULL) AS a_cloture
        FROM value_bets vb
        LEFT JOIN clv_snapshots cs
               ON cs.value_bet_id = vb.id AND cs.closing = 1
        LEFT JOIN events e ON e.event_key = vb.event_key
    """))
    if not rows:
        raise SystemExit("Aucune détection en base.")

    gardees = [r for r in rows
               if (a.sport is None or (r["sport"] or "") == a.sport)
               and (porte is None or porte(r))]
    if not gardees:
        raise SystemExit("Aucune détection ne passe ce filtre.")

    print(f"\nÉCART DE CAPTURE DE LA CLÔTURE — porte : {desc}")
    print(f"Sport : {a.sport or 'tous'}   ·   {len(gardees)} opportunités")
    if not a_perdu:
        print("⚠️ Colonne `closing_lost` absente de cette base : la colonne "
              "« perdue » lira 0 partout.")

    def deplace(r) -> bool:
        return len(par_groupe.get(_groupe_tolerant(r["event_key"]), ())) > 1

    par_bande: dict[str, list] = defaultdict(list)
    for r in gardees:
        par_bande[_bande_delai(r)].append(r)

    # « déplacé SANS » et « déplacé AVEC » : la part des horaires déplacés dans
    # chacun des deux lots. C'est leur ÉCART qui décide, pas leur niveau.
    ent = (f"{'délai':16} {'opp':>6} {'clôture':>8} {'%':>6}   "
           f"{'perdue':>6}   {'horaire déplacé':>16}   "
           f"{'dépl.SANS':>9} {'dépl.AVEC':>9} {'z':>6}")
    print()
    print(ent)
    print("-" * len(ent))
    for lab in ORDRE + sorted(set(par_bande) - set(ORDRE)):
        sub = par_bande.get(lab)
        if not sub:
            continue
        n = len(sub)
        avec = [r for r in sub if r["a_cloture"]]
        sans = [r for r in sub if not r["a_cloture"]]
        perdue = sum(1 for r in sub if r["closing_lost"])
        dep = sum(1 for r in sub if deplace(r))
        # SANS d'abord : voir `_prop_diff`.
        p_sans, p_avec, _d, z = _prop_diff(
            sum(1 for r in sans if deplace(r)), len(sans),
            sum(1 for r in avec if deplace(r)), len(avec))
        f = lambda v: "—" if v is None else f"{100 * v:5.1f}%"  # noqa: E731
        print(f"{lab:16} {n:6} {len(avec):8} {100 * len(avec) / n:5.1f}%   "
              f"{perdue:6}   {dep:6} {100 * dep / n:8.1f}%   "
              f"{f(p_sans):>7} {f(p_avec):>7} "
              f"{('—' if z is None else f'{z:+.2f}'):>6}")

    print("\nLE TÉMOIN, TOUTES BANDES CONFONDUES")
    print("-" * len(ent))
    avec = [r for r in gardees if r["a_cloture"]]
    sans = [r for r in gardees if not r["a_cloture"]]
    p_sans, p_avec, d, z = _prop_diff(
        sum(1 for r in sans if deplace(r)), len(sans),
        sum(1 for r in avec if deplace(r)), len(avec))
    if p_sans is None:
        print("  Incalculable : un des deux lots est vide.")
    else:
        print(f"  horaire déplacé PARMI CEUX SANS clôture : {100 * p_sans:5.2f} % "
              f"(n={len(sans)})")
        print(f"  horaire déplacé PARMI CEUX AVEC clôture : {100 * p_avec:5.2f} % "
              f"(n={len(avec)})")
        print(f"  écart {100 * d:+.2f} points (SANS − AVEC)   z = "
              f"{'—' if z is None else f'{z:+.2f}'}")
        if z is None or abs(z) < 2:
            print("\n  → Les deux parts ne se distinguent PAS. Le déplacement "
                  "d'horaire n'explique\n    pas le déficit de capture : "
                  "l'hypothèse de la clé exacte est ÉCARTÉE, et le\n    déficit "
                  "reste à expliquer (marché trop mince pour être dévigué, "
                  "aucune cote\n    Pinnacle avant le coup d'envoi).")
        elif z > 0:
            print("\n  → Les paris SANS clôture sont PLUS souvent déplacés : "
                  "l'hypothèse de la clé\n    exacte de `closing_group` "
                  "(storage.py:1848) est SOUTENUE. Câbler la clé tolérante\n"
                  "    `_event_key_like` dans `closing_group` récupérerait ces "
                  "clôtures.")
        else:
            print("\n  → Les paris SANS clôture sont MOINS souvent déplacés. "
                  "C'est l'INVERSE de\n    l'hypothèse : elle est écartée, et "
                  "le déficit vient d'ailleurs.")

    print("\n⚠️ Cette sonde ne voit un déplacement que si le daemon a créé une "
          "ligne `events`\n   pour la nouvelle clé. Un horaire déplacé alors "
          "que l'événement n'était plus\n   scanné reste invisible : le "
          "phénomène est SOUS-ESTIMÉ, jamais surestimé.")
    print("\nLecture seule — aucune écriture, aucun réglage modifié.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
