#!/usr/bin/env python3
"""Tes paris JOUÉS sans résultat : combien, et POURQUOI chacun.

POURQUOI CETTE SONDE
--------------------
Le dashboard dit « 100 réglés sur 200 ». Tu sais, toi, que tu n'as pas 100
paris en cours chez les books. L'écart est fait de paris dont le match est
fini mais dont le résultat n'est jamais arrivé en base — et chacun manque
pour UNE raison précise, qui appelle UN geste précis :

* le match n'est pas encore joué, ou vient de finir ........ rien à faire ;
* football d'aujourd'hui ................................... le pont ne prend
  une journée que le LENDEMAIN ;
* journée jamais récupérée par le pont ..................... cliquer, ou
  élargir `SCORES_BRIDGE_DAYS` si la journée est sortie de sa fenêtre ;
* journée refusée par la source (hors abonnement) .......... définitif ;
* journée capturée trop tôt ................................. sera reprise au
  prochain clic ;
* journée complète mais match absent, non apparié ou non
  terminé (reporté, prolongations) ......................... `results-update
  --dry-run --day` dit lequel ;
* tennis, doubles, abandons, sport sans source, marché non réglable.

⚠️ `results-update` résume tout ça en un pourcentage par sport, et un taux ne
dit pas QUELLE cause domine. Deux causes opposées — « le pont n'a pas demandé
la journée » et « la source n'a pas le match » — donnent le même symptôme :
rien. C'est le §13.12.

⚠️ LECTURE SEULE. Rien n'est écrit, ni en base ni sur disque.

Usage :
    .venv/bin/python -m scripts.resultats_manquants
    .venv/bin/python -m scripts.resultats_manquants --depuis 2026-09-19 --lister
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.clv import settle  # noqa: E402
from src.config import load_env_file  # noqa: E402

#: Même grâce que `results-update` (`until = now - 2 h`) : avant, le match
#: n'est pas encore réclamé, et le compter comme « manquant » serait faux.
GRACE = timedelta(hours=2)

#: Les sports qui ont une source de résultats (`score_sources.provider_for`).
SPORTS_AVEC_SOURCE = ("soccer", "tennis")
MARCHES_REGLABLES = ("h2h", "totals")

REGLE = "réglé"
A_VENIR = "pas encore joué, ou fini depuis moins de 2 h"
FOOT_DU_JOUR = "football d'aujourd'hui — le pont ne prend une journée que le lendemain"
JAMAIS_DEMANDEE = "journée JAMAIS récupérée — elle est dans la fenêtre du pont"
HORS_FENETRE = "journée JAMAIS récupérée — hors de la fenêtre du pont, ne sera plus demandée"
REFUSEE = "journée REFUSÉE par la source (hors abonnement) — définitif"
TROP_TOT = "journée capturée trop tôt — sera reprise au prochain clic"
TROP_TOT_HORS = "journée capturée trop tôt et hors fenêtre — ne sera plus reprise"
FOOT_ABSENT = "journée complète, mais match absent de la source, non apparié ou non terminé"
TENNIS = "tennis sans résultat (doubles et abandons ne se règlent jamais)"
SANS_EVENTS = "aucune ligne `events` — jamais réclamé"
SANS_SOURCE = "sport sans source de résultats"
MARCHE = "marché que `settle` ne sait pas régler (mi-temps…)"
INEXPLOITABLE = "résultat en base, mais insuffisant pour ce pari"

#: L'ordre d'impression : ce qui est normal d'abord, puis ce qui demande un
#: geste, du plus fréquent attendu au plus rare.
ORDRE = [REGLE, A_VENIR, FOOT_DU_JOUR, JAMAIS_DEMANDEE, HORS_FENETRE, REFUSEE,
         TROP_TOT, TROP_TOT_HORS, FOOT_ABSENT, TENNIS, SANS_EVENTS, SANS_SOURCE,
         MARCHE, INEXPLOITABLE]
NORMAUX = {REGLE, A_VENIR, FOOT_DU_JOUR}


def _instant(brut) -> "datetime | None":
    if not brut:
        return None
    try:
        d = datetime.fromisoformat(str(brut).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _coup_d_envoi(r) -> "datetime | None":
    """`events.start_time`, sinon l'heure UTC écrite en tête de la clé."""
    t = _instant(r["start_time"])
    if t is not None:
        return t
    try:
        return datetime.strptime(str(r["event_key"]).split("::", 1)[0],
                                 "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def etat_journee(jour: date, dossier: Path, maintenant: datetime,
                 jours_pont: int, final_apres: int) -> str:
    """Ce que le pont football a fait de cette journée UTC — la même règle
    que `_handle_scores_plan`, relue et non recopiée à l'aveugle :

    * le pont ne demande que les journées d'HIER à `jours_pont` jours ;
    * une pierre tombale `.refused` est définitive ;
    * un fichier capturé moins de `final_apres` secondes après la fin de la
      journée n'est pas définitif : il sera redemandé tant que la journée
      reste dans la fenêtre."""
    recul = (maintenant.date() - jour).days
    dans_fenetre = 1 <= recul <= jours_pont
    if (dossier / f"{jour.isoformat()}.refused").exists():
        return REFUSEE
    f = dossier / f"{jour.isoformat()}.json"
    if not f.exists():
        return JAMAIS_DEMANDEE if dans_fenetre else HORS_FENETRE
    fin = datetime.combine(jour + timedelta(days=1), datetime.min.time(),
                           tzinfo=timezone.utc).timestamp()
    if f.stat().st_mtime < fin + final_apres:
        return TROP_TOT if dans_fenetre else TROP_TOT_HORS
    return FOOT_ABSENT


