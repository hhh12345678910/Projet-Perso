#!/usr/bin/env python3
"""Pourquoi Smarkets a-t-il si peu de points dans odds_history ?

Mesuré après branchement : ~7 000 cotes Smarkets persistées par cycle, et 5 à
17 points d'historique, contre 800 à 1 700 pour chaque autre book. Un book qui
répond, des milliers de cotes, presque rien qui en sort — le §11.

Une courbe ne se trace que si la cote Smarkets porte la MÊME clé d'événement
que la sélection suivie, laquelle est clée sur Pinnacle. Tout se joue donc dans
le rapprochement flou des noms, exactement comme au §15.4 où Eurobet et
BetFirst perdaient tout leur tennis en écrivant « Nom, Prénom ».

Cet outil ne modifie rien.

⚠️ `quotes` n'est jamais interrogée. Elle pèse des dizaines de Go et grossit de
~80 M de lignes par jour : même bornée par l'index `fetched_at`, une requête y
déclenche des centaines de milliers de lectures aléatoires. Les clés Pinnacle
viennent donc d'`events` (petite, clée en primaire) et celles de Smarkets sont
relues depuis l'API, ce qui prend une quinzaine de secondes et vérifie au
passage que le scraper fonctionne.

Chaque étape s'annonce avant de partir : une sonde muette pendant deux minutes
est indiscernable d'une sonde plantée.

Usage :
    .venv/bin/python tools/smarkets_match_check.py
    .venv/bin/python tools/smarkets_match_check.py --sport tennis
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, ".")

from src.matcher import (  # noqa: E402
    parse_event_key,
    reconcile_event_keys,
    team_similarity,
    tolerance_for,
)

DB = "data/valuebet.db"


def p(msg: str = "") -> None:
    print(msg, flush=True)


def _teams(key: str) -> str:
    return key.split("::", 1)[1] if "::" in key else key


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sport", default="soccer", choices=("soccer", "tennis"))
    ap.add_argument("--show", type=int, default=10)
    ap.add_argument("--max-near", type=int, default=120,
                    help="nombre de non-appariés passés au calcul de proximité")
    args = ap.parse_args()

    p("=" * 72)
    try:
        head = subprocess.run(["git", "log", "--oneline", "-1"],
                              capture_output=True, text=True).stdout.strip()
        p(f"Code déployé : {head}")
    except Exception:                                              # noqa: BLE001
        pass
    p("=" * 72)

    conn = sqlite3.connect(DB)
    now = datetime.now(timezone.utc)

    # --- Pinnacle : lu dans `events`, pas dans `quotes` -------------------
    p(f"\n[1/4] Événements Pinnacle du sport ({args.sport})…")
    t0 = time.monotonic()
    pin = {
        r[0] for r in conn.execute(
            "SELECT event_key FROM events WHERE sport = ? AND start_time > ?",
            (args.sport, now.isoformat()),
        )
    }
    p(f"      {len(pin)} événements à venir  ({time.monotonic() - t0:.1f} s)")

    # --- Smarkets : relu depuis l'API, JAMAIS depuis `quotes` -------------
    #
    # Lire `quotes` ici était une fausse bonne idée : l'index sur fetched_at
    # rend les identifiants de ligne, mais il faut ensuite aller chercher
    # chaque ligne dans une table de plusieurs dizaines de Go pour tester le
    # book — ~290 000 lectures aléatoires sur cinq minutes, dont 2 % seulement
    # concernent Smarkets. L'API rend la même information en une quinzaine de
    # secondes, et vérifie en prime que le scraper fonctionne.
    p("\n[2/4] Clés Smarkets, relues depuis l'API…")
    t0 = time.monotonic()
    from src.scrapers.smarkets import SmarketsScraper, iter_all_quotes_fast
    try:
        with SmarketsScraper() as _sm:
            sm = {q.event_key for q in iter_all_quotes_fast(_sm, args.sport)}
    except Exception as e:                                         # noqa: BLE001
        p(f"      ÉCHEC — {e}")
        return 1
    p(f"      {len(sm)} événements distincts  ({time.monotonic() - t0:.1f} s)")
    if not sm:
        p("\n      L'API n'a rien rendu pour ce sport.")
        return 1

    # Un événement Smarkets peut porter un sport que l'on ne scanne pas ici ;
    # on ne garde donc que ceux dont le coup d'envoi est encore devant nous.
    sm = {k for k in sm if (parse_event_key(k) or (now,))[0] > now}
    p(f"      dont {len(sm)} encore à venir")

    # --- Rapprochement ----------------------------------------------------
    p("\n[3/4] Rapprochement flou…")
    t0 = time.monotonic()
    exact = pin & sm
    scores: dict[str, float] = {}
    mapping = reconcile_event_keys(
        reference_keys=list(pin),
        candidate_keys=sm,
        time_tolerance_minutes=tolerance_for(args.sport),
        scores=scores,
    )
    p(f"      identiques au caractère près : {len(exact)}")
    p(f"      appariés par rapprochement   : {len(mapping)}"
      f"  ({time.monotonic() - t0:.1f} s)")
    if sm:
        p(f"      → taux d'appariement : {100 * len(mapping) / len(sm):.1f} %")
    if len(mapping) > len(exact):
        p(f"      ✅ le flou récupère {len(mapping) - len(exact)} appariements "
          f"que l'égalité stricte ratait")

    # --- Quasi-appariements rejetés ---------------------------------------
    unmatched = [k for k in sm if k not in mapping]
    p(f"\n[4/4] {len(unmatched)} non appariés — recherche de quasi-appariements")
    p("      (un match que Pinnacle ne price pas est NORMAL : c'est la raison")
    p("       d'être du repli. Le suspect, c'est un nom très proche rejeté.)")
    if not unmatched:
        p("\n      Aucun. Rien à corriger.")
        return 0

    pin_parsed = [(k, parse_event_key(k)) for k in pin]
    pin_parsed = [(k, v) for k, v in pin_parsed if v is not None]
    near = []
    t0 = time.monotonic()
    for k in unmatched[: args.max_near]:
        pk = parse_event_key(k)
        if pk is None:
            continue
        best, best_s = None, 0.0
        for r, pr in pin_parsed:
            if abs((pr[0] - pk[0]).total_seconds()) > 6 * 3600:
                continue
            s = (team_similarity(pk[1], pr[1]) + team_similarity(pk[2], pr[2])) / 2
            if s > best_s:
                best, best_s = r, s
        if best is not None and best_s >= 60:
            near.append((best_s, k, best))
    p(f"      {len(unmatched[:args.max_near])} examinés "
      f"({time.monotonic() - t0:.1f} s)")

    near.sort(reverse=True)
    if not near:
        p("\n      Aucun quasi-appariement. Les non-appariés sont bien des")
        p("      matchs absents de Pinnacle — couverture complémentaire,")
        p("      rien à corriger dans le rapprochement.")
        return 0

    p(f"\n      {len(near)} quasi-appariements (score ≥ 60), les plus proches :\n")
    for s, k, r in near[: args.show]:
        verdict = "REJETÉ" if s < 85 else "?? devrait passer"
        p(f"       {s:5.1f}  {verdict}")
        p(f"              smarkets : {_teams(k)}")
        p(f"              pinnacle : {_teams(r)}")
    p("\n      Un score élevé et pourtant rejeté = piège de clé, comme le")
    p("      §15.4 (« Griekspoor, Tallon » contre « Tallon Griekspoor »).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
