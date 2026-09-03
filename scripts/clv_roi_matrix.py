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

Usage :
    .venv/bin/python -m scripts.clv_roi_matrix --premium
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


def _bande(odd: float) -> str:
    for lab, lo, hi in BANDES_COTE:
        if lo <= odd < hi:
            return lab
    return "?"


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
    }


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
    ap.add_argument("--porte-sur", choices=("cote", "fair"), default="cote",
                    dest="porte_sur",
                    help="Variable sur laquelle la bande de COTES du canal "
                         "est évaluée : la cote prise (production) ou la fair "
                         "odd. ANALYSE SEULE — ne change aucun réglage.")
    ap.add_argument("--comparer", action="store_true",
                    help="Rejouer les DEUX portes et afficher leur "
                         "recouvrement. Implique --premium.")
    a = ap.parse_args()
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

    groupes: dict[tuple, list] = defaultdict(list)
    for r in opp:
        groupes[((r["sport"] or "?"), _bande(float(r["odd_taken"])))].append(r)

    lignes = []
    for sport in sorted({k[0] for k in groupes}):
        for lab, _lo, _hi in BANDES_COTE:
            sub = groupes.get((sport, lab))
            if sub:
                lignes.append({"sport": sport, "tranche": lab, **_cellule(sub, a.stake)})
        tout = [r for r in opp if (r["sport"] or "?") == sport]
        lignes.append({"sport": sport, "tranche": "TOTAL", **_cellule(tout, a.stake)})
    lignes.append({"sport": "TOUS", "tranche": "TOTAL", **_cellule(opp, a.stake)})

    entete = (f"{'sport':8} {'tranche':8} {'opp':>5} {'matchs':>6} {'joués':>5} "
              f"{'n_clv':>5} {'CLV':>8} {'CLV+':>6} "
              f"{'réglés':>6} {'G/P/N':>12} {'ROI':>8} {'σ':>5} {'P&L':>9}")
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
        pnl = "—" if l["pnl_eur"] is None else f"{l['pnl_eur']:+.0f}€"
        print(f"{l['sport'][:8]:8} {l['tranche']:8} "
              f"{l['n_opportunites']:5} {l['n_matchs']:6} {l['n_joues']:5} "
              f"{l['n_clv']:5} {clv:>8} {clv_pos:>6} "
              f"{l['n_regles']:6} {gpn:>12} {roi:>8} {sig:>5} {pnl:>9}")

    print("\n⚠️ `n_clv` et `réglés` ne décrivent PAS la même population : la CLV "
          "exige une clôture\n   capturée, le ROI un résultat. Comparer leurs "
          "moyennes suppose de regarder d'abord\n   si les deux effectifs se "
          "ressemblent.")

    if a.out:
        champs = ["sport", "tranche", "n_opportunites", "n_matchs", "n_joues",
                  "n_clv", "clv_moy_pct", "clv_positives_pct", "n_regles",
                  "gagnes", "perdus", "annules", "roi_pct", "sigma_roi", "pnl_eur"]
        with open(a.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=champs)
            w.writeheader()
            w.writerows(lignes)
        print(f"\n✓ CSV écrit : {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