def classer(r, maintenant: datetime, dossier: Path, jours_pont: int,
            final_apres: int) -> str:
    """La raison pour laquelle CE pari joué a — ou n'a pas — son résultat."""
    marche = r["market"]
    if r["has_result"]:
        statut = settle(marche, r["outcome_label"], r["line"], r["winner"],
                        r["home_score"], r["away_score"])
        if statut is not None:
            return REGLE
    if marche not in MARCHES_REGLABLES:
        return MARCHE
    if r["has_result"]:
        return INEXPLOITABLE
    depart = _coup_d_envoi(r)
    if depart is None or depart > maintenant - GRACE:
        return A_VENIR
    if not r["has_event"]:
        return SANS_EVENTS
    sport = (r["sport"] or "").lower()
    if sport not in SPORTS_AVEC_SOURCE:
        return SANS_SOURCE
    if sport == "tennis":
        return TENNIS
    jour = depart.date()
    if jour >= maintenant.date():
        return FOOT_DU_JOUR
    return etat_journee(jour, dossier, maintenant, jours_pont, final_apres)


def charger(db: str, depuis: date) -> list:
    """Les paris JOUÉS dont le match tombe à partir de `depuis` (UTC)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = list(con.execute("""
        SELECT pb.dedup_key, pb.played_at, pb.event_key, pb.book, pb.market,
               pb.outcome_label, pb.line, pb.odd_taken, pb.stake,
               COALESCE(e.sport, pb.sport) AS sport, e.league AS league,
               e.home AS home, e.away AS away, e.start_time AS start_time,
               (e.event_key IS NOT NULL) AS has_event,
               (r.event_key IS NOT NULL) AS has_result,
               r.winner, r.home_score, r.away_score
        FROM played_bets pb
        LEFT JOIN events e  ON e.event_key = pb.event_key
        LEFT JOIN results r ON r.event_key = pb.event_key
    """))
    con.close()
    seuil = datetime.combine(depuis, datetime.min.time(), tzinfo=timezone.utc)
    return [r for r in rows
            if (_coup_d_envoi(r) or _instant(r["played_at"]) or seuil) >= seuil]


def analyser(rows: list, maintenant: datetime, dossier: Path, jours_pont: int,
             final_apres: int) -> dict:
    classes = [(r, classer(r, maintenant, dossier, jours_pont, final_apres))
               for r in rows]
    return {"classes": classes,
            "comptes": Counter(c for _r, c in classes)}


def _nom(r) -> str:
    return f"{r['home'] or '?'} - {r['away'] or '?'}"


def imprimer(res: dict, depuis: date, dossier: Path, jours_pont: int,
             source_jours: str, maintenant: datetime, lister: bool) -> None:
    classes, comptes = res["classes"], res["comptes"]
    total = len(classes)
    print(f"PARIS JOUÉS SANS RÉSULTAT — matchs à partir du {depuis.isoformat()} (UTC)")
    print(f"Pont football : {dossier}  ·  SCORES_BRIDGE_DAYS = {jours_pont} "
          f"({source_jours})\n")
    if not total:
        print("Aucun pari joué sur cette fenêtre.")
        return

    manquants = total - sum(comptes[c] for c in NORMAUX)
    larg = max(len(c) for c in ORDRE)
    print(f"Sur {total} paris joués :")
    for c in ORDRE:
        if comptes[c]:
            marque = "  " if c in NORMAUX else "❗"
            print(f"  {marque} {c:{larg}}  {comptes[c]:5}")
    print(f"\n→ {manquants} pari(s) dont le match est FINI et le résultat "
          f"ABSENT (lignes ❗).")
    if not manquants:
        print("  Rien ne manque : les paris non réglés sont des matchs à venir.")
        return

    # Par journée, pour le football : c'est l'unité que le pont récupère.
    foot = defaultdict(Counter)
    for r, c in classes:
        if c not in NORMAUX and c not in (TENNIS, SANS_EVENTS, SANS_SOURCE,
                                          MARCHE, INEXPLOITABLE):
            foot[_coup_d_envoi(r).date()][c] += 1
    if foot:
        print("\nFOOTBALL, JOURNÉE PAR JOURNÉE (la journée est l'unité du pont)")
        for j in sorted(foot):
            (c, n), = foot[j].most_common(1)
            print(f"  {j.isoformat()}  {sum(foot[j].values()):3} pari(s)  — {c}")

    print("\nQUE FAIRE, DANS CET ORDRE")
    jours_hors = sorted({_coup_d_envoi(r).date() for r, c in classes
                         if c in (HORS_FENETRE, TROP_TOT_HORS)})
    # Élargir AVANT de cliquer : un clic sur l'ancienne fenêtre ne reprendrait
    # pas les journées qui en sont sorties, et on croirait le trou comblé.
    if jours_hors:
        recul = (maintenant.date() - jours_hors[0]).days
        print(f"  • {len(jours_hors)} journée(s) sont SORTIES de la fenêtre du "
              f"pont ({jours_hors[0].isoformat()} → {jours_hors[-1].isoformat()}) "
              f": le pont ne\n    les demandera plus. Élargis la fenêtre à "
              f"{recul} jours au moins, puis redémarre le serveur :\n"
              f"      sed -i '/^SCORES_BRIDGE_DAYS=/d' .env && echo "
              f"'SCORES_BRIDGE_DAYS={recul}' >> .env\n"
              f"      sudo systemctl restart betano-ingest\n"
              f"    ⚠️ Si ton abonnement API-Football est gratuit, il ne sert que "
              f"trois jours autour\n    d'aujourd'hui : les journées plus "
              f"anciennes seront REFUSÉES, définitivement.")
    if jours_hors or comptes[JAMAIS_DEMANDEE] or comptes[TROP_TOT]:
        print("  • Clique « Récupérer mes résultats » dans le script, attends la "
              "fin, puis :\n"
              "      .venv/bin/python -m src.main results-update --days 10 "
              "--sport soccer,tennis\n"
              "      .venv/bin/python -m src.main track-update\n"
              "    et relance cette sonde pour voir ce qui reste.")
    if comptes[FOOT_ABSENT]:
        jours = sorted({_coup_d_envoi(r).date() for r, c in classes
                        if c == FOOT_ABSENT})
        print(f"  • {comptes[FOOT_ABSENT]} match(s) de football sur des journées "
              f"COMPLÈTES : la source ne les a pas,\n    ne les apparie pas, ou "
              f"ils ne sont pas terminés. D'abord relancer results-update (si tu "
              f"ne\n    l'as pas fait depuis ton dernier clic), puis, pour savoir "
              f"lequel des trois :")
        for j in jours[-3:]:
            print(f"      .venv/bin/python -m src.main results-update --dry-run "
                  f"--day {j.isoformat()} --sport soccer")
    if comptes[TENNIS]:
        print(f"  • {comptes[TENNIS]} match(s) de tennis : relancer "
              f"`results-update --days 10 --sport tennis`. Ce qui reste\n"
              f"    ensuite est surtout des DOUBLES et des ABANDONS, que la "
              f"source ne règle pas —\n    ceux-là sont à noter à la main, "
              f"ou à laisser non réglés.")
    if comptes[SANS_EVENTS]:
        print(f"  • {comptes[SANS_EVENTS]} pari(s) sans ligne `events` : "
              f"`.venv/bin/python -m scripts.repair_events --apply`.")
    if comptes[REFUSEE]:
        print(f"  • {comptes[REFUSEE]} pari(s) sur des journées refusées par la "
              f"source : perdus pour le P&L automatique.")

    if lister:
        print("\nLES PARIS SANS RÉSULTAT, UN PAR UN")
        for c in ORDRE:
            lot = [r for r, cc in classes if cc == c and c not in NORMAUX]
            if not lot:
                continue
            print(f"\n── {c} ({len(lot)})")
            for r in sorted(lot, key=lambda r: _coup_d_envoi(r) or maintenant):
                d = _coup_d_envoi(r)
                pari = f"{r['outcome_label']}"
                # Le libellé porte parfois déjà la ligne (« over 2.5 ») : ne
                # pas l'écrire deux fois.
                if r["line"] is not None and f"{r['line']:g}" not in pari:
                    pari += f" {r['line']:g}"
                print(f"  {d.strftime('%Y-%m-%d %H:%M') if d else '?':16} "
                      f"{(r['sport'] or '?')[:6]:6} {_nom(r)[:38]:38} "
                      f"{(r['market'] or '')[:7]:7} {pari[:12]:12} "
                      f"@{float(r['odd_taken'] or 0):5.2f} "
                      f"{(r['book'] or '')[:14]:14} "
                      f"{(r['league'] or '')[:28]}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Tes paris JOUÉS sans résultat : combien, et pourquoi "
                    "chacun — journée non récupérée par le pont, refusée par "
                    "la source, match absent, tennis, etc. Lecture seule.")
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--depuis", default=None, metavar="AAAA-MM-JJ",
                    help="Premier jour de match inclus (UTC). Défaut : il y a "
                         "10 jours.")
    ap.add_argument("--lister", action="store_true",
                    help="Nommer chaque pari sans résultat, groupé par cause.")
    a = ap.parse_args(argv)

    # `.env` d'abord : `SCORES_BRIDGE_DAYS` y vit, et c'est lui qui décide de
    # la fenêtre du pont. Le lire avant le chargement ferait juger la fenêtre
    # sur le défaut (2 jours) pendant que le serveur en utilise une autre.
    load_env_file()
    brut = os.getenv("SCORES_BRIDGE_DAYS")
    jours_pont = int(brut) if brut else 2
    source_jours = ("lu dans l'environnement" if brut
                    else "absent de .env — défaut du serveur")
    final_apres = int(os.getenv("SCORES_FINAL_AFTER_SEC", str(6 * 3600)))
    # Même défaut que le serveur d'ingestion : relatif au PROJET, pas au
    # répertoire courant — lancée d'ailleurs, la sonde regarderait un dossier
    # vide et déclarerait toutes les journées « jamais récupérées ».
    racine = Path(__file__).resolve().parents[1]
    dossier = Path(os.getenv("SCORES_INGEST_DIR",
                             str(racine / "data" / "scores"))) / "soccer"

    maintenant = datetime.now(timezone.utc)
    try:
        depuis = (date.fromisoformat(a.depuis) if a.depuis
                  else maintenant.date() - timedelta(days=10))
    except ValueError:
        ap.error(f"--depuis attend AAAA-MM-JJ, reçu {a.depuis!r}")
    rows = charger(a.db, depuis)
    res = analyser(rows, maintenant, dossier, jours_pont, final_apres)
    imprimer(res, depuis, dossier, jours_pont, source_jours, maintenant, a.lister)


if __name__ == "__main__":
    main()
