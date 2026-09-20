"""L'orchestration d'une analyse : filtres → SQL → métriques → découpes.

CE QUE CE MODULE GARANTIT
-------------------------
* **Lecture seule, garantie par SQLite et non par ma discipline.** Toutes les
  connexions sont ouvertes en `mode=ro` : une écriture accidentelle lève une
  erreur au lieu d'abîmer la production.
* **Les filtres s'exécutent DANS la base.** Rien n'est chargé pour être
  filtré ensuite — sauf deux exceptions nommées et bornées (voir plus bas).
* **Aucune définition métier n'est créée ici.** Les bandes de cote viennent de
  `pnl_detections.BANDES_COTE`, les bandes d'EV de `main._ev_bucket`, la
  semaine de `clv_roi_matrix._bande_semaine`, la porte des canaux de
  `pnl_detections.porte_de_canal`. Toutes sont importées, aucune recopiée.

LES DEUX FILTRES QUI NE DESCENDENT PAS DANS LE SQL, ET POURQUOI
---------------------------------------------------------------
`ELIGIBLE_FOR_ALERT` rejoue `routing.canaux_pour` et les gardes de
`alerter.send_value_bet` ; `SETTLED` appelle `clv.settle`. Traduire l'un ou
l'autre en SQL créerait une seconde définition qui divergerait de la
production — c'est le §17.7, et ce projet l'a payé trois fois.

Ils s'appliquent donc en Python, mais JAMAIS sur la base entière : sur
l'ensemble déjà réduit par tous les autres filtres, qui sont eux exécutés par
SQLite. L'ordre compte, et il est tenu par un test.

⚠️ LES IMPORTS DE `scripts/` SONT DIFFÉRÉS. `clv_roi_matrix` importe
`src.analytics` ; un import au niveau du module créerait un cycle. Différés,
ils sont résolus à l'appel. C'est une dette assumée le temps de la V1 : `src/`
ne devrait pas dépendre de `scripts/`, mais une dépendance visible vaut mieux
qu'un doublon invisible.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .filtres import Filtres
from .metriques import (avertissements, clv_de, resume, statut_de,
                        _cellule, _gains)
from .perimetre import (BANDES_DELAI, BORNES_EV, MARCHES_ANALYTICS,
                        SPORTS_ANALYTICS, bande_delai, canoniser_book,
                        libelle_book, libelle_groupe_book, libelle_marche,
                        libelle_sport, ordre_delai, slug_cote,
                        taille_echantillon)
from .populations import (Population, alias_de, EXPLICATION, EXPOSEES,
                          LIBELLE, LIMITES)
from .requete import construire

DB_DEFAUT = "data/valuebet.db"

#: Granularités de l'axe temporel. « semaine » est le défaut : le jour est
#: trop bruité pour lire une CLV, le mois trop grossier sur trois mois de
#: données.
GRANULARITES = ("jour", "semaine", "mois")


def _connexion(db_path) -> sqlite3.Connection:
    """Une connexion LECTURE SEULE. `mode=ro` échoue à l'ouverture si le
    fichier n'existe pas — on préfère cette erreur-là à une base vide créée
    silencieusement à côté de la vraie, qui rendrait « 0 opportunité »."""
    chemin = Path(db_path)
    if not chemin.exists():
        raise FileNotFoundError(
            f"Base introuvable : {chemin}. La couche analytique ne CRÉE jamais "
            f"de base — ce chemin doit désigner la base de production.")
    con = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# ── Les découpes, toutes lues ailleurs ───────────────────────────────

def _bandes_cote():
    from scripts.pnl_detections import BANDES_COTE
    return BANDES_COTE


def _bande_cote(odd: float) -> str:
    for lab, lo, hi in _bandes_cote():
        if lo <= odd < hi:
            return lab
    return "?"


def _bande_ev(ev: float) -> str:
    from src.main import _ev_bucket
    return _ev_bucket(ev)


def _ordre_ev():
    from src.main import _EV_BUCKET_ORDER
    return list(_EV_BUCKET_ORDER)


def _bande_temps(row, granularite: str) -> str:
    if granularite == "semaine":
        from scripts.clv_roi_matrix import _bande_semaine
        return _bande_semaine(row)
    brut = row["detected_at"] or ""
    return brut[:10] if granularite == "jour" else brut[:7]


# ── Les filtres qui restent en Python ────────────────────────────────

def _porte_eligibilite(db_path, filtres):
    """Le prédicat de production : porte du canal PUIS gardes d'envoi.

    Rend (predicat, description, minutes_fenetre_morte, provenance)."""
    from scripts.pnl_detections import porte_de_canal
    from scripts.clv_roi_matrix import _fenetre_morte_defaut
    from .populations import _alertable

    porte, desc = porte_de_canal(str(db_path), filtres.canal)
    if filtres.fenetre_morte_min is None:
        minutes, source = _fenetre_morte_defaut()
    else:
        minutes, source = filtres.fenetre_morte_min, "imposée par le filtre"

    def predicat(row) -> bool:
        # L'ORDRE EST CELUI DE LA PRODUCTION : la porte du canal d'abord,
        # puis les suppressions d'envoi. Les inverser ne changerait pas le
        # résultat aujourd'hui, mais la production s'écrit dans ce sens.
        return bool(porte(row)) and _alertable(row, minutes)

    return predicat, desc, minutes, source


def _appliquer_python(lignes, db_path, filtres) -> "tuple[list, dict]":
    """Les deux filtres hors SQL, sur l'ensemble DÉJÀ réduit par la base."""
    info: dict = {}
    pop = alias_de(filtres.population)

    if pop is Population.SETTLED:
        avant = len(lignes)
        lignes = [r for r in lignes if statut_de(r) is not None]
        info["settled_filtre_python"] = {
            "avant": avant, "apres": len(lignes),
            "pourquoi": "`clv.settle` est la seule définition du règlement du "
                        "projet ; le SQL n'a fait que pré-filtrer sur la "
                        "présence d'un résultat.",
        }

    if pop is Population.ELIGIBLE_FOR_ALERT:
        predicat, desc, minutes, source = _porte_eligibilite(db_path, filtres)
        avant = len(lignes)
        lignes = [r for r in lignes if predicat(r)]
        info["eligibilite"] = {
            "avant": avant, "apres": len(lignes), "porte": desc,
            "fenetre_morte_min": minutes, "source_seuil": source,
        }
    return lignes, info


