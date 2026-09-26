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
les fichiers du pont (qui sont toujours là) et compare l'orientation que
l'ancienne règle a prise à celle de la nouvelle :

* d'accord ........................................ rien à faire ;
* nul symétrique (1-1) ............................ l'orientation ne change rien ;
* INVERSÉ PROBABLE — les deux règles s'opposent ... le score est sans doute
  à l'envers ;
* INDÉCIDABLE — la nouvelle règle ne sait pas ..... une chance sur deux ;
* score différent de ce que le pont donnait ....... d'une autre origine
  (import CSV, saisie) : non audité ;
* non retrouvé dans les fichiers .................. non audité.

Elle signale aussi la CLASSE INCERTAINE : un match sans ligue (juin-juillet,
la ligue n'est collectée que depuis le 01/08) rapproché alors que la source
avait, au même horaire, un jumeau féminin ou de jeunes des mêmes clubs — rien
ne dit lequel des deux était le nôtre.

⚠️ LECTURE SEULE. Rien n'est écrit en base. `--sortie FICHIER` écrit la liste
des résultats suspects, valeurs comprises : c'est à la fois la liste à
corriger et la sauvegarde qui permet d'annuler.

⚠️ Le tennis n'est pas audité : sa source est une API, aucun fichier ne garde
ce qu'elle a répondu.

Usage :
    .venv/bin/python -m scripts.verif_resultats --depuis 2026-06-01
    .venv/bin/python -m scripts.verif_resultats --depuis 2026-06-01 --sortie /tmp/suspects.jsonl
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
from src.config import load_env_file  # noqa: E402
from src.matcher import (class_marker_from_league, match_event,  # noqa: E402
                         normalize_team, team_class, with_class_marker)
from src.scores import (_is_swapped, _nom_entier, _orientation,  # noqa: E402
                        _sans_cote, tolerance_for_scores)

OK = "orientation confirmée par la nouvelle règle"
SYMETRIQUE = "nul symétrique — l'orientation n'y change rien"
INVERSE = "INVERSÉ PROBABLE — les deux règles s'opposent"
INDECIDABLE = "INDÉCIDABLE — la nouvelle règle ne sait pas dans quel sens le lire"
AUTRE_ORIGINE = "score différent de ce que le pont donnait — autre origine, non audité"
INTROUVABLE = "non retrouvé dans les fichiers du pont — non audité"

ORDRE = [OK, SYMETRIQUE, INVERSE, INDECIDABLE, AUTRE_ORIGINE, INTROUVABLE]
SUSPECTS = (INVERSE, INDECIDABLE)

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


def auditer(r, noms: dict, source: SourceFoot) -> dict:
    """Le verdict pour UN résultat stocké."""
    ev = _notre_evenement(r, noms)
    if ev.start_time is None:
        return {"verdict": INTROUVABLE}
    marque = class_marker_from_league(ev.league)
    evm = replace(ev, home=with_class_marker(ev.home, marque),
                  away=with_class_marker(ev.away, marque))
    tol = tolerance_for_scores("soccer")
    lot = _resultats_production(source, ev.start_time.date(), None)
    best = match_event(evm, lot, time_tolerance_minutes=tol, min_score=85.0)
    if best is None:
        return {"verdict": INTROUVABLE}
    ancien = _is_swapped(evm, best)
    stocke = (r["home_score"], r["away_score"])
    out = {"source": f"{best.home} - {best.away}",
           "score_source": (best.home_score, best.away_score),
           "stocke": stocke,
           "jumeau": None if (r["league"] or "") else _jumeau_de_classe(evm, best, lot, tol)}
    if not _meme_score(stocke, _oriente(best, ancien)):
        out["verdict"] = AUTRE_ORIGINE
        return out
    if _sans_cote(best):
        out["verdict"] = SYMETRIQUE
        return out
    nouveau = _orientation(evm, best)
    if nouveau is None:
        out["verdict"] = INDECIDABLE
    elif (nouveau == "inverse") != ancien:
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
    "c = sqlite3.connect('{db}'); "
    "n = sum(c.execute('DELETE FROM results WHERE event_key = ? AND winner IS ? "
    "AND home_score IS ? AND away_score IS ?', (d['event_key'], d['winner'], "
    "d['home_score'], d['away_score'])).rowcount for d in L); "
    "c.commit(); print(n, 'résultat(s) retiré(s)')\"")
#: Remet les lignes telles qu'elles étaient — sans écraser ce qui a été
#: réécrit depuis.
CMD_ANNULER = (
    ".venv/bin/python -c \"import json, sqlite3; "
    "L = [json.loads(l) for l in open('{f}') if l.strip()]; "
    "c = sqlite3.connect('{db}'); "
    "n = sum(c.execute('INSERT OR IGNORE INTO results (event_key, winner, "
    "home_score, away_score, source, settled_at) VALUES (?, ?, ?, ?, ?, ?)', "
    "(d['event_key'], d['winner'], d['home_score'], d['away_score'], "
    "d['source'], d['settled_at'])).rowcount for d in L); "
    "c.commit(); print(n, 'résultat(s) remis')\"")


