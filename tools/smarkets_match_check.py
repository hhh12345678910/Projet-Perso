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

⚠️ Coût des requêtes. `quotes` pèse des dizaines de Go et grossit de ~80 M de
lignes par jour. Les clés Pinnacle sont donc lues dans `events` (petite, clée
en primaire) et non dans `quotes` ; seules les clés Smarkets s'y lisent, sur
une fenêtre étroite et via l'index `fetched_at`. Chaque étape s'annonce avant
de partir : une sonde muette pendant deux minutes est indiscernable d'une
sonde plantée.

Usage :
    .venv/bin/python tools/smarkets_match_check.py
    .venv/bin/python tools/smarkets_match_check.py --sport tennis --minutes 10
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

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
    ap.add_argument("--minutes", type=float, default=5.0,
                    help="fenêtre de lecture des cotes Smarkets (défaut 5)")
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
    cut = (now - timedelta(minutes=args.minutes)).isoformat()

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

    # --- Smarkets : fenêtre étroite sur `quotes` --------------------------
    p(f"\n[2/4] Clés Smarkets des {args.minutes:g} dernières minutes…")
    t0 = time.monotonic()
    sm = {
        r[0] for r in conn.execute(
            "SELECT DISTINCT event_key FROM quotes "
            "WHERE fetched_at > ? AND book = 'smarkets'", (cut,)
        )
    }
    p(f"      {len(sm)} événements distincts  ({time.monotonic() - t0:.1f} s)")
    if not sm:
        p("\n      Aucune cote Smarkets sur la fenêtre. Élargis --minutes,")
        p("      ou vérifie que le rafraîchissement tourne :")
        p("        grep -i 'Smarkets rafraîchi' valuebet.log | tail -3")
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