def _charger(db_path, filtres) -> "tuple[list, dict]":
    """Le lot d'opportunités dédupliquées que ces filtres laissent passer."""
    filtres = filtres.valider()
    sql, params = construire(filtres)
    with _connexion(db_path) as con:
        brut = [dict(r) for r in con.execute(sql, params)]
    return _appliquer_python(brut, db_path, filtres)


# ── L'API publique du module ─────────────────────────────────────────

def _recensement_exclus(con) -> dict:
    """Combien de détections le périmètre écarte, et pour quelle raison.

    Trois comptes sur `value_bets` — pas sur les opportunités dédupliquées :
    on compte ce qui n'entre PAS, et la déduplication ne s'applique qu'à ce
    qui entre. Le total brut est rendu avec, pour que l'écart se vérifie à la
    soustraction plutôt que sur parole."""
    ms = ", ".join("?" * len(SPORTS_ANALYTICS))
    mm = ", ".join("?" * len(MARCHES_ANALYTICS))
    def compte(sql, params=()):
        return con.execute(sql, params).fetchone()[0]
    return {
        "total_detections": compte("SELECT COUNT(*) FROM value_bets"),
        "sans_evenement": compte(
            "SELECT COUNT(*) FROM value_bets vb "
            "WHERE NOT EXISTS (SELECT 1 FROM events e "
            "                   WHERE e.event_key = vb.event_key)"),
        "sport_hors_perimetre": compte(
            f"SELECT COUNT(*) FROM value_bets vb "
            f"JOIN events e ON e.event_key = vb.event_key "
            f"WHERE lower(e.sport) NOT IN ({ms})", SPORTS_ANALYTICS),
        "marche_hors_perimetre": compte(
            f"SELECT COUNT(*) FROM value_bets WHERE market NOT IN ({mm})",
            MARCHES_ANALYTICS),
    }


