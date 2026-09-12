"""Les agrégations d'un lot d'opportunités — CLV, ROI, P&L, couverture.

⚠️ AUCUNE DÉFINITION N'EST CRÉÉE ICI. Tout vient de `src.clv` :

    CLV        `clv_pct(odd_taken, closing_fair_odd) = odd/closing - 1`
               où `closing_fair_odd` est `clv_snapshots.fair_odd`, la ligne
               de clôture DÉVIGUÉE — jamais `pinnacle_odd`, qui porte la
               commission et gonflerait chaque CLV de toute la marge.
    Règlement  `clv.settle(market, outcome_label, line, winner, hs, as)`
    P&L        `clv.pnl(statut, odd, stake)` — `None` tant que le résultat
               est inconnu, JAMAIS 0.
    ROI        `100 × Σ(gains) / (n_réglés × mise)` — voir `_cellule`.

`_gains`, `_cellule` et `_vecteurs` ont été DÉPLACÉES depuis
`scripts/clv_roi_matrix.py`, sans une virgule de changement : elles sont la
seule définition du ROI du projet, et `clv_roi_matrix` les réimporte d'ici.

LA FORMULE DU ROI, ÉNONCÉE
--------------------------
    mise  = stake × (nombre de paris RÉGLÉS)
    ROI % = 100 × Σ(P&L) / mise

Le dénominateur ne compte QUE les paris réglés : mettre au dénominateur les
paris sans résultat diluerait le ROI vers zéro à mesure que le settlement
prend du retard, et ferait baisser le chiffre sans qu'aucun pari n'ait perdu.
Un `void` compte au dénominateur (il a été misé) et rend 0 au numérateur.
"""
from __future__ import annotations

import statistics as st

from ..clv import aggregate as clv_aggregate
from ..clv import clv_pct
from ..clv import pnl as clv_pnl
from ..clv import settle as clv_settle


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



# ── Seuils de qualité ────────────────────────────────────────────────
#
# Nommés et groupés ici pour être lisibles d'un coup d'œil et testables. Un
# seuil enfoui dans un `if` au milieu d'un calcul finit par exister en deux
# exemplaires qui ne disent pas la même chose.

#: Sous ce taux, la CLV porte sur une minorité de la population : la moyenne
#: décrit alors les paris dont la clôture a été capturée, pas le lot demandé.
SEUIL_COUVERTURE_CLV = 80.0

#: Sous ce taux, le ROI décrit les matchs DÉJÀ réglés, qui ne sont pas un
#: échantillon au hasard du lot — le règlement arrive plus vite sur les
#: grands championnats que sur le reste.
SEUIL_SETTLEMENT = 60.0

#: En dessous, l'intervalle de confiance du ROI dépasse tout écart qu'on
#: chercherait à y lire. Même convention que `pnl_detections`.
SEUIL_EFFECTIF = 30

#: ⚠️ LA DATE À PARTIR DE LAQUELLE LA CLV EXISTE VRAIMENT.
#:
#: Mesuré le 12/09 sur la base de production : les clôtures portent un
#: `fair_odd` nul pour 100 % de juin et 70,1 % de juillet, contre 0 % en août
#: et en septembre. Ce n'est pas un trou de collecte — le prix de clôture est
#: là — c'est le DEVIG qui n'a jamais été calculé. Or le devig exige le marché
#: entier à l'instant de la clôture, lu dans `quotes`, purgée à deux jours
#: (`PRUNE_DAYS=2`). `backfill-fair-lines` le dit lui-même : « leur CLV est
#: définitivement perdu ».
#:
#: Conséquence : toute CLV antérieure à cette date porte sur une minorité
#: non aléatoire de la population. Elle doit être ANNONCÉE, jamais corrigée.
DEVIG_COMPLET_DEPUIS = "2026-08-01"


def statut_de(row) -> "str | None":
    """« won » / « lost » / « void », ou None si le résultat ne tranche pas.

    ⚠️ None N'EST PAS UNE PERTE ET N'EST PAS UN NUL. Un pari sans résultat
    connu, ou sur un marché que le moteur ne sait pas régler, doit ressortir
    comme NON RÉGLÉ jusqu'à l'affichage. Le confondre avec un P&L de zéro
    tirerait mécaniquement le ROI vers la moyenne et ferait passer un retard
    de règlement pour une série de matchs nuls."""
    return clv_settle(row["market"], row["outcome_label"], row["line"],
                      row["winner"], row["home_score"], row["away_score"])


def clv_de(row) -> "float | None":
    """La CLV d'une ligne en %, ou None si la clôture déviguée manque."""
    c = row["closing_fair_odd"]
    if not c or float(c) <= 0:
        return None
    return clv_pct(float(row["odd_taken"]), float(c)) * 100.0


