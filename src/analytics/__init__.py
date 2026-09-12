"""Couche analytique — LECTURE SEULE sur la base du moteur Valuebet.

RÈGLE FONDATRICE : ce paquet ne DÉFINIT rien qui existe déjà.

    CLV          `src.clv.clv_pct`, contre `clv_snapshots.fair_odd` (déviguée)
    règlement    `src.clv.settle`
    P&L          `src.clv.pnl`
    ROI          `metriques._cellule`, déplacée depuis `clv_roi_matrix`
    éligibilité  `routing.canaux_pour` via `pnl_detections.porte_de_canal`,
                 plus les gardes de `alerter.send_value_bet`
    bandes       `pnl_detections.BANDES_COTE`, `main._ev_bucket`

Une seconde définition, même « équivalente », finit par diverger : c'est
arrivé trois fois dans ce projet (§17.7), chaque fois en silence, et chaque
fois une sonde a décrit pendant des semaines un flux qui n'existait plus.

Ce paquet n'écrit JAMAIS. Les connexions sont ouvertes en `mode=ro` : la
garantie vient de SQLite, pas de la discipline de celui qui écrit le code.

    from src.analytics import Filtres, Population, analyser, detail

    f = Filtres(sports=("soccer",), books=("unibet_be", "ladbrokes_be"),
                cote_min=2.0, cote_max=4.0, ev_min=10, ev_max=20,
                date_from="2026-08-01", date_to="2026-09-12",
                population=Population.SETTLED)
    res = analyser("data/valuebet.db", f)
    print(res["summary"]["roi"], res["warnings"])
"""
from .filtres import Filtres, FiltreInvalide  # noqa: F401
from .populations import Population  # noqa: F401
from .service import analyser, detail, valeurs_disponibles  # noqa: F401

__all__ = ["Filtres", "FiltreInvalide", "Population",
           "analyser", "detail", "valeurs_disponibles"]