def valeurs_disponibles(db_path=DB_DEFAUT) -> dict:
    """De quoi peupler les listes déroulantes — LU DANS LA BASE.

    ⚠️ Les books viennent de `value_bets`, jamais de `played_bets` dont
    76,6 % des lignes portent un libellé d'affichage. Proposer « StarCasino »
    et « starcasino_sport » comme deux choix distincts serait un piège."""
    marqueurs_s = ", ".join("?" * len(SPORTS_ANALYTICS))
    marqueurs_m = ", ".join("?" * len(MARCHES_ANALYTICS))
    with _connexion(db_path) as con:
        def col(sql, params=()):
            return [r[0] for r in con.execute(sql, params)
                    if r[0] not in (None, "")]
        bornes = con.execute(
            "SELECT MIN(substr(detected_at,1,10)), MAX(substr(detected_at,1,10)) "
            "FROM value_bets").fetchone()
        # ⚠️ TOUT EST BORNÉ AU PÉRIMÈTRE, Y COMPRIS LES LISTES DE CHOIX.
        # Proposer « basketball » dans un menu qui le refusera ensuite serait
        # un piège ; ne proposer que ce qui est analysable est la seule
        # présentation honnête. Les books et les compétitions sont eux aussi
        # restreints aux sports du périmètre : un bookmaker qui n'apparaît
        # que sur du hockey n'a rien à faire dans la liste.
        sports = sorted(col(
            f"SELECT DISTINCT lower(sport) FROM events "
            f"WHERE lower(sport) IN ({marqueurs_s})", SPORTS_ANALYTICS))
        books = sorted({canoniser_book(b) for b in col(
            f"SELECT DISTINCT vb.book FROM value_bets vb "
            f"JOIN events e ON e.event_key = vb.event_key "
            f"WHERE lower(e.sport) IN ({marqueurs_s}) "
            f"  AND vb.market IN ({marqueurs_m})",
            tuple(SPORTS_ANALYTICS) + tuple(MARCHES_ANALYTICS))})
        marches = sorted(col(
            f"SELECT DISTINCT market FROM value_bets "
            f"WHERE market IN ({marqueurs_m})", MARCHES_ANALYTICS))
        ligues = sorted(col(
            f"SELECT DISTINCT league FROM events "
            f"WHERE league IS NOT NULL AND lower(sport) IN ({marqueurs_s})",
            SPORTS_ANALYTICS))
        return {
            "sports": sports,
            "sports_labels": {s: libelle_sport(s) for s in sports},
            # Une seule case à cocher pour les quatre jumeaux : en proposer
            # quatre laisserait croire à quatre choix distincts alors qu'ils
            # rendent tous le même lot.
            "bookmakers": books,
            "bookmakers_labels": {b: libelle_groupe_book(b) for b in books},
            "markets": marches,
            "markets_labels": {m: libelle_marche(m) for m in marches},
            "leagues": ligues,
            # Les bandes que l'interface propose en cases à cocher. Elles
            # viennent d'ici et non d'une liste recopiée dans le JavaScript :
            # une bande ajoutée au moteur doit apparaître dans l'interface
            # sans qu'on touche au frontend.
            "ev_bands": list(_ordre_ev()),
            # ⚠️ LE SLUG VIENT DU SERVEUR, IL N'EST PAS RECALCULÉ EN JS.
            # C'est lui qui nomme les paramètres `ev_odds_min_<slug>` ; une
            # seconde règle de fabrication côté navigateur finirait par
            # produire un nom que le serveur ne reconnaît plus, et le filtre
            # serait refusé — ou pire, ignoré.
            "odds_bands": [{"key": lab, "slug": slug_cote(lab), "min": lo,
                            "max": None if hi >= 1e9 else hi}
                           for lab, lo, hi in _bandes_cote()],
            "delay_bands": [{"key": lab, "min": lo, "max": hi}
                            for lab, lo, hi in BANDES_DELAI],
            # Cinq populations, pas six : `BET` est un alias de `CLICKED`.
            # Voir `populations.EXPOSEES` — elle reste acceptée par l'API.
            "populations": [
                {"value": p.value, "libelle": LIBELLE[p],
                 "explication": EXPLICATION[p],
                 "limites": list(LIMITES[p])} for p in EXPOSEES],
            "date_min": bornes[0], "date_max": bornes[1],
            # ⚠️ Le périmètre est ANNONCÉ, pas subi en silence : l'utilisateur
            # doit savoir que son total ne couvre pas tout ce qu'il a détecté.
            "perimetre": {
                "sports": list(SPORTS_ANALYTICS),
                "markets": list(MARCHES_ANALYTICS),
                # ⚠️ CE QUE LE PÉRIMÈTRE ÉCARTE EST COMPTÉ, PAS SUPPOSÉ.
                # Un total de 27 000 opportunités là où le daemon en a détecté
                # 44 000 se lit comme un bug tant que l'écart n'est pas
                # chiffré. Ces trois nombres le rendent vérifiable, et la
                # ligne `sans_evenement` nomme le cas le moins évident : une
                # détection dont l'événement manque n'a pas de sport, donc
                # ne peut pas être déclarée dans la portée.
                "exclus": _recensement_exclus(con),
                "pourquoi": (
                    "L'Analytics se limite au football et au tennis, marchés "
                    "H2H et Totals. Les autres sports n'ont aucune source de "
                    "résultats (0 résultat sur 501 matchs de basket, 95 de "
                    "volley, 21 de hockey) et les autres marchés ne sont "
                    "jamais réglés par `clv.settle`. Les données restent en "
                    "base ; seule l'analyse les écarte."),
            },
        }


