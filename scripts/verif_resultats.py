#!/usr/bin/env python3
"""Les résultats de football EN BASE sont-ils justes ? Orientation et classe.

POURQUOI CETTE SONDE
--------------------
Mesuré le 27/09 : l'ancienne règle d'orientation de `bind_results` (le seul
score d'appariement, qui juge un nom sur son meilleur FRAGMENT) stockait le
score À L'ENVERS entre clubs aux noms emboîtés — « Dundee Utd v Dundee »
contre la source « Dundee United v Dundee », dans le MÊME ordre, était
retourné. Un 1X2 réglé à l'envers ne lève rien : il empoisonne le ROI en
silence. La production est corrigée (`scores._orientation`) ; cette sonde
retrouve les résultats DÉJÀ stockés par l'ancienne règle.

Pour chaque résultat de football en base, elle rejoue le rapprochement sur
les fichiers du pont (qui sont toujours là), lit dans quel sens le score a
été STOCKÉ — dans l'ordre de la source, ou retourné — et le compare au sens
que donne la nouvelle règle. Elle ne suppose pas quelle version de la
production l'a écrit : avant le 18/08 rien n'était retourné, du 18 au 21/08
le rapprochement se faisait sur les noms compactés, et une règle rejouée
aujourd'hui ne dirait pas ce qu'elles ont fait.

* même sens que la nouvelle règle ............... rien à faire ;
* nul symétrique (1-1) ............................ l'orientation ne change rien ;
* INVERSÉ PROBABLE — sens contraire ............... le score est sans doute
  à l'envers ;
* INDÉCIDABLE — la nouvelle règle ne sait pas ..... une chance sur deux ;
* score qui ne vaut la source dans AUCUN sens ..... d'une autre origine
  (import CSV, saisie) : non audité ;
* non retrouvé dans les fichiers .................. non audité.

Elle signale aussi la CLASSE INCERTAINE : un match dont la ligue ne porte
aucune classe, rapproché alors que la source avait, au même horaire, un
jumeau féminin ou de jeunes des mêmes clubs — rien ne dit lequel des deux
était le nôtre. (Pas seulement les matchs SANS ligue : `repair_leagues
--apply` remplit la ligue avec celle du match même qu'on met en doute.)

⚠️ LECTURE SEULE. Rien n'est écrit en base. `--sortie FICHIER` écrit la liste
des résultats suspects, valeurs comprises : c'est à la fois la liste à
corriger et la sauvegarde qui permet d'annuler.

⚠️ Le tennis n'est audité que sur un point, qui ne demande aucun fichier : un
DOUBLE réglé par la source tennis est faux à coup sûr — elle ne sert aucun
double, et ce résultat est celui d'un simple des mêmes joueurs (revue du
27/09). Pour le reste, sa source est une API : aucun fichier ne garde ce
qu'elle a répondu.

⚠️ Seuls les résultats venus de la SOURCE (api-football, livetennisapi) sont
proposés au retrait. Un résultat saisi ou importé (`settle --from`) est
listé, jamais retiré : la sonde ne sait pas s'il a été corrigé à la main.

Usage :
    .venv/bin/python -m scripts.verif_resultats --depuis 2026-06-01
    .venv/bin/python -m scripts.verif_resultats --depuis 2026-06-01 \
        --sortie data/verif_resultats-$(date +%Y%m%d-%H%M).jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rapidfuzz import fuzz  # noqa: E402

from scripts.resultats_manquants import (SourceFoot, _coup_d_envoi,  # noqa: E402
                                         _notre_evenement, _resultats_production)
from src.config import ScanConfig, load_env_file  # noqa: E402
from src.matcher import (class_marker_from_league, match_event,  # noqa: E402
                         normalize_team, team_class, with_class_marker)
from src.scores import (_nom_entier, _orientation, _sans_cote,  # noqa: E402
                        tolerance_for_scores)

OK = "stocké dans le sens que donne la nouvelle règle"
SYMETRIQUE = "nul symétrique — l'orientation n'y change rien"
INVERSE = "INVERSÉ PROBABLE — stocké dans le sens contraire de la nouvelle règle"
INDECIDABLE = "INDÉCIDABLE — la nouvelle règle ne sait pas dans quel sens le lire"
AUTRE_ORIGINE = ("score qui ne vaut la source dans aucun sens — autre origine, "
                 "non audité")
INTROUVABLE = "non retrouvé dans les fichiers du pont — non audité"

DOUBLE_FAUX = ("DOUBLE de tennis réglé par le score d'un SIMPLE — faux à coup "
               "sûr (la source n'a aucun double)")

ORDRE = [OK, SYMETRIQUE, INVERSE, INDECIDABLE, DOUBLE_FAUX, AUTRE_ORIGINE,
         INTROUVABLE]
SUSPECTS = (INVERSE, INDECIDABLE, DOUBLE_FAUX)
#: Les sources dont la sonde sait qu'elles ont écrit ce qu'elles ont lu.
SOURCES_API = ("api-football", "livetennisapi")


def a_retirer(r, d) -> bool:
    """Suspect ET venu de la source : les seuls que la sonde propose de
    retirer. Un score saisi ou importé peut être une correction à la main."""
    return d["verdict"] in SUSPECTS and str(r["source"] or "").startswith(SOURCES_API)

#: Deux noms sont « les mêmes clubs » pour la recherche de jumeaux quand ils se
#: ressemblent EN ENTIER, classe retirée — pas par un fragment.
JUMEAU_ENTIER = 90.0


def charger(db: str, depuis: date) -> tuple:
    """(résultats de football en base à partir de `depuis`, avec leur match ;
    noms affichés ; paris joués par event_key)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    borne = datetime.combine(depuis, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    rows = [dict(r) for r in con.execute("""
        SELECT e.event_key, e.league, e.home, e.away, e.start_time,
               r.winner, r.home_score, r.away_score, r.source, r.settled_at,
               (SELECT COUNT(*) FROM value_bets v WHERE v.event_key = e.event_key)
                   AS n_det
        FROM results r JOIN events e ON e.event_key = r.event_key
        WHERE e.sport = 'soccer' AND e.start_time >= ?
        ORDER BY e.start_time""", (borne,))]
    try:
        noms = {n: d for n, d in con.execute(
            "SELECT normalized_name, display_name FROM teams")}
    except sqlite3.OperationalError:
        noms = {}
    joues: dict = defaultdict(list)
    try:
        for p in con.execute("""
                SELECT event_key, market, outcome_label, line, odd_taken, stake
                FROM played_bets WHERE event_key IS NOT NULL"""):
            joues[p["event_key"]].append(dict(p))
    except sqlite3.OperationalError:
        pass
    con.close()
    return rows, noms, joues


def charger_doubles(db: str, depuis: date, noms: dict) -> list:
    """Les résultats de tennis que la SOURCE a écrits sur un match de double.
    Elle n'en sert aucun : chacun est le score d'un simple, attribué à tort."""
    from src.scores import _DOUBLES
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    borne = datetime.combine(depuis, datetime.min.time(), tzinfo=timezone.utc).isoformat()
    try:
        rows = [dict(r) for r in con.execute("""
            SELECT e.event_key, e.league, e.home, e.away, e.start_time,
                   r.winner, r.home_score, r.away_score, r.source, r.settled_at,
                   (SELECT COUNT(*) FROM value_bets v WHERE v.event_key = e.event_key)
                       AS n_det
            FROM results r JOIN events e ON e.event_key = r.event_key
            WHERE e.sport = 'tennis' AND e.start_time >= ?
              AND r.source LIKE 'livetennisapi%'""", (borne,))]
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return [r for r in rows
            if "/" in (noms.get(r["home"]) or "") or "/" in (noms.get(r["away"]) or "")
            or _DOUBLES.search(r["league"] or "")]


def _oriente(res, inverse: bool) -> tuple:
    """(dom, ext) du résultat source, dans l'orientation donnée."""
    return ((res.away_score, res.home_score) if inverse
            else (res.home_score, res.away_score))


def _meme_score(a, b) -> bool:
    return all((x is None and y is None) or (x is not None and y is not None
                                              and float(x) == float(y))
               for x, y in zip(a, b))


def _jumeau_de_classe(evm, best, lot, tol: int) -> "str | None":
    """Un autre match de la source, au même horaire (tolérance), des mêmes
    clubs EN ENTIER mais d'une autre classe que celui retenu. Rend son nom."""
    cible = team_class(normalize_team(best.home))
    h, a = _nom_entier(evm.home), _nom_entier(evm.away)
    for x in lot:
        if x is best or abs((x.start_time - evm.start_time).total_seconds()) > tol * 60:
            continue
        if team_class(normalize_team(x.home)) == cible:
            continue
        xh, xa = _nom_entier(x.home), _nom_entier(x.away)
        if ((fuzz.ratio(h, xh) >= JUMEAU_ENTIER and fuzz.ratio(a, xa) >= JUMEAU_ENTIER)
                or (fuzz.ratio(h, xa) >= JUMEAU_ENTIER and fuzz.ratio(a, xh) >= JUMEAU_ENTIER)):
            return f"{x.home} - {x.away}"
    return None


def _apparier(evm, source: SourceFoot, tol: int):
    """Le résultat source retenu, comme la production l'a fait.

    D'abord le lot veille + jour + lendemain. Mais un results-update quotidien
    tourne AVANT que le fichier du lendemain existe (le pont ne demande une
    journée qu'une fois finie) : si le lendemain ajoute un rival qui rend le
    lot ambigu, on rejoue sans lui, comme la production l'a vu."""
    jour = evm.start_time.date()
    for jours in (None, {jour - timedelta(days=1), jour}):
        lot = _resultats_production(source, jour, jours)
        best = match_event(evm, lot, time_tolerance_minutes=tol, min_score=85.0)
        if best is not None:
            return best, lot
    return None, []


def auditer(r, noms: dict, source: SourceFoot) -> dict:
    """Le verdict pour UN résultat stocké."""
    ev = _notre_evenement(r, noms)
    if ev.start_time is None:
        return {"verdict": INTROUVABLE}
    marque = class_marker_from_league(ev.league)
    evm = replace(ev, home=with_class_marker(ev.home, marque),
                  away=with_class_marker(ev.away, marque))
    tol = tolerance_for_scores("soccer")
    best, lot = _apparier(evm, source, tol)
    if best is None:
        return {"verdict": INTROUVABLE}
    stocke = (r["home_score"], r["away_score"])
    out = {"source": f"{best.home} - {best.away}",
           "score_source": (best.home_score, best.away_score),
           "stocke": stocke,
           # Une ligue sans classe ne dit rien de la classe du match : c'est le
           # cas des matchs sans ligue ET de ceux que `repair_leagues` a
           # remplis avec la ligue du match même qu'on met en doute.
           "jumeau": (None if class_marker_from_league(r["league"] or "")
                      else _jumeau_de_classe(evm, best, lot, tol))}
    if _sans_cote(best) and _meme_score(stocke, _oriente(best, False)):
        out["verdict"] = SYMETRIQUE
        return out
    # Le sens dans lequel le score a été STOCKÉ, lu sur le score lui-même.
    if _meme_score(stocke, _oriente(best, False)):
        stocke_inverse = False
    elif _meme_score(stocke, _oriente(best, True)):
        stocke_inverse = True
    else:
        out["verdict"] = AUTRE_ORIGINE
        return out
    nouveau = _orientation(evm, best)
    if nouveau is None:
        out["verdict"] = INDECIDABLE
    elif (nouveau == "inverse") != stocke_inverse:
        out["verdict"] = INVERSE
        out["corrige"] = _oriente(best, nouveau == "inverse")
    else:
        out["verdict"] = OK
    return out


def analyser(rows: list, noms: dict, dossier: Path, progres=None) -> list:
    source = SourceFoot(dossier)
    out = []
    for i, r in enumerate(rows, 1):
        t = _coup_d_envoi(r)
        if t is not None:
            source.oublier(t.date() - timedelta(days=1))
        out.append((r, auditer(r, noms, source)))
        if progres and i % 1000 == 0:
            progres(i, len(rows))
    return out


def _f(x) -> str:
    """Un score, ou « ? » : `settle --from` accepte un CSV sans score."""
    return "?" if x is None else f"{x:g}"


def _pari(p) -> str:
    lib = p["outcome_label"] or "?"
    if p["line"] is not None and f"{p['line']:g}" not in lib:
        lib += f" {p['line']:g}"
    return f"{p['market']} {lib} @{float(p['odd_taken'] or 0):.2f}"


#: Retire un résultat SEULEMENT s'il vaut encore exactement ce que la sonde a
#: lu : une ligne corrigée entre-temps n'est pas touchée.
CMD_RETIRER = (
    ".venv/bin/python -c \"import json, sqlite3; "
    "L = [json.loads(l) for l in open('{f}') if l.strip()]; "
    "c = sqlite3.connect('{db}', timeout=60); "
    "n = sum(c.execute('DELETE FROM results WHERE event_key = ? AND winner IS ? "
    "AND home_score IS ? AND away_score IS ?', (d['event_key'], d['winner'], "
    "d['home_score'], d['away_score'])).rowcount for d in L); "
    "c.commit(); print(n, 'résultat(s) retiré(s)')\"")
#: Remet les lignes telles qu'elles étaient — sans écraser ce qui a été
#: réécrit depuis.
CMD_ANNULER = (
    ".venv/bin/python -c \"import json, sqlite3; "
    "L = [json.loads(l) for l in open('{f}') if l.strip()]; "
    "c = sqlite3.connect('{db}', timeout=60); "
    "n = sum(c.execute('INSERT OR IGNORE INTO results (event_key, winner, "
    "home_score, away_score, source, settled_at) VALUES (?, ?, ?, ?, ?, ?)', "
    "(d['event_key'], d['winner'], d['home_score'], d['away_score'], "
    "d['source'], d['settled_at'])).rowcount for d in L); "
    "c.commit(); print(n, 'résultat(s) remis')\"")


def imprimer(res: list, joues: dict, depuis: date, db: str,
             sortie: "str | None", noms: "dict | None" = None,
             production: bool = True) -> None:
    noms = noms or {}

    def nom(r) -> str:
        """Le nom AFFICHÉ, pour la vérification à l'œil — pas la clé compactée."""
        return (f"{noms.get(r['home']) or r['home']} - "
                f"{noms.get(r['away']) or r['away']}")
    print(f"RÉSULTATS EN BASE — orientation, classe et doubles, matchs à "
          f"partir du {depuis.isoformat()} (UTC)")
    print("(chaque résultat de football est rejoué sur les fichiers du pont : "
          "le sens dans lequel il\na été stocké contre celui de la nouvelle "
          "règle)\n")
    if not res:
        print("Aucun résultat sur cette fenêtre.")
        return
    comptes = Counter(d["verdict"] for _r, d in res)
    larg = max(len(v) for v in ORDRE)
    print(f"Sur {len(res)} résultats audités :")
    for v in ORDRE:
        if comptes[v]:
            print(f"  {'❗' if v in SUSPECTS else '  '} {v:{larg}}  {comptes[v]:6}")
    jumeaux = [(r, d) for r, d in res if d.get("jumeau")]
    if jumeaux:
        print(f"  ❗ {'CLASSE INCERTAINE — ligue sans classe, jumeau d’une autre classe':{larg}}"
              f"  {len(jumeaux):6}")

    suspects = [(r, d) for r, d in res if a_retirer(r, d)]
    a_la_main = [(r, d) for r, d in res
                 if d["verdict"] in SUSPECTS and not a_retirer(r, d)]
    uniques = {r["event_key"]: (r, d) for r, d in suspects + a_la_main + jumeaux}
    touches = [(r, d, p) for r, d in uniques.values()
               for p in joues.get(r["event_key"], [])]
    for titre, lot in (("INVERSÉS PROBABLES", [x for x in suspects if x[1]["verdict"] == INVERSE]),
                       ("INDÉCIDABLES", [x for x in suspects if x[1]["verdict"] == INDECIDABLE]),
                       ("DOUBLES RÉGLÉS PAR UN SIMPLE", [x for x in suspects
                                                         if x[1]["verdict"] == DOUBLE_FAUX]),
                       ("SAISIS OU IMPORTÉS — à vérifier à la main, jamais retirés", a_la_main),
                       ("CLASSE INCERTAINE", jumeaux)):
        if not lot:
            continue
        print(f"\n── {titre} ({len(lot)})")
        for r, d in lot:
            t = _coup_d_envoi(r)
            print(f"  {t.strftime('%Y-%m-%d %H:%M') if t else '?':16}  "
                  f"{nom(r)}  [{(r['league'] or '?')[:28]}]  "
                  f"{r['n_det']} dét., {len(joues.get(r['event_key'], []))} joué(s)")
            if d["verdict"] == DOUBLE_FAUX:
                print(f"      en base : {_f(r['home_score'])}-{_f(r['away_score'])} "
                      f"({r['winner']}) — le score d'un simple des mêmes joueurs")
                continue
            print(f"      en base : {_f(d['stocke'][0])}-{_f(d['stocke'][1])} · source : "
                  f"{d['source']} {_f(d['score_source'][0])}-{_f(d['score_source'][1])}"
                  + (f" · corrigé : {_f(d['corrige'][0])}-{_f(d['corrige'][1])}"
                     if d.get("corrige") else "")
                  + (f" · jumeau : {d['jumeau']}" if d.get("jumeau") else "")
                  + (f" · origine : {r['source']}" if not a_retirer(r, d) else ""))
    if touches:
        print(f"\nTES PARIS JOUÉS SUR CES MATCHS ({len(touches)}) — leur P&L en dépend")
        for r, d, p in touches:
            print(f"  {nom(r)}  {_pari(p)}  mise {p['stake'] or '?'}"
                  f"  → {d['verdict'] if d['verdict'] in SUSPECTS else 'classe incertaine'}")

    print("\nQUE FAIRE")
    non_audites = comptes[AUTRE_ORIGINE] + comptes[INTROUVABLE]
    if not suspects and not a_la_main and not jumeaux:
        print(f"  • Rien parmi les {len(res) - non_audites} résultats audités : "
              f"aucun n'est faux.\n    ({non_audites} n'ont pas pu "
              f"être audités — voir les lignes « non audité » ci-dessus.)")
        return
    if suspects and not production:
        print(f"  • {len(suspects)} résultat(s) suspect(s) dans {db}. ⚠️ Ce n'est "
              f"pas data/valuebet.db : results-update\n    et track-update agissent "
              f"sur la base de PRODUCTION, pas sur cette copie. Les commandes de\n"
              f"    correction ne sont pas imprimées — relance la sonde sur "
              f"data/valuebet.db pour les avoir.")
    elif suspects:
        foot = [x for x in suspects if x[1]["verdict"] != DOUBLE_FAUX]
        if sortie:
            print(f"  • {len(suspects)} résultat(s) suspect(s) écrit(s) dans "
                  f"{sortie} — la liste ET la sauvegarde.\n"
                  f"    Pour les retirer (seulement s'ils valent encore ce que la "
                  f"sonde a lu) :\n"
                  f"      {CMD_RETIRER.format(f=sortie, db=db)}\n"
                  f"    ⚠️ Pour annuler, AVANT l'étape suivante (après, seuls les "
                  f"indécidables reviendraient) :\n"
                  f"      {CMD_ANNULER.format(f=sortie, db=db)}\n"
                  + (f"    Puis, LE JOUR MÊME, pour que la nouvelle règle les relise — "
                     f"elle règle les inversés\n    dans le bon sens et laisse les "
                     f"indécidables sans résultat :\n"
                     f"      .venv/bin/python -m src.main results-update --days "
                     f"{_jours(foot)} --sport soccer\n" if foot else
                     "    Puis (les doubles restent sans résultat : la source n'en a "
                     "aucun) :\n")
                  + f"      .venv/bin/python -m src.main track-update\n"
                  f"    ⚠️ `track-update` TOUT DE SUITE : un `settle --from` sur un "
                  f"ancien paris_track.csv\n    réécrirait les scores retirés.")
        else:
            print(f"  • {len(suspects)} résultat(s) suspect(s). Relance avec\n"
                  f"      --sortie data/verif_resultats-$(date +%Y%m%d-%H%M).jsonl\n"
                  f"    pour obtenir la liste, sa sauvegarde et les commandes de "
                  f"correction.")
    if a_la_main:
        print(f"  • {len(a_la_main)} résultat(s) suspect(s) SAISIS OU IMPORTÉS : "
              f"la sonde ne sait pas s'ils ont été\n    corrigés à la main. À "
              f"vérifier toi-même — elle ne propose pas de les retirer.")
    if jumeaux:
        print(f"  • {len(jumeaux)} match(s) à la classe incertaine : rien ne dit "
              f"si c'était le match des\n    seniors ou son jumeau. À vérifier à "
              f"l'œil — la sonde ne propose pas de les retirer.")


def _jours(lot) -> int:
    """Le `--days` qui fera relire les résultats retirés. Une semaine de marge :
    `since = maintenant - days` se décale d'heure en heure, et un results-update
    lancé demain ne reprendrait plus le plus vieux — il resterait retiré, en
    silence. Une fenêtre plus large ne fait que relire des matchs en attente."""
    ts = [t for r, _d in lot for t in [_coup_d_envoi(r)] if t]
    if not ts:
        return 7
    return (datetime.now(timezone.utc).date() - min(ts).date()).days + 7


def ecrire_sortie(chemin: str, res: list) -> int:
    """La liste ET la sauvegarde. Jamais écrasée (mode "x") : relancer la sonde
    après une correction réécrirait sinon un fichier sans les lignes retirées,
    et l'annulation n'aurait plus rien à remettre."""
    if not any(a_retirer(r, d) for r, d in res):
        return 0
    n = 0
    with open(chemin, "x", encoding="utf-8") as f:
        for r, d in res:
            if a_retirer(r, d):
                f.write(json.dumps({k: r[k] for k in (
                    "event_key", "winner", "home_score", "away_score", "source",
                    "settled_at")}, ensure_ascii=False) + "\n")
                n += 1
    return n


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Les résultats EN BASE : score de football à l'envers "
                    "(ancienne règle d'orientation), classe incertaine, double "
                    "de tennis réglé par un simple. Lecture seule.")
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--depuis", default="2026-06-01", metavar="AAAA-MM-JJ")
    ap.add_argument("--sortie", default=None, metavar="FICHIER",
                    help="Écrire les résultats suspects (JSON par ligne) : la "
                         "liste à corriger et la sauvegarde pour annuler.")
    a = ap.parse_args(argv)
    if a.sortie and Path(a.sortie).exists():
        ap.error(f"{a.sortie} existe déjà : c'est peut-être la sauvegarde d'une "
                 "correction. Choisis un autre nom — la sonde ne l'écrase jamais.")
    if a.sortie and not Path(a.sortie).resolve().parent.is_dir():
        ap.error(f"le dossier de {a.sortie} n'existe pas — à vérifier AVANT de "
                 "rejouer quatre mois de résultats.")
    try:
        depuis = date.fromisoformat(a.depuis)
    except ValueError:
        ap.error(f"--depuis attend AAAA-MM-JJ, reçu {a.depuis!r}")
    load_env_file()
    racine = Path(__file__).resolve().parents[1]
    dossier = Path(os.getenv("SCORES_INGEST_DIR",
                             str(racine / "data" / "scores"))) / "soccer"
    rows, noms, joues = charger(a.db, depuis)

    def progres(i, n):
        print(f"  … {i}/{n} résultats rejoués", file=sys.stderr)
    res = analyser(rows, noms, dossier, progres)
    res += [(r, {"verdict": DOUBLE_FAUX}) for r in charger_doubles(a.db, depuis, noms)]
    # results-update et track-update ouvrent TOUJOURS la base de production
    # (`ScanConfig.db_path`, relative au répertoire courant) : corriger une
    # copie laisserait l'erreur là où le P&L la lit.
    production = (Path(a.db).resolve() == Path(ScanConfig().db_path).resolve())
    if a.sortie and production:
        ecrire_sortie(a.sortie, res)
    imprimer(res, joues, depuis, a.db, a.sortie if production else None, noms,
             production)


if __name__ == "__main__":
    main()