def imprimer(res: list, joues: dict, depuis: date, db: str,
             sortie: "str | None") -> None:
    print(f"RÉSULTATS DE FOOTBALL EN BASE — orientation et classe, matchs à "
          f"partir du {depuis.isoformat()} (UTC)")
    print("(chaque résultat est rejoué sur les fichiers du pont : l'orientation "
          "prise par l'ancienne\nrègle contre celle de la nouvelle)\n")
    if not res:
        print("Aucun résultat de football sur cette fenêtre.")
        return
    comptes = Counter(d["verdict"] for _r, d in res)
    larg = max(len(v) for v in ORDRE)
    print(f"Sur {len(res)} résultats de football en base :")
    for v in ORDRE:
        if comptes[v]:
            print(f"  {'❗' if v in SUSPECTS else '  '} {v:{larg}}  {comptes[v]:6}")
    jumeaux = [(r, d) for r, d in res if d.get("jumeau")]
    if jumeaux:
        print(f"  ❗ {'CLASSE INCERTAINE — match sans ligue, jumeau d’une autre classe':{larg}}"
              f"  {len(jumeaux):6}")

    suspects = [(r, d) for r, d in res if d["verdict"] in SUSPECTS]
    touches = [(r, d, p) for r, d in suspects + jumeaux for p in joues.get(r["event_key"], [])]
    for titre, lot in (("INVERSÉS PROBABLES", [x for x in suspects if x[1]["verdict"] == INVERSE]),
                       ("INDÉCIDABLES", [x for x in suspects if x[1]["verdict"] == INDECIDABLE]),
                       ("CLASSE INCERTAINE", jumeaux)):
        if not lot:
            continue
        print(f"\n── {titre} ({len(lot)})")
        for r, d in lot:
            t = _coup_d_envoi(r)
            print(f"  {t.strftime('%Y-%m-%d %H:%M') if t else '?':16}  "
                  f"{r['home']} - {r['away']}  [{(r['league'] or '?')[:28]}]  "
                  f"{r['n_det']} dét., {len(joues.get(r['event_key'], []))} joué(s)")
            print(f"      en base : {d['stocke'][0]:g}-{d['stocke'][1]:g} · source : "
                  f"{d['source']} {d['score_source'][0]:g}-{d['score_source'][1]:g}"
                  + (f" · corrigé : {d['corrige'][0]:g}-{d['corrige'][1]:g}"
                     if d.get("corrige") else "")
                  + (f" · jumeau : {d['jumeau']}" if d.get("jumeau") else ""))
    if touches:
        print(f"\nTES PARIS JOUÉS SUR CES MATCHS ({len(touches)}) — leur P&L en dépend")
        for r, d, p in touches:
            print(f"  {r['home']} - {r['away']}  {_pari(p)}  mise {p['stake'] or '?'}"
                  f"  → {d['verdict'] if d['verdict'] in SUSPECTS else 'classe incertaine'}")

    print("\nQUE FAIRE")
    if not suspects and not jumeaux:
        print("  • Rien : aucun résultat de football en base n'a été mal orienté "
              "par l'ancienne règle.")
        return
    if suspects:
        if sortie:
            print(f"  • {len(suspects)} résultat(s) suspect(s) écrit(s) dans "
                  f"{sortie} — la liste ET la sauvegarde.\n"
                  f"    Pour les retirer (seulement s'ils valent encore ce que la "
                  f"sonde a lu) :\n"
                  f"      {CMD_RETIRER.format(f=sortie, db=db)}\n"
                  f"    puis, pour que la nouvelle règle les relise — elle règle "
                  f"les inversés dans le bon\n    sens et laisse les indécidables "
                  f"sans résultat :\n"
                  f"      .venv/bin/python -m src.main results-update --days "
                  f"{_jours(suspects)} --sport soccer\n"
                  f"      .venv/bin/python -m src.main track-update\n"
                  f"    ⚠️ `track-update` TOUT DE SUITE : un `settle --from` sur un "
                  f"ancien paris_track.csv\n    réécrirait les scores retirés.\n"
                  f"    Pour annuler :\n"
                  f"      {CMD_ANNULER.format(f=sortie, db=db)}")
        else:
            print(f"  • {len(suspects)} résultat(s) suspect(s). Relance avec "
                  f"`--sortie /tmp/suspects.jsonl` pour obtenir\n    la liste, "
                  f"sa sauvegarde et les commandes de correction.")
    if jumeaux:
        print(f"  • {len(jumeaux)} match(s) à la classe incertaine : rien ne dit "
              f"si c'était le match des\n    seniors ou son jumeau. À vérifier à "
              f"l'œil — la sonde ne propose pas de les retirer.")


def _jours(lot) -> int:
    ts = [t for r, _d in lot for t in [_coup_d_envoi(r)] if t]
    if not ts:
        return 3
    return max(3, (datetime.now(timezone.utc).date() - min(ts).date()).days + 1)


def ecrire_sortie(chemin: str, res: list) -> int:
    n = 0
    with open(chemin, "w", encoding="utf-8") as f:
        for r, d in res:
            if d["verdict"] in SUSPECTS:
                f.write(json.dumps({k: r[k] for k in (
                    "event_key", "winner", "home_score", "away_score", "source",
                    "settled_at")}, ensure_ascii=False) + "\n")
                n += 1
    return n


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Les résultats de football EN BASE : score à l'envers "
                    "(ancienne règle d'orientation) ou classe incertaine. "
                    "Lecture seule.")
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--depuis", default="2026-06-01", metavar="AAAA-MM-JJ")
    ap.add_argument("--sortie", default=None, metavar="FICHIER",
                    help="Écrire les résultats suspects (JSON par ligne) : la "
                         "liste à corriger et la sauvegarde pour annuler.")
    a = ap.parse_args(argv)
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
    if a.sortie:
        ecrire_sortie(a.sortie, res)
    imprimer(res, joues, depuis, a.db, a.sortie)


if __name__ == "__main__":
    main()
