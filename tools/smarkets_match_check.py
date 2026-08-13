#!/usr/bin/env python3
"""Pourquoi Smarkets a-t-il si peu de points dans odds_history ?

Mesuré après branchement : 7 175 cotes Smarkets persistées par cycle, et 5 à 17
points d'historique, contre 800 à 1 700 pour chaque autre book. Un book qui
répond, des milliers de cotes, presque rien qui en sort — le §11.

Une courbe ne se trace que si la cote Smarkets porte la MÊME clé d'événement
que la sélection suivie, laquelle est clée sur Pinnacle. Tout se joue donc dans
le rapprochement flou des noms, exactement comme au §15.4 où Eurobet et
BetFirst perdaient tout leur tennis parce qu'ils écrivent « Nom, Prénom ».

Cet outil ne modifie rien. Il répond à trois questions :

  1. Le correctif d'alignement est-il seulement déployé ?
  2. Combien d'événements Pinnacle et Smarkets ont-ils en commun, une fois le
     rapprochement flou appliqué avec la tolérance du sport ?
  3. Quels sont les quasi-appariements rejetés, et à quel score ? C'est cette
     liste qui a révélé l'inversion nom/prénom du §15.4.

Usage :
    .venv/bin/python tools/smarkets_match_check.py
    .venv/bin/python tools/smarkets_match_check.py --minutes 60
"""
from __future__ import annotations

import argparse
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, ".")

from src.matcher import (  # noqa: E402
    parse_event_key,
    reconcile_event_keys,
    team_similarity,
    tolerance_for,
)

DB = "data/valuebet.db"


def _keys(conn, book: str, cut: str) -> set[str]:
    """Clés d'événement distinctes d'un book sur la fenêtre. Borné par
    fetched_at, qui est indexé — un COUNT sans borne sur `quotes` balaie des
    dizaines de Go et ne rend jamais la main."""
    return {
        r[0] for r in conn.execute(
            "SELECT DISTINCT event_key FROM quotes "
            "WHERE fetched_at > ? AND book = ?", (cut, book)
        )
    }


def _teams(key: str) -> str:
    return key.split("::", 1)[1] if "::" in key else key


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=30.0)
    ap.add_argument("--sport", default="soccer", choices=("soccer", "tennis"))
    ap.add_argument("--show", type=int, default=12, help="quasi-appariements à lister")
    args = ap.parse_args()

    print("=" * 72)
    try:
        head = subprocess.run(["git", "log", "--oneline", "-1"],
                              capture_output=True, text=True).stdout.strip()
        print(f"Code déployé : {head}")
        print("  ⚠️ Si 'align' n'apparaît pas dans l'historique récent, le")
        print("     correctif d'alignement n'est peut-être pas déployé :")
        print("     git log --oneline -3")
    except Exception:                                              # noqa: BLE001
        pass
    print("=" * 72)

    conn = sqlite3.connect(DB)
    cut = (datetime.now(timezone.utc) - timedelta(minutes=args.minutes)).isoformat()

    pin = _keys(conn, "pinnacle", cut)
    sm = _keys(conn, "smarkets", cut)
    print(f"\nFenêtre : {args.minutes:g} min")
    print(f"  Pinnacle : {len(pin):>5} événements distincts")
    print(f"  Smarkets : {len(sm):>5} événements distincts")
    if not sm:
        print("\n  Aucune cote Smarkets sur la fenêtre — rien à mesurer.")
        return 1

    # Identité stricte : ce qui marchait AVANT tout rapprochement.
    exact = pin & sm
    print(f"  Clés identiques au caractère près : {len(exact)}")

    # Rapprochement flou, avec la tolérance du sport.
    scores: dict[str, float] = {}
    mapping = reconcile_event_keys(
        reference_keys=list(pin),
        candidate_keys=sm,
        time_tolerance_minutes=tolerance_for(args.sport),
        scores=scores,
    )
    print(f"  Appariés par rapprochement flou   : {len(mapping)}")
    if len(sm):
        print(f"  → taux d'appariement : {100 * len(mapping) / len(sm):.1f} %")

    if len(mapping) > len(exact):
        print("\n  ✅ Le rapprochement flou apporte "
              f"{len(mapping) - len(exact)} appariements que l'égalité stricte")
        print("     ratait. C'est ce que fait align_reference_source.")

    # Les rejetés : c'est cette liste qui a révélé l'inversion du §15.4.
    unmatched = [k for k in sm if k not in mapping]
    print(f"\n{len(unmatched)} événements Smarkets non appariés.")
    print("Certains sont normaux — Pinnacle ne price pas tout, c'est même la")
    print("raison d'être du repli. Les suspects sont ceux dont un candidat")
    print("Pinnacle ressemble beaucoup au nom sans passer le seuil.\n")

    near = []
    for k in unmatched:
        pk = parse_event_key(k)
        if pk is None:
            continue
        best, best_s = None, 0.0
        for r in pin:
            pr = parse_event_key(r)
            if pr is None:
                continue
            # Même journée seulement, sinon la comparaison n'a pas de sens.
            if abs((pr[0] - pk[0]).total_seconds()) > 6 * 3600:
                continue
            s = (team_similarity(pk[1], pr[1]) + team_similarity(pk[2], pr[2])) / 2
            if s > best_s:
                best, best_s = r, s
        if best is not None and best_s >= 60:
            near.append((best_s, k, best))

    near.sort(reverse=True)
    if not near:
        print("  Aucun quasi-appariement : les non-appariés sont bien des")
        print("  matchs que Pinnacle ne price pas. Rien à corriger.")
    else:
        print(f"  {len(near)} quasi-appariements (score >= 60) — les plus proches :\n")
        for s, k, r in near[:args.show]:
            verdict = "REJETÉ" if s < 85 else "?? devrait passer"
            print(f"   {s:5.1f}  {verdict}")
            print(f"          smarkets : {_teams(k)}")
            print(f"          pinnacle : {_teams(r)}")
        print("\n  Un score élevé et pourtant rejeté = piège de clé, comme le")
        print("  §15.4 (« Griekspoor, Tallon » contre « Tallon Griekspoor »).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
