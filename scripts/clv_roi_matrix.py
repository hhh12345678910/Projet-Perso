#!/usr/bin/env python3
"""CLV **et** ROI dans la même table, par sport et par tranche de cote.

POURQUOI CET OUTIL
------------------
Le §21.17 a trouvé que la CLV et le P&L se contredisent sur les grosses cotes.
Le vérifier demandait jusqu'ici deux commandes, deux populations et deux
fichiers : `clv_split --by cote` d'un côté, `pnl_detections` de l'autre. Les
deux tables ne se superposent que si les bornes sont identiques — d'où
l'import de `BANDES_COTE` plutôt qu'une copie.

⚠️ LES DEUX MESURES NE PORTENT PAS SUR LA MÊME POPULATION, et c'est pour ça
que chaque colonne porte SON effectif :

* la **CLV** exige une clôture capturée (`clv_snapshots.closing = 1`). Un match
  dont la ligne de clôture a été manquée n'en a pas, définitivement — les
  cotes sont purgées à deux jours (§7) ;
* le **ROI** exige un résultat dans `results`, donc une source de scores.

Un `n_clv` très inférieur au `n_regles` (ou l'inverse) n'est pas une anomalie :
c'est ce que ces deux chaînes couvrent, et le voir vaut mieux que de comparer
deux moyennes calculées sur des matchs différents en croyant les opposer.

FILTRE DE BOOKS
---------------
`--books` accepte les noms de la base et l'alias **`kambi`**, qui se déplie en
Unibet + 711 + Bingoal + Scooore — le groupe est lu dans `reference.KAMBI_BOOKS`,
jamais recopié. Le filtre s'applique AVANT la déduplication : « le meilleur
prix parmi les books que je joue vraiment », et non le meilleur prix du marché.

AXE DES LIGNES
--------------
`--axe cote` (défaut) découpe par tranche de cote prise. `--axe delai` découpe
par heures entre la détection et le coup d'envoi, et pousse le découpage au
delà des « > 48 h » où le §16.4 s'arrêtait : 48-72, 72-96, 96-120, 120-168,
> 168 h. Sur cet axe la table tous sports confondus est imprimée EN PREMIER,
parce que c'est la seule où le ROI garde un effectif lisible dans les bandes
lointaines.

Usage :
    .venv/bin/python -m scripts.clv_roi_matrix --premium
    .venv/bin/python -m scripts.clv_roi_matrix --premium --axe delai
    .venv/bin/python -m scripts.clv_roi_matrix --premium --books kambi,ladbrokes_be
    .venv/bin/python -m scripts.clv_roi_matrix --premium --books kambi,ladbrokes_be \\
        --out clv_roi.csv
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import statistics as st
import sys
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.clv import clv_pct  # noqa: E402
from src.clv import pnl as clv_pnl  # noqa: E402
from src.clv import settle as clv_settle  # noqa: E402
from src.config import load_env_file  # noqa: E402
from src.reference import KAMBI_BOOKS  # noqa: E402
from scripts.pnl_detections import BANDES_COTE, porte_de_canal  # noqa: E402

_ALIAS = {"kambi": tuple(b.value for b in KAMBI_BOOKS)}


class _VueFairOdd:
    """La même ligne, vue avec `fair_odd` là où la porte lit `odd_taken`.

    C'est le seul moyen de rejouer la porte EXACTE de production sur une autre
    variable sans dupliquer une seule de ses règles (§17.7) — qu'elle vienne
    des canaux en base ou de `TelegramConfig`, elle passe par `__getitem__`.

    ⚠️ Rappel utile pour lire le résultat : `ev = odd_taken / fair_odd - 1`,
    donc sur un value bet `fair_odd < odd_taken` TOUJOURS. Basculer la bande
    de cotes sur la fair odd décale donc chaque pari vers le BAS : une bande
    1,5–4 sur la fair accepte une cote prise allant jusqu'à 4,8 à 20 % d'EV,
    et rejette les cotes prises entre 1,5 et 1,5×(1+EV). Ce n'est pas un
    élargissement uniforme, c'est un glissement.
    """
    __slots__ = ("_r",)

    def __init__(self, r) -> None:
        self._r = r

    def __getitem__(self, k):
        return self._r["fair_odd"] if k == "odd_taken" else self._r[k]


# Le decoupage fin demande par l'utilisateur : le §16.4 s'arretait a « > 48 h »
# sur la CLV, sans jamais savoir ce qu'il y avait dedans. Les bornes suivent les
# journees de calendrier parce que c'est ainsi que les books ouvrent leurs
# marches, puis s'elargissent quand les effectifs fondent.
BANDES_DELAI = [("0-2 h", 0.0, 2.0), ("2-6 h", 2.0, 6.0), ("6-12 h", 6.0, 12.0),
                ("12-24 h", 12.0, 24.0), ("24-48 h", 24.0, 48.0),
                ("48-72 h", 48.0, 72.0), ("72-96 h", 72.0, 96.0),
                ("96-120 h", 96.0, 120.0), ("120-168 h", 120.0, 168.0),
                ("> 168 h", 168.0, 1e9)]


def _heures(brut) -> "float | None":
    if not brut:
        return None
    try:
        d = datetime.fromisoformat(str(brut).replace("Z", "+00:00"))
    except ValueError:
        return None
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()


def _delai_h(row) -> "float | None":
    """Heures entre la DETECTION et le coup d'envoi.

    ⚠️ `detected_at` ne bouge JAMAIS : `insert_value_bet` ne cree qu'une ligne
    par opportunite et rend l'existante sans rien reecrire quand le daemon la
    redetecte (§14.5). Ce delai est donc celui de la PREMIERE detection, pas
    celui de l'instant ou tu aurais mise. Un pari vu a 60 h puis encore present
    a 3 h compte ici en 48-72 h.

    ⚠️ Un delai negatif est une detection LIVE comparee a une ligne prematch
    morte (§9, un tiers des detections a l'epoque). La porte premium est
    prematch, donc ils sont deja ecartes — mais la bande `< 0` existe pour que
    leur presence eventuelle SE VOIE au lieu d'etre repartie en silence."""
    a, b = _heures(row["detected_at"]), _heures(row["start_time"])
    return None if (a is None or b is None) else (b - a) / 3600.0


def _bande(odd: float) -> str:
    for lab, lo, hi in BANDES_COTE:
        if lo <= odd < hi:
            return lab
    return "?"


def _bande_delai(row) -> str:
    h = _delai_h(row)
    if h is None:
        return "? (sans horaire)"
    if h < 0:
        return "< 0 (LIVE)"
    for lab, lo, hi in BANDES_DELAI:
        if lo <= h < hi:
            return lab
    return "?"


# Les deux bandes hors barème existent pour SE VOIR (§11 : le mode de panne
# du projet est le silence). « < 0 » est une detection live, « ? » une ligne
# sans horaire de coup d'envoi ou sans `detected_at` exploitable.
ORDRE_DELAI = (["< 0 (LIVE)"] + [lab for lab, _lo, _hi in BANDES_DELAI]
               + ["? (sans horaire)"])


def _axe(nom: str):
    """(libellé de colonne, fonction de bande, ordre d'affichage)."""
    if nom == "delai":
        return "délai", _bande_delai, ORDRE_DELAI
    return ("tranche", lambda r: _bande(float(r["odd_taken"])),
            [lab for lab, _lo, _hi in BANDES_COTE])


def _books_demandes(brut: str | None) -> set[str] | None:
    if not brut:
        return None
    out: set[str] = set()
    for morceau in (m.strip().lower() for m in brut.split(",")):
        if not morceau:
            continue
        out.update(_ALIAS.get(morceau, (morceau,)))
    return out or None


def _gains(rows: list, stake: float) -> list:
    """Le P&L de chaque pari notable du lot, un par élément.

    Extrait pour que le t de la différence entre deux lots disjoints puisse
    être calculé : `_cellule` n'agrège que des moyennes, et la variance de
    l'écart demande les gains individuels."""
    out = []
    for r in rows:
        statut = clv_settle(r["market"], r["outcome_label"], r["line"],
                            r["winner"], r["home_score"], r["away_score"])
        p = clv_pnl(statut, float(r["odd_taken"]), stake)
        if p is not None:
            out.append(p)
    return out


def _cellule(rows: list, stake: float) -> dict:
    """Les deux mesures d'un groupe, chacune avec SON effectif."""
    matchs = {(r["home"], r["away"], (r["start_time"] or "")[:10]) for r in rows}

    clvs = [clv_pct(float(r["odd_taken"]), float(r["closing_fair_odd"])) * 100.0
            for r in rows
            if r["closing_fair_odd"] and float(r["closing_fair_odd"]) > 0]

    gains, gagnes, perdus, nuls = [], 0, 0, 0
    for r in rows:
        statut = clv_settle(r["market"], r["outcome_label"], r["line"],
                            r["winner"], r["home_score"], r["away_score"])
        p = clv_pnl(statut, float(r["odd_taken"]), stake)
        if p is None:
            continue
        gains.append(p)
        if statut == "won":
            gagnes += 1
        elif statut == "lost":
            perdus += 1
        else:
            nuls += 1

    mise = stake * len(gains)
    ecart = st.stdev(gains) if len(gains) > 1 else 0.0
    # La CLV avait son effectif mais PAS sa precision. C'est pourtant elle qui
    # decide : elle est ~8 fois moins bruitee par pari que le P&L, donc c'est
    # le seul des deux instruments qui separe deux bandes a cet effectif.
    ecart_clv = st.stdev(clvs) if len(clvs) > 1 else 0.0
    return {
        "n_opportunites": len(rows),
        "n_matchs": len(matchs),
        "n_joues": sum(1 for r in rows if r["played"]),
        "n_clv": len(clvs),
        "clv_moy_pct": round(st.mean(clvs), 2) if clvs else None,
        "clv_positives_pct": (round(100.0 * sum(1 for x in clvs if x > 0) / len(clvs), 1)
                              if clvs else None),
        "n_regles": len(gains),
        "gagnes": gagnes,
        "perdus": perdus,
        "annules": nuls,
        "roi_pct": round(100.0 * sum(gains) / mise, 2) if mise else None,
        "pnl_eur": round(sum(gains), 2) if gains else None,
        "sigma_roi": (round(sum(gains) / (ecart * len(gains) ** 0.5), 1)
                      if ecart > 0 and gains else None),
        "sigma_clv": (round(st.mean(clvs) * len(clvs) ** 0.5 / ecart_clv, 1)
                      if ecart_clv > 0 and clvs else None),
    }


def _vecteurs(rows: list, stake: float):
    """(les CLV en %, les P&L en €) du lot — chacune avec SON effectif.

    Les moyennes de `_cellule` ne suffisent pas pour tester deux lots l'un
    contre l'autre : il faut les observations."""
    clvs = [clv_pct(float(r["odd_taken"]), float(r["closing_fair_odd"])) * 100.0
            for r in rows
            if r["closing_fair_odd"] and float(r["closing_fair_odd"]) > 0]
    return clvs, _gains(rows, stake)


def _welch(a: list, b: list):
    """(écart des moyennes, t de Welch) — variances inégales, effectifs inégaux.

    Welch et non Student : les deux lots n'ont ni la même taille ni la même
    dispersion, et le lot « le reste » est toujours le plus gros."""
    if len(a) < 2 or len(b) < 2:
        return None, None
    d = st.mean(a) - st.mean(b)
    v = st.variance(a) / len(a) + st.variance(b) / len(b)
    return d, (d / v ** 0.5 if v > 0 else None)


def _bloc_contre_le_reste(opp: list, bande_de, ordre: list, stake: float,
                          col_axe: str) -> None:
    """Chaque bande contre TOUT LE RESTE, corrigé du nombre de comparaisons.

    ⚠️ Pourquoi ce bloc existe : lue seule, la table invite à comparer une
    cellule à la ligne TOTAL. Ce test-là est faux deux fois — le TOTAL
    CONTIENT la bande (les deux échantillons se chevauchent, donc l'écart-type
    de l'écart est sous-estimé), et on le refait dix fois de suite sans jamais
    corriger le seuil. À dix comparaisons, un |t| de 2,3 arrive par pur hasard
    sous une vérité parfaitement plate.

    ⚠️ Le test est fait TOUS SPORTS CONFONDUS, donc il ne sépare pas l'effet du
    délai de celui de la composition : au-delà de 48 h la population est
    quasi exclusivement du soccer. La dernière colonne imprime la part du sport
    dominant de chaque bande pour que ce mélange se VOIE."""
    presentes = [lab for lab in ordre if any(bande_de(r) == lab for r in opp)]
    if len(presentes) < 2:
        return
    seuil = st.NormalDist().inv_cdf(1 - 0.025 / len(presentes))

    print(f"\nCHAQUE BANDE CONTRE TOUT LE RESTE — le test qui répond à "
          f"« où suis-je le moins bon »")
    print(f"{len(presentes)} bandes testées, donc seuil de Bonferroni "
          f"|t| ≥ {seuil:.2f} pour 5 % d'erreur sur TOUT le tableau.")
    # 9 et non 8 : « +10.13 pt » fait 9 caracteres et decalait toute la ligne.
    ent = (f"{col_axe:16} {'n_clv':>5} {'Δ CLV':>9} {'t':>6}   "
           f"{'réglés':>6} {'Δ ROI':>9} {'t':>6}   {'sport dominant':>22}")
    print(ent)
    print("-" * len(ent))
    retenues = []
    for lab in presentes:
        dedans = [r for r in opp if bande_de(r) == lab]
        dehors = [r for r in opp if bande_de(r) != lab]
        c_in, g_in = _vecteurs(dedans, stake)
        c_out, g_out = _vecteurs(dehors, stake)
        dc, tc = _welch(c_in, c_out)
        dg, tg = _welch(g_in, g_out)
        # Le P&L de Welch est en euros par pari : le ramener en points de ROI,
        # sinon la colonne ne se compare pas à celle de la CLV.
        dr = None if dg is None else dg / stake * 100.0
        comptes: dict[str, int] = defaultdict(int)
        for r in dedans:
            comptes[(r["sport"] or "?")] += 1
        dom, n_dom = max(comptes.items(), key=lambda kv: kv[1])
        f = lambda v, u="": "—" if v is None else f"{v:+.2f}{u}"  # noqa: E731
        ft = lambda v: "—" if v is None else f"{v:+.2f}"  # noqa: E731
        marque = ""
        if tc is not None and abs(tc) >= seuil:
            marque += " CLV✔"
        if tg is not None and abs(tg) >= seuil:
            marque += " ROI✔"
        print(f"{lab:16} {len(c_in):5} {f(dc, ' pt'):>9} {ft(tc):>6}   "
              f"{len(g_in):6} {f(dr, ' pt'):>9} {ft(tg):>6}   "
              f"{dom[:14]:>14} {100.0 * n_dom / len(dedans):5.0f} %{marque}")
        retenues.append((lab, tc, tg))

    survivants = [(l, tc, tg) for l, tc, tg in retenues
                  if (tc is not None and abs(tc) >= seuil)
                  or (tg is not None and abs(tg) >= seuil)]
    print(f"\n✔ = franchit le seuil de Bonferroni. "
          f"{len(survivants)} bande(s) sur {len(presentes)} le franchissent"
          + (" : " + ", ".join(l for l, _, _ in survivants) if survivants
             else " — aucune."))
    if not survivants:
        print("   Aucune bande ne se distingue du reste une fois le nombre de "
              "comparaisons pris en\n   compte. Ce n'est pas « les bandes sont "
              "égales » : c'est « à cet effectif, ce\n   tableau ne peut pas "
              "les séparer ».")
    print("\n⚠️ Ce test est TOUS SPORTS CONFONDUS : un écart peut être un écart "
          "de composition\n   plutôt que de délai. La colonne « sport dominant "
          "» dit à quel point la bande est\n   homogène — une bande à 99 % "
          "soccer comparée à un reste mixte compare aussi\n   deux sports.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--premium", action="store_true",
                    help="Filtrer par la porte RÉELLE du canal premium.")
    ap.add_argument("--canal", default=None, metavar="NOM",
                    help="Un autre canal, par son nom exact (implique --premium).")
    ap.add_argument("--books", default=None, metavar="LISTE",
                    help="Books séparés par des virgules. Alias : kambi.")
    ap.add_argument("--stake", type=float, default=25.0,
                    help="Mise notionnelle par pari (défaut 25).")
    ap.add_argument("--out", default=None, metavar="CSV",
                    help="Écrire la table dans un CSV.")
    ap.add_argument("--axe", choices=("cote", "delai"), default="cote",
                    help="Axe des lignes : tranche de COTE (défaut) ou DÉLAI "
                         "avant le coup d'envoi. Le délai découpe au-delà de "
                         "48 h, là où le §16.4 s'arrêtait.")
    ap.add_argument("--porte-sur", choices=("cote", "fair"), default="cote",
                    dest="porte_sur",
                    help="Variable sur laquelle la bande de COTES du canal "
                         "est évaluée : la cote prise (production) ou la fair "
                         "odd. ANALYSE SEULE — ne change aucun réglage.")
    ap.add_argument("--comparer", action="store_true",
                    help="Rejouer les DEUX portes et afficher leur "
                         "recouvrement. Implique --premium.")
    a = ap.parse_args()
    # Un drapeau ignoré en silence est exactement le mode de panne du projet.
    if a.comparer and a.axe != "cote":
        ap.error("--axe n'a pas de sens avec --comparer : la comparaison "
                 "n'affiche que des totaux, sans découpage en bandes.")
    if a.canal or a.comparer:
        a.premium = True
    load_env_file()

    porte = None
    porte_desc = "aucune — toutes les détections"
    if a.premium:
        porte, porte_desc = porte_de_canal(a.db, a.canal)
        if a.porte_sur == "fair" and not a.comparer:
            brute = porte
            porte = lambda r: brute(_VueFairOdd(r))  # noqa: E731
            porte_desc += "  ·  bande de cotes évaluée sur la FAIR ODD"

    books = _books_demandes(a.books)

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = list(con.execute("""
        SELECT vb.id, vb.event_key, vb.book, vb.market, vb.outcome_label,
               vb.line, vb.odd_taken, vb.fair_odd, vb.ev_pct, vb.detected_at,
               e.sport AS sport, e.league AS league,
               e.home AS home, e.away AS away, e.start_time AS start_time,
               cs.fair_odd AS closing_fair_odd,
               r.winner, r.home_score, r.away_score,
               (pb.value_bet_id IS NOT NULL) AS played
        FROM value_bets vb
        LEFT JOIN clv_snapshots cs
               ON cs.value_bet_id = vb.id AND cs.closing = 1
        LEFT JOIN events e   ON e.event_key = vb.event_key
        LEFT JOIN results r  ON r.event_key = vb.event_key
        LEFT JOIN played_bets pb ON pb.value_bet_id = vb.id
    """))
    if not rows:
        raise SystemExit("Aucune détection en base.")

    def selectionner(predicat):
        """Les opportunités dédupliquées que cette porte laisserait passer.

        La déduplication reste sur la COTE PRISE dans les deux régimes : c'est
        elle qui paie, et changer aussi le critère de « meilleur prix » ferait
        varier deux choses à la fois."""
        gardees = [r for r in rows
                   if (books is None or (r["book"] or "").lower() in books)
                   and (predicat is None or predicat(r))]
        best = {}
        for r in gardees:
            cle = ((r["home"] or "").lower(), (r["away"] or "").lower(),
                   (r["start_time"] or "")[:10], r["market"],
                   r["outcome_label"], r["line"])
            prev = best.get(cle)
            if prev is None or float(r["odd_taken"]) > float(prev["odd_taken"]):
                best[cle] = r
        return best

    if a.comparer:
        sur_cote = selectionner(porte)
        sur_fair = selectionner(lambda r: porte(_VueFairOdd(r)))

        # ⚠️ Partition sur l'IDENTITÉ DU PARI (`vb.id`), JAMAIS sur la clé de
        # déduplication. `selectionner` rejoue la dédup « meilleure cote prise »
        # sur un vivier différent dans chaque régime : pour une même clé
        # (équipes+jour+marché+pari), le représentant retenu peut être un AUTRE
        # book, à un AUTRE prix. Partitionner sur la clé faisait tomber ces cas
        # dans « gardées par les DEUX » et les sortait des deux lots exclusifs,
        # alors que les deux colonnes de totaux les comptaient avec deux prix,
        # deux CLV et deux P&L différents.
        #
        # Le biais était systématique ET orienté : il touche exactement les
        # sélections dont la cote prise dépasse la bande mais dont la fair odd y
        # retombe — donc le régime fair y promeut la cote la PLUS LONGUE de la
        # même sélection, un pari de variance supérieure, invisible dans le
        # tableau. Détecté par la revue adverse, puis confirmé sur les données
        # réelles du 03/09 : les totaux ne se reconstituaient pas —
        # 7 284 + 1 530 = 8 814 pour un total affiché de 8 855, 41 € manquants.
        par_id_c = {r["id"]: r for r in sur_cote.values()}
        par_id_f = {r["id"]: r for r in sur_fair.values()}
        communs = par_id_c.keys() & par_id_f.keys()

        # Combien de sélections changent de prix retenu d'un régime à l'autre.
        # Tant que ce nombre n'est pas nul, les deux colonnes de totaux ne
        # portent PAS sur les mêmes paris, et il faut le dire.
        bascules = sum(1 for k in set(sur_cote) & set(sur_fair)
                       if sur_cote[k]["id"] != sur_fair[k]["id"])

        print(f"\nCOMPARAISON DES DEUX PORTES — {porte_desc}")
        print(f"Books : {', '.join(sorted(books)) if books else 'tous'}"
              f"   ·   mise notionnelle {a.stake:g} €")
        print("\nLa bande d'EV et toutes les autres règles sont IDENTIQUES. "
              "Seule change\nla variable sur laquelle la bande de COTES est "
              "évaluée.\n")

        entete = (f"{'':34}{'porte COTE PRISE':>18}{'porte FAIR ODD':>18}")
        print(entete)
        print("-" * len(entete))
        ca, fa = _cellule(list(sur_cote.values()), a.stake), \
            _cellule(list(sur_fair.values()), a.stake)
        for lib, cle, suf, dec in (
                ("opportunités", "n_opportunites", "", 0),
                ("matchs distincts", "n_matchs", "", 0),
                ("paris valorisés en CLV", "n_clv", "", 0),
                ("CLV moyenne", "clv_moy_pct", " %", 2),
                ("CLV positives", "clv_positives_pct", " %", 1),
                ("paris réglés", "n_regles", "", 0),
                ("ROI", "roi_pct", " %", 2),
                # ⚠️ Chaque σ teste « cette porte gagne-t-elle » contre zéro,
                # SÉPARÉMENT. Les deux échantillons partagent l'essentiel de
                # leurs paris : ces deux nombres NE SE SOUSTRAIENT PAS, et leur
                # écart ne porte aucune significativité. Le seul t qui réponde
                # à la question est celui de la différence, imprimé plus bas.
                ("σ vs 0 (chaque porte seule)", "sigma_roi", "", 1),
                ("P&L notionnel", "pnl_eur", " €", 0)):
            # Un signe n'a de sens que sur une grandeur qui peut être négative.
            # « CLV positives : +100,0 % » se lirait comme une variation.
            signe = not (cle.startswith("n_") or cle == "clv_positives_pct")
            f = (lambda v, s=signe: "—" if v is None else
                 (f"{v:+.{dec}f}{suf}" if s
                  else f"{v:,.{dec}f}{suf}".replace(",", " ")))
            print(f"{lib:34}{f(ca[cle]):>18}{f(fa[cle]):>18}")

        print("\nRECOUVREMENT — ce que chaque porte prend SEULE")
        print("-" * len(entete))
        lot_c = [par_id_c[i] for i in par_id_c.keys() - communs]
        lot_f = [par_id_f[i] for i in par_id_f.keys() - communs]
        blocs = [("gardés par les DEUX", [par_id_c[i] for i in communs]),
                 ("SEULEMENT par la cote prise", lot_c),
                 ("SEULEMENT par la fair odd", lot_f)]
        for lib, sous in blocs:
            c = _cellule(sous, a.stake)
            roi = "—" if c["roi_pct"] is None else f"{c['roi_pct']:+.2f} %"
            clv = "—" if c["clv_moy_pct"] is None else f"{c['clv_moy_pct']:+.2f} %"
            pnl = "—" if c["pnl_eur"] is None else f"{c['pnl_eur']:+.0f} €"
            # Même convention que `pnl_detections.report` : une ligne sous
            # seuil est marquée. C'est ici qu'elle manquait le plus — le lot
            # exclusif est par construction le plus petit du tableau, et c'est
            # celui sur lequel la decision repose.
            flag = " ⚠️" if c["n_regles"] < 30 else ""
            print(f"  {lib:30} n={c['n_opportunites']:5}  "
                  f"réglés={c['n_regles']:5}  CLV {clv:>9}  ROI {roi:>9}  "
                  f"P&L {pnl:>9}{flag}")

        # Le t de la DIFFÉRENCE, la seule quantité qui réponde à la question.
        # Les échantillons sont APPARIÉS : la part commune s'annule exactement,
        # donc la variance de l'écart ne depend QUE des deux lots disjoints.
        gc, gf = _gains(lot_c, a.stake), _gains(lot_f, a.stake)
        var = ((st.variance(gc) * len(gc) if len(gc) > 1 else 0.0)
               + (st.variance(gf) * len(gf) if len(gf) > 1 else 0.0))
        ecart = sum(gf) - sum(gc)
        t = ecart / var ** 0.5 if var > 0 else None
        print(f"\n  ÉCART NET de la bascule : {ecart:+.0f} € "
              f"({'t = %+.2f' % t if t is not None else 't incalculable'})"
              f"   — sous |t| = 2, c'est du bruit.")
        if bascules:
            print(f"  ⚠️ {bascules} sélection(s) changent de PRIX RETENU d'un "
                  f"régime à l'autre : sur\n     celles-là, les deux colonnes "
                  f"de totaux ne comparent pas le même pari.")

        print("\n⚠️ Ce sont les DEUX dernières lignes du recouvrement qui "
              "décident, pas les totaux.\n   La partition porte sur l'identité "
              "du pari, donc les totaux se reconstituent\n   exactement : "
              "commun + lot exclusif = total, de chaque côté.")
        print("\n⚠️ Sur un value bet, `fair_odd < odd_taken` toujours "
              "(ev = odd/fair − 1). La bascule\n   n'élargit donc pas la bande, "
              "elle la fait GLISSER vers le haut des cotes prises :\n   à 20 % "
              "d'EV, une bande 1,5–4 sur la fair accepte jusqu'à 4,8 de cote "
              "prise et\n   rejette tout ce qui est pris sous 1,8.")
        print("\nAnalyse seule — aucun réglage n'a été lu autrement ni modifié.")
        return 0

    # Filtre de books AVANT la déduplication : on veut le meilleur prix parmi
    # les books qu'on joue, pas le meilleur prix du marché.
    retenues = []
    for r in rows:
        if books is not None and (r["book"] or "").lower() not in books:
            continue
        if porte is not None and not porte(r):
            continue
        retenues.append(r)

    # Même clé que `pnl_detections` : équipes + jour + marché + pari (§17.8).
    best: dict[tuple, sqlite3.Row] = {}
    for r in retenues:
        cle = ((r["home"] or "").lower(), (r["away"] or "").lower(),
               (r["start_time"] or "")[:10], r["market"], r["outcome_label"],
               r["line"])
        prev = best.get(cle)
        if prev is None or float(r["odd_taken"]) > float(prev["odd_taken"]):
            best[cle] = r
    opp = list(best.values())

    print(f"\nCLV ET ROI — porte : {porte_desc}")
    print(f"Books : {', '.join(sorted(books)) if books else 'tous'}")
    print(f"Mise notionnelle : {a.stake:g} €")
    print(f"{len(opp)} opportunités dédupliquées, sur {len(rows)} lignes\n")

    col_axe, bande_de, ordre = _axe(a.axe)

    groupes: dict[tuple, list] = defaultdict(list)
    for r in opp:
        groupes[((r["sport"] or "?"), bande_de(r))].append(r)

    # Une bande absente de l'ordre canonique ne doit pas DISPARAÎTRE : elle
    # s'ajoute en queue. Sans ça, un libellé imprévu retirerait ses paris du
    # tableau sans rien dire, et les lignes ne sommeraient plus au TOTAL.
    ordre = list(ordre) + sorted({k[1] for k in groupes} - set(ordre))

    lignes = []
    # Sur l'axe du délai, les effectifs par sport fondent dans les bandes
    # lointaines : la table tous sports confondus passe DEVANT, parce que
    # c'est la seule où le ROI garde un effectif lisible au-delà de 48 h.
    if a.axe == "delai":
        par_bande: dict[str, list] = defaultdict(list)
        for (_s, lab), sub in groupes.items():
            par_bande[lab].extend(sub)
        for lab in ordre:
            if par_bande.get(lab):
                lignes.append({"sport": "TOUS", "tranche": lab,
                               **_cellule(par_bande[lab], a.stake)})
        lignes.append({"sport": "TOUS", "tranche": "TOTAL",
                       **_cellule(opp, a.stake)})

    for sport in sorted({k[0] for k in groupes}):
        for lab in ordre:
            sub = groupes.get((sport, lab))
            if sub:
                lignes.append({"sport": sport, "tranche": lab, **_cellule(sub, a.stake)})
        tout = [r for r in opp if (r["sport"] or "?") == sport]
        lignes.append({"sport": sport, "tranche": "TOTAL", **_cellule(tout, a.stake)})
    if a.axe != "delai":
        lignes.append({"sport": "TOUS", "tranche": "TOTAL", **_cellule(opp, a.stake)})

    # Les libellés de délai vont jusqu'à « ? (sans horaire) » : une largeur
    # figée à 8 les tronquerait ou décalerait toute la ligne.
    larg = max(len(col_axe), max(len(l["tranche"]) for l in lignes))
    # Deux σ, donc deux noms : un « σ » unique se lisait comme s'il portait sur
    # les deux mesures, alors qu'il ne portait que sur le ROI.
    entete = (f"{'sport':8} {col_axe:{larg}} {'opp':>5} {'matchs':>6} {'joués':>5} "
              f"{'n_clv':>5} {'CLV':>8} {'σCLV':>5} {'CLV+':>6} "
              f"{'réglés':>6} {'G/P/N':>12} {'ROI':>8} {'σROI':>5} {'P&L':>9}")
    print(entete)
    print("-" * len(entete))
    for l in lignes:
        if l["tranche"] == "TOTAL":
            print("-" * len(entete))
        clv = "—" if l["clv_moy_pct"] is None else f"{l['clv_moy_pct']:+.2f}%"
        clv_pos = "—" if l["clv_positives_pct"] is None else f"{l['clv_positives_pct']:.0f}%"
        gpn = f"{l['gagnes']}/{l['perdus']}/{l['annules']}"
        roi = "—" if l["roi_pct"] is None else f"{l['roi_pct']:+.2f}%"
        sig = "—" if l["sigma_roi"] is None else f"{l['sigma_roi']:.1f}"
        sig_c = "—" if l["sigma_clv"] is None else f"{l['sigma_clv']:.1f}"
        pnl = "—" if l["pnl_eur"] is None else f"{l['pnl_eur']:+.0f}€"
        # Un ROI sur 11 paris s'imprime comme un ROI sur 400. Sur l'axe du
        # delai les bandes lointaines fondent : sans marque, la ligne la plus
        # spectaculaire du tableau est aussi la moins fiable, et rien ne le dit.
        maigre = " ⚠️" if 0 < l["n_regles"] < 30 else ""
        print(f"{l['sport'][:8]:8} {l['tranche']:{larg}} "
              f"{l['n_opportunites']:5} {l['n_matchs']:6} {l['n_joues']:5} "
              f"{l['n_clv']:5} {clv:>8} {sig_c:>5} {clv_pos:>6} "
              f"{l['n_regles']:6} {gpn:>12} {roi:>8} {sig:>5} {pnl:>9}{maigre}")

    if any(0 < l["n_regles"] < 30 for l in lignes):
        print("\n⚠️ = moins de 30 paris réglés dans la cellule. À cet effectif, "
              "l'intervalle de\n   confiance du ROI dépasse largement l'écart "
              "qu'on cherche à lire : la ligne est\n   un indice, pas un "
              "résultat.")

    _bloc_contre_le_reste(opp, bande_de, ordre, a.stake, col_axe)

    print("\n⚠️ `n_clv` et `réglés` ne décrivent PAS la même population : la CLV "
          "exige une clôture\n   capturée, le ROI un résultat. Comparer leurs "
          "moyennes suppose de regarder d'abord\n   si les deux effectifs se "
          "ressemblent.")

    if a.axe == "delai":
        print("\n⚠️ LE DÉLAI EST CELUI DE LA PREMIÈRE DÉTECTION, pas de la mise. "
              "`detected_at` ne\n   bouge jamais (§14.5) : une opportunité vue à "
              "60 h et encore affichée à 3 h\n   compte ici en 48-72 h. Ces "
              "bandes mesurent QUAND LE PRIX EST APPARU, ce qui est\n   la "
              "question posée à la CLV, mais elles ne prouvent pas qu'un pari "
              "ait été\n   plaçable pendant toute la bande.")
        print("\n⚠️ Le délai n'est pas indépendant du reste : les marchés "
              "ouverts tôt ne sont pas\n   les mêmes ligues, ni les mêmes "
              "books, ni les mêmes cotes que ceux ouverts à\n   2 h du coup "
              "d'envoi. Un écart de ROI entre deux bandes peut donc être un "
              "écart\n   de composition — croiser avec `--axe cote` avant de "
              "conclure.")

    if a.out:
        champs = ["sport", "tranche", "n_opportunites", "n_matchs", "n_joues",
                  "n_clv", "clv_moy_pct", "sigma_clv", "clv_positives_pct",
                  "n_regles", "gagnes", "perdus", "annules", "roi_pct",
                  "sigma_roi", "pnl_eur"]
        with open(a.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=champs)
            w.writeheader()
            w.writerows(lignes)
        print(f"\n✓ CSV écrit : {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
