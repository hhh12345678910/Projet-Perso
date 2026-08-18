#!/usr/bin/env python3
"""P&L sur TOUTES les détections, pas seulement sur celles qu'on a cliquées.

`track-update` mesure les paris joués. C'est utile, mais c'est une population
choisie à la main, et le projet a mesuré quatre fois que cette sélection
n'apporte rien : jouées +10,73 % contre non jouées +9,87 %, t = +0,90. Ce
qu'on gagne en la retirant, c'est un effectif bien plus grand et l'absence de
tout biais — un pari qu'on a « oublié » de jouer parce qu'on dormait n'est pas
un pari qu'on a écarté.

⚠️ La mise est NOTIONNELLE et constante. Une détection non jouée n'a pas de
mise ; en imposer une identique à toutes est le seul choix honnête, et il rend
le ROI directement comparable d'un segment à l'autre. Ce n'est donc pas de
l'argent, c'est un rendement par unité risquée.

⚠️ Déduplication sur `équipes + jour + marché + pari`, jamais sur `event_key` :
au tennis Pinnacle révise son horaire par pas de 15 minutes et fabrique jusqu'à
onze clés pour un seul match (§17.8). Sans ça les effectifs sont gonflés et les
significativités avec.

Usage :
    .venv/bin/python scripts/pnl_detections.py
    .venv/bin/python scripts/pnl_detections.py --premium      # canal premium seul
    .venv/bin/python scripts/pnl_detections.py --stake 25
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.clv import pnl as clv_pnl  # noqa: E402
from src.clv import settle as clv_settle  # noqa: E402


def is_premium(odd: float, ev: float) -> bool:
    """Les seuils de production, recopiés — cote 1,5-4 dès 8 % d'EV, ou 4-6
    dès 20 %. Les relire ici plutôt que d'inventer garantit qu'on mesure le
    canal tel qu'il alerte vraiment."""
    return (1.5 <= odd <= 4 and ev >= 8) or (4 < odd <= 6 and ev >= 20)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--premium", action="store_true",
                    help="Ne garder que le canal premium.")
    ap.add_argument("--stake", type=float, default=25.0,
                    help="Mise notionnelle par pari.")
    a = ap.parse_args()

    c = sqlite3.connect(a.db)
    c.row_factory = sqlite3.Row
    rows = list(c.execute("""
        SELECT vb.event_key, vb.book, vb.market, vb.outcome_label, vb.line,
               vb.odd_taken, vb.ev_pct, vb.detected_at,
               e.sport AS sport, e.home AS home, e.away AS away,
               e.start_time AS start_time,
               r.winner, r.home_score, r.away_score
        FROM value_bets vb
        JOIN results r ON r.event_key = vb.event_key
        LEFT JOIN events e ON e.event_key = vb.event_key
    """))
    if not rows:
        print("Aucune détection avec résultat. Lance `results-update`.")
        return 1

    # Déduplication : une opportunité = équipes + jour + marché + pari.
    best: dict[tuple, sqlite3.Row] = {}
    for r in rows:
        if a.premium and not is_premium(float(r["odd_taken"]), float(r["ev_pct"])):
            continue
        key = ((r["home"] or "").lower(), (r["away"] or "").lower(),
               (r["start_time"] or "")[:10], r["market"], r["outcome_label"],
               r["line"])
        prev = best.get(key)
        if prev is None or float(r["odd_taken"]) > float(prev["odd_taken"]):
            best[key] = r
    opp = list(best.values())

    def report(label: str, sub: list, warn: int = 50) -> None:
        w = l = v = 0
        total = 0.0
        staked = 0.0
        odds = []
        for r in sub:
            status = clv_settle(r["market"], r["outcome_label"], r["line"],
                                r["winner"], r["home_score"], r["away_score"])
            p = clv_pnl(status, float(r["odd_taken"]), a.stake)
            if p is None:            # résultat insuffisant pour ce marché
                continue
            staked += a.stake
            total += p
            odds.append(float(r["odd_taken"]))
            if status == "won":
                w += 1
            elif status == "lost":
                l += 1
            else:
                v += 1
        n = w + l + v
        if not n:
            print(f"  {label:22} —")
            return
        roi = 100 * total / staked if staked else 0.0
        wr = 100 * w / (w + l) if (w + l) else 0.0
        # Écart-type du ROI : de quoi savoir si le chiffre veut dire quelque chose.
        per = [clv_pnl(clv_settle(r["market"], r["outcome_label"], r["line"],
                                  r["winner"], r["home_score"], r["away_score"]),
                       float(r["odd_taken"]), a.stake) for r in sub]
        per = [x for x in per if x is not None]
        sd = st.stdev(per) if len(per) > 1 else 0.0
        sigma = (total / (sd * math.sqrt(len(per)))) if sd > 0 else float("nan")
        flag = " ⚠️" if n < warn else ""
        print(f"  {label:22} n={n:5}  ROI {roi:+7.2f}%  {sigma:5.1f}σ  "
              f"gagnés {wr:5.1f}%  cote moy {st.mean(odds):.2f}  "
              f"P&L {total:+9.0f}€{flag}")

    scope = "canal premium" if a.premium else "toutes détections"
    print(f"\nP&L NOTIONNEL — {scope}, mise constante de {a.stake:.0f} €")
    print(f"{len(opp)} opportunités dédupliquées, sur {len(rows)} lignes\n")

    report("TOTAL", opp)
    print()

    # ⚠️ LA découpe qui teste la qualité de la référence.
    #
    # Un devig biaisé aux cotes hautes rend une « ligne juste » trop généreuse
    # pour les outsiders : ces paris affichent alors de l'EV et de la CLV sans
    # que l'argent suive. La signature est nette — edge positif sur les cotes
    # basses, négatif sur les hautes. Si le ROI décroît régulièrement quand la
    # cote monte, ce n'est pas la malchance, c'est la méthode de devig.
    print("  par tranche de cote  ⚠️ décroissance régulière = devig suspect")
    bands = [("1.0-1.8", 1.0, 1.8), ("1.8-2.3", 1.8, 2.3), ("2.3-3.0", 2.3, 3.0),
             ("3.0-4.0", 3.0, 4.0), ("4.0-6.0", 4.0, 6.0), ("> 6.0", 6.0, 1e9)]
    for lab, lo, hi in bands:
        report(f"    cote {lab}",
               [r for r in opp if lo <= float(r["odd_taken"]) < hi], warn=30)
    print()

    for field, title in (("sport", "par sport"), ("market", "par marché"),
                         ("book", "par book")):
        by = defaultdict(list)
        for r in opp:
            by[r[field] or "?"].append(r)
        print(f"  {title}")
        for k, sub in sorted(by.items(), key=lambda kv: -len(kv[1])):
            report(f"    {k}", sub, warn=30)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