def analyser(db_path=DB_DEFAUT, filtres=None, granularite="semaine") -> dict:
    """Le bloc de chiffres, ses avertissements, et toutes les découpes."""
    filtres = (filtres or Filtres()).valider()
    if granularite not in GRANULARITES:
        from .filtres import FiltreInvalide
        raise FiltreInvalide(
            f"granularite doit valoir {' | '.join(GRANULARITES)} — "
            f"reçu : {granularite!r}")
    lignes, info = _charger(db_path, filtres)

    bloc = resume(lignes, filtres.stake)
    return {
        "filters": filtres.en_dict(),
        "population": {
            "value": alias_de(filtres.population).value,
            "demandee": filtres.population.value,
            "explication": EXPLICATION[filtres.population],
            "limites": list(LIMITES[filtres.population]),
            **info,
        },
        "summary": bloc,
        "warnings": avertissements(bloc, filtres.date_from, filtres.date_to),
        "by_time": _cumuler(_decouper(lignes, filtres.stake,
                                      lambda r: _bande_temps(r, granularite),
                                      ordre=None, tri=True)),
        # Les jumeaux Kambi comptent pour UNE ligne : quatre lignes portant
        # le même prix donneraient quatre échantillons trop petits là où il
        # n'y a qu'une opportunité. `libelle_book` reste employé pour le
        # DÉTAIL, qui doit nommer le book où le prix a vraiment été vu.
        "by_book": _decouper(lignes, filtres.stake,
                             lambda r: canoniser_book(r["book"] or "?"),
                             libelle=libelle_groupe_book),
        "by_sport": _decouper(lignes, filtres.stake,
                              lambda r: r["sport"] or "?",
                              ordre=list(SPORTS_ANALYTICS),
                              libelle=libelle_sport),
        "by_market": _decouper(lignes, filtres.stake,
                               lambda r: r["market"] or "?",
                               ordre=list(MARCHES_ANALYTICS),
                               libelle=libelle_marche),
        "by_odds": _decouper(lignes, filtres.stake,
                             lambda r: _bande_cote(float(r["odd_taken"])),
                             ordre=[l for l, _, _ in _bandes_cote()]),
        "by_ev": _decouper(lignes, filtres.stake,
                           lambda r: _bande_ev(float(r["ev_pct"] or 0.0)),
                           ordre=_ordre_ev()),
        "by_delay": _decouper(lignes, filtres.stake,
                              lambda r: bande_delai(r["delai_h"]),
                              ordre=ordre_delai()),
        "matrix": _matrice(lignes, filtres.stake),
        # La règle d'EV RÉELLEMENT appliquée, sport par sport — relisible.
        "ev_rules": {
            "global": list(filtres.ev_bandes),
            "by_sport": {s: list(b) for s, b in filtres.ev_par_sport},
            "sports_analyses": list(filtres.sports_effectifs()),
        },
    }


