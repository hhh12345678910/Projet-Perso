#!/usr/bin/env python3
"""Récupère `events.league` là où elle est vide, depuis les fichiers de scores.

POURQUOI LE TROU EXISTE
-----------------------
`main.py:982` ne construit `league_by_event` qu'à partir des cotes PINNACLE
(et de la secondaire, éteinte depuis le §19.6) :

    for q in (*pinnacle_q, *(secondary or [])):
        if q.league and q.event_key not in league_by_event: ...

Or `build_event_rows` couvre le CADRE DE RÉFÉRENCE ENTIER — `{k[0] for k in
fair}`. Tout événement dont la cote Pinnacle ne porte pas de `league.name`
reçoit donc `""`, et `upsert_events` ne remplit une ligue vide que si un cycle
ULTÉRIEUR en apporte une. Si Pinnacle ne l'a jamais nommée, elle ne vient
jamais. Mesuré le 03/09 : 4 908 lignes sans ligue, du 21/06 au 29/08.

POURQUOI LES FICHIERS DE SCORES PEUVENT LA COMBLER
--------------------------------------------------
Chaque fixture d'API-Football porte `league.name` et `league.country`, et le
backfill du 03/09 en a déposé 79 journées sur disque. Le rapprochement
nom+horaire qui a déjà lié 8 129 de nos événements à leur résultat lie donc
aussi chaque événement à SA LIGUE, gratuitement et sans réseau.

⚠️ CE QUE CET OUTIL NE PEUT PAS RÉCUPÉRER, et il faut le savoir avant de lire
son chiffre :

1. **Les événements que le rapprochement ne lie pas.** C'est la même fonction
   que `results-update` : ce qu'elle rate là, elle le rate ici. Les 1 155
   événements « ? » du tableau par ligue sont précisément des non-liés — ils
   resteront sans ligue. La bonne nouvelle est que la population qui compte
   pour une analyse par ligue est celle qui a un RÉSULTAT, donc celle qui a
   été liée.
2. **Le féminin et les jeunes, structurellement.** `bind_results` reprend la
   classe depuis `OurEvent.league` (§21.16 pt 11) — la ligue qu'on n'a
   justement pas. La barrière de classe rend alors `team_similarity` nulle
   contre « Houston Dash W ». Le raisonnement est circulaire et l'outil ne
   peut pas en sortir : ces catégories seront sous-récupérées. Ne lis donc PAS
   un « féminin : 0 » comme « je n'ai pas de féminin ».
3. **Les matchs non terminés.** `parse_apifootball_results` ne garde que `FT`.
   Un match reporté n'a pas de fixture exploitable, donc pas de ligue.

N'ÉCRIT RIEN SANS `--apply`, et n'écrase JAMAIS une ligue déjà connue — même
garde que `Storage.upsert_events` :

    UPDATE events SET league = ? WHERE event_key = ? AND (league IS NULL OR league = '')

Usage :
    .venv/bin/python -m scripts.repair_leagues                 # sonde
    .venv/bin/python -m scripts.repair_leagues --apply         # écrit
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src import teams  # noqa: E402
from src.leagues import categorize  # noqa: E402
from src.score_sources import parse_apifootball_results  # noqa: E402
from src.scores import OurEvent, bind_results  # noqa: E402
from src.storage import Storage  # noqa: E402


def _dt(raw):
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d.astimezone(timezone.utc) if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _nom_de_ligue(bloc: dict) -> str:
    """« Pays - Ligue », la convention de Pinnacle dans `events.league`.

    Pinnacle écrit « USA - National Womens Soccer League », « Finland -
    Kolmonen ». On s'aligne dessus pour que `leagues.categorize` et toute
    analyse par ligue voient une seule convention. `country` vaut « World »
    sur les amicaux et les compétitions internationales, où Pinnacle ne
    préfixe rien : on ne préfixe pas non plus."""
    nom = (bloc.get("name") or "").strip()
    pays = (bloc.get("country") or "").strip()
    if not nom:
        return ""
    if pays and pays.lower() != "world":
        return f"{pays} - {nom}"
    return nom


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--dir", default=None,
                    help="Répertoire des scores (défaut : SCORES_INGEST_DIR, "
                         "sinon data/scores).")
    ap.add_argument("--sport", default="soccer",
                    help="Seul le football a une source de ligues aujourd'hui.")
    ap.add_argument("--top", type=int, default=20,
                    help="Nombre de ligues détaillées (défaut 20).")
    ap.add_argument("--apply", action="store_true",
                    help="ÉCRIT en base. Sans lui, rien n'est modifié.")
    a = ap.parse_args()

    base = Path(a.dir or os.getenv("SCORES_INGEST_DIR", "data/scores")) / a.sport
    if not base.is_dir():
        raise SystemExit(
            f"Répertoire de scores introuvable : {base}. Lance la commande "
            f"depuis la racine du projet, ou passe --dir.")

    con = sqlite3.connect(a.db)
    con.row_factory = sqlite3.Row
    # Le registre `teams` rend le nom d'origine à partir de la forme compactée
    # stockée dans `events.home` (§21.16 pt 15). Même réparation à la lecture
    # que `results-update`, pour que le rapprochement voie les mêmes noms.
    teams.init(Storage(a.db))

    rows = con.execute(
        """
        SELECT e.event_key, e.home, e.away, e.start_time,
               EXISTS(SELECT 1 FROM results r
                      WHERE r.event_key = e.event_key) AS a_resultat
        FROM events e
        WHERE e.sport = ?
          AND (e.league IS NULL OR e.league = '')
          AND EXISTS(SELECT 1 FROM value_bets vb
                     WHERE vb.event_key = e.event_key)
        """,
        (a.sport,),
    ).fetchall()

    if not rows:
        print(f"Aucun événement {a.sport} sans ligue. Rien à faire.")
        return 0

    par_jour: dict[str, list] = defaultdict(list)
    sans_horaire = 0
    for r in rows:
        debut = _dt(r["start_time"])
        if debut is None:
            sans_horaire += 1
            continue
        par_jour[debut.date().isoformat()].append((r, debut))

    trouve: dict[str, str] = {}
    jours_sans_fichier = 0
    evts_sans_fichier = 0

    for jour, items in sorted(par_jour.items()):
        fichier = base / f"{jour}.json"
        if not fichier.exists():
            jours_sans_fichier += 1
            evts_sans_fichier += len(items)
            continue
        try:
            payload = json.loads(fichier.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"  ⚠️ {fichier.name} illisible — {e}")
            continue

        ligue_par_id = {}
        for fx in payload.get("response") or []:
            fid = str(((fx.get("fixture") or {}).get("id")) or "")
            nom = _nom_de_ligue(fx.get("league") or {})
            if fid and nom:
                ligue_par_id[fid] = nom

        resultats, _ = parse_apifootball_results(payload)
        evenements = [
            OurEvent(event_key=r["event_key"],
                     home=teams.display(r["home"]) or r["home"],
                     away=teams.display(r["away"]) or r["away"],
                     start_time=debut,
                     league="")     # c'est précisément ce qu'on cherche
            for r, debut in items
        ]
        liens, _ = bind_results(evenements, resultats, sport=a.sport)
        for event_key, res in liens:
            ligue = ligue_par_id.get(res.source_id, "")
            if ligue:
                trouve[event_key] = ligue

    avec_resultat = sum(1 for r in rows if r["a_resultat"])
    print(f"\nRÉPARATION DES LIGUES — {a.sport}")
    print(f"  événements sans ligue (portant au moins un value bet) : {len(rows)}")
    print(f"    dont un résultat est déjà noté                      : {avec_resultat}")
    if sans_horaire:
        print(f"    dont sans heure de coup d'envoi (inexploitables)    : {sans_horaire}")
    if jours_sans_fichier:
        print(f"  journées sans fichier de scores : {jours_sans_fichier} "
              f"({evts_sans_fichier} événements hors de portée)")
    part = 100.0 * len(trouve) / len(rows) if rows else 0.0
    print(f"  → LIGUE RETROUVÉE POUR : {len(trouve)} ({part:.1f} %)")

    if trouve:
        par_ligue = Counter(trouve.values())
        print(f"\n  Les {min(a.top, len(par_ligue))} ligues les plus récupérées")
        for nom, n in par_ligue.most_common(a.top):
            print(f"      {nom[:46]:46} {n:5}")
        print("\n  Par catégorie (leagues.categorize)")
        for cat, n in Counter(categorize(v) for v in trouve.values()).most_common():
            print(f"      {cat:16} {n:5}")
        print("\n  ⚠️ Le féminin et les jeunes sont SOUS-récupérés par construction "
              "(voir l'en-tête) :\n     un zéro dans ces catégories ne dit rien "
              "de ce que tu détectes vraiment.")

    if not a.apply:
        print("\nSonde seule — rien n'a été écrit. Ajoute --apply pour écrire.")
        return 0

    cur = con.cursor()
    cur.executemany(
        "UPDATE events SET league = ? "
        "WHERE event_key = ? AND (league IS NULL OR league = '')",
        [(ligue, ek) for ek, ligue in trouve.items()],
    )
    con.commit()
    print(f"\n✓ {cur.rowcount} ligne(s) de `events` mises à jour. "
          f"Aucune ligue existante n'a été écrasée.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