def _moyenne(valeurs) -> "float | None":
    vals = [float(v) for v in valeurs if v is not None]
    return round(st.mean(vals), 4) if vals else None


def resume(rows: list, stake: float) -> dict:
    """Le bloc de chiffres d'une analyse, dans la forme que l'API rend.

    ⚠️ CHAQUE MESURE PORTE SA COUVERTURE, ET C'EST NON NÉGOCIABLE. `clv` sans
    `clv_coverage` est un chiffre qu'on ne peut pas juger : mesuré sur la base
    réelle, la même requête peut rendre +10,4 % sur 95 % du lot ou sur 30 %,
    et ce n'est pas la même phrase. Les deux sortent toujours ensemble.
    """
    # ⚠️ `_cellule` est la SEULE définition du ROI du projet. On construit
    # par-dessus, on ne recalcule pas à côté.
    c = _cellule(rows, stake)
    clvs = [x for x in (clv_de(r) for r in rows) if x is not None]
    # La médiane vient de `clv.aggregate`, définition existante elle aussi.
    agg = clv_aggregate([(float(r["odd_taken"]), float(r["closing_fair_odd"]))
                         for r in rows
                         if r["closing_fair_odd"] and float(r["closing_fair_odd"]) > 0])
    n = len(rows)
    non_regles = n - c["n_regles"]
    return {
        "opportunities": n,
        "matches": c["n_matchs"],
        "settled": c["n_regles"],
        "unsettled": non_regles,
        "settlement_rate": round(100.0 * c["n_regles"] / n, 2) if n else None,
        "clv_n": c["n_clv"],
        "clv_coverage": round(100.0 * c["n_clv"] / n, 2) if n else None,
        "clv": c["clv_moy_pct"],
        "clv_median": round(agg.median_clv_pct, 2) if clvs else None,
        "clv_positive_rate": c["clv_positives_pct"],
        "roi": c["roi_pct"],
        "pnl": c["pnl_eur"],
        "stake_total": round(stake * c["n_regles"], 2),
        "ev_mean": _moyenne(r["ev_pct"] for r in rows),
        "odds_mean": _moyenne(r["odd_taken"] for r in rows),
        "won": c["gagnes"],
        "lost": c["perdus"],
        "void": c["annules"],
        "played": c["n_joues"],
        "sigma_roi": c["sigma_roi"],
        "sigma_clv": c["sigma_clv"],
    }


def avertissements(bloc: dict, date_from: "str | None" = None,
                   date_to: "str | None" = None) -> list:
    """Ce qu'il faut dire à l'utilisateur AVANT qu'il ne lise les chiffres.

    Le mode de panne dominant de ce projet est le silence (§11) : un chiffre
    calculé sur une population amputée ressemble exactement à un chiffre
    calculé sur la population entière. Ces messages sont la seule différence
    visible entre les deux."""
    out = []
    if not bloc["opportunities"]:
        return ["Aucune opportunité ne passe ces filtres — il n'y a rien à "
                "mesurer. Ce n'est pas un résultat nul, c'est un lot vide."]

    couv = bloc["clv_coverage"]
    if couv is not None and couv < SEUIL_COUVERTURE_CLV:
        out.append(
            f"CLV disponible sur {couv:.0f} % des opportunités "
            f"({bloc['clv_n']} sur {bloc['opportunities']}) — la moyenne "
            f"décrit ce sous-ensemble, pas le lot entier.")

    taux = bloc["settlement_rate"]
    if taux is not None and taux < SEUIL_SETTLEMENT:
        out.append(
            f"Résultats provisoires — settlement incomplet "
            f"({taux:.0f} %, {bloc['settled']} paris réglés sur "
            f"{bloc['opportunities']}).")

    if bloc["settled"] and bloc["settled"] < SEUIL_EFFECTIF:
        out.append(
            f"Seulement {bloc['settled']} paris réglés : à cet effectif "
            f"l'intervalle de confiance du ROI dépasse tout écart qu'on "
            f"chercherait à y lire. Indice, pas résultat.")

    # ⚠️ Le trou de devig. Il ne se répare pas et il doit donc se DIRE.
    if date_from is None or str(date_from) < DEVIG_COMPLET_DEPUIS:
        out.append(
            f"La période demandée commence avant le {DEVIG_COMPLET_DEPUIS} : "
            f"la CLV y est partiellement ou totalement indisponible (devig "
            f"jamais calculé, cotes Pinnacle purgées depuis — irrécupérable). "
            f"Mesuré : 100 % de juin et 70 % de juillet 2026 sans ligne de "
            f"clôture déviguée. Les CLV de cette période portent sur une "
            f"minorité NON ALÉATOIRE des opportunités.")
    return out