def _cumuler(tranches: list) -> list:
    """Ajoute le P&L cumulé et la CLV cumulée à une série temporelle.

    ⚠️ LA CLV CUMULÉE EST UNE MOYENNE PONDÉRÉE PAR L'EFFECTIF, jamais une
    moyenne de moyennes. Une semaine à +20 % sur 3 paris et une semaine à
    +2 % sur 3 000 ne se moyennent pas à +11 % : ce serait donner au bruit le
    même poids qu'au signal. On cumule donc les sommes et les effectifs.

    Les périodes sans mesure ne cassent pas la courbe : le cumul REPORTE la
    dernière valeur connue au lieu de rendre `None`, sinon une semaine creuse
    trouerait un P&L cumulé qui, lui, n'a pas bougé."""
    pnl, somme_clv, n_clv = 0.0, 0.0, 0
    out = []
    for t in tranches:
        if t["pnl"] is not None:
            pnl += t["pnl"]
        if t["clv"] is not None and t["clv_n"]:
            somme_clv += t["clv"] * t["clv_n"]
            n_clv += t["clv_n"]
        out.append({**t,
                    "pnl_cumul": round(pnl, 2),
                    "clv_cumul": round(somme_clv / n_clv, 2) if n_clv else None,
                    "clv_n_cumul": n_clv})
    return out


def _decouper(lignes, stake, cle, ordre=None, tri=False, libelle=None) -> list:
    """Un découpage par une clé, chaque tranche portant SES effectifs.

    ⚠️ Une tranche dont le libellé n'est pas dans l'ordre canonique s'ajoute
    en QUEUE au lieu de disparaître. Sans ça, un libellé imprévu retirerait
    ses paris du tableau sans rien dire, et les tranches ne sommeraient plus
    au total — exactement le mode de panne du §11."""
    groupes: dict = {}
    for r in lignes:
        groupes.setdefault(cle(r), []).append(r)
    libelles = list(ordre or ())
    reste = sorted(set(groupes) - set(libelles))
    libelles = [l for l in libelles if l in groupes] + reste
    if tri:
        libelles = sorted(groupes)
    # `label` est de PRÉSENTATION et `key` reste canonique : le frontend
    # affiche le premier et renvoie le second dans ses filtres. Les confondre
    # ferait repartir « Unibet BE » vers l'API, qui ne connaît que
    # `unibet_be` — et rendrait zéro ligne sous un en-tête normal.
    return [{"key": lib, "label": libelle(lib) if libelle else lib,
             **resume(groupes[lib], stake)} for lib in libelles]


def _matrice(lignes, stake) -> dict:
    """La matrice COTE × EV. Chaque cellule porte N, CLV, ROI et sa couverture.

    Les cellules vides ne sont PAS omises : une case absente se lirait comme
    une case à zéro, alors qu'elle veut dire « aucune opportunité ici »."""
    lig = [l for l, _, _ in _bandes_cote()]
    col = _ordre_ev()
    cases: dict = {}
    for r in lignes:
        k = (_bande_cote(float(r["odd_taken"])),
             _bande_ev(float(r["ev_pct"] or 0.0)))
        cases.setdefault(k, []).append(r)
    return {
        "rows": lig, "cols": col,
        "cells": [{"odds": a, "ev": b, **resume(cases.get((a, b), []), stake)}
                  for a in lig for b in col],
    }


def meilleurs_segments(db_path=DB_DEFAUT, filtres=None, *, min_n=None,
                       trier_par="clv", limite=25, profondeur=3) -> dict:
    """Les combinaisons qui ressortent du lot filtré — avec leurs garde-fous.

    ⚠️ LE LOT EST CELUI DES FILTRES COURANTS, pas la base entière. Chercher
    des segments sur un lot déjà restreint puis les présenter comme des
    propriétés générales serait une double sélection ; l'appelant reçoit donc
    les filtres appliqués avec le résultat, pour que les deux ne se lisent
    jamais séparément."""
    from .perimetre import MIN_SEGMENT
    from .segments import chercher

    filtres = (filtres or Filtres()).valider()
    lignes, info = _charger(db_path, filtres)
    resultat = chercher(
        lignes, filtres.stake, _bande_cote, _bande_ev,
        min_n=MIN_SEGMENT if min_n is None else min_n,
        trier_par=trier_par, limite=limite, profondeur=profondeur)
    return {
        "filters": filtres.en_dict(),
        "population": {"value": alias_de(filtres.population).value, **info},
        # Le total du lot est rendu AVEC les segments : un segment à +12 % de
        # CLV ne veut pas dire la même chose selon que le lot entier est à
        # +2 % ou à +11 %. Sans le repère, le podium se lit comme un exploit.
        "overall": resume(lignes, filtres.stake),
        **resultat,
    }


def detail(db_path=DB_DEFAUT, filtres=None, page=1, par_page=50,
           tri="detected_at", ordre="desc") -> dict:
    """Les opportunités une par une, PAGINÉES.

    ⚠️ La pagination est ici et pas dans le navigateur. 44 498 lignes envoyées
    à une page web pour en afficher cinquante, c'est le défaut que l'énoncé
    interdit explicitement — et c'est aussi ce qui rendrait la page
    inutilisable sur téléphone."""
    filtres = (filtres or Filtres()).valider()
    page = max(1, int(page))
    par_page = max(1, min(int(par_page), 500))
    lignes, _ = _charger(db_path, filtres)

    inverse = str(ordre).lower() != "asc"
    if tri not in ("detected_at", "odd_taken", "ev_pct", "clv", "pnl",
                   "sport", "book", "start_time"):
        tri = "detected_at"

    def cle(r):
        if tri == "clv":
            v = clv_de(r)
        elif tri == "pnl":
            from ..clv import pnl as _pnl
            v = _pnl(statut_de(r), float(r["odd_taken"]), filtres.stake)
        else:
            v = r.get(tri)
        # ⚠️ LES VALEURS MANQUANTES VONT TOUJOURS EN FIN DE LISTE, dans les
        # DEUX sens de tri. Un `None` en tête ferait croire à un extrême — un
        # pari sans CLV apparaîtrait comme le meilleur du lot.
        #
        # Le drapeau doit donc être PRÉ-INVERSÉ en ordre décroissant : sinon
        # `reverse=True` le retourne avec le reste et remonte précisément ce
        # qu'on voulait reléguer. Attrapé par un test, pas par relecture.
        manquant = v is None
        return ((not manquant) if inverse else manquant,
                v if v is not None else 0)

    lignes = sorted(lignes, key=cle, reverse=inverse)
    total = len(lignes)
    debut = (page - 1) * par_page
    from ..clv import pnl as clv_pnl
    return {
        "page": page, "per_page": par_page, "total": total,
        "pages": (total + par_page - 1) // par_page if total else 0,
        "items": [{
            "id": r["id"],
            "detected_at": r["detected_at"],
            "start_time": r["start_time"],
            "sport": r["sport"],
            "league": r["league"],
            "event": f"{r['home']} vs {r['away']}" if r["home"] else r["event_key"],
            "market": r["market"],
            "selection": r["outcome_label"],
            "line": r["line"],
            "bookmaker": r["book"],
            "odds": r["odd_taken"],
            "fair_odd": r["fair_odd"],
            "ev_pct": r["ev_pct"],
            "closing_fair_odd": r["closing_fair_odd"],
            "clv_pct": clv_de(r),
            "delay_h": r["delai_h"],
            "played": bool(r["played"]),
            "notified_at": r["notified_at"],
            # ⚠️ « non réglé » est un statut À PART ENTIÈRE, pas un trou.
            "result": statut_de(r) or "unsettled",
            "stake": filtres.stake if statut_de(r) else None,
            "pnl": clv_pnl(statut_de(r), float(r["odd_taken"]), filtres.stake),
        } for r in lignes[debut:debut + par_page]],
    }
