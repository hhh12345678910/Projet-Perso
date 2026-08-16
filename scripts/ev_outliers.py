#!/usr/bin/env python3
"""Une EV énorme : le book offre-t-il un cadeau, ou la référence est-elle fausse ?

Le §8 réclame un plafond d'EV de sécurité depuis juillet, et le §18.8 en donne
le cas d'école : +82,32 % sur Dundee United, où DEUX books indépendants
cotaient 3,50-3,65 contre une « juste » à 2,00. Quand plusieurs books s'accordent
et que la référence s'en écarte de 75 %, c'est la référence qui est fausse — et
on mise dessus.

Mais un plafond posé au jugé couperait aussi de la vraie value. Il faut d'abord
SAVOIR, cas par cas, de quel côté vient l'écart. C'est exactement ce que
`odds_history` permet : elle enregistre, pour chaque pari suivi, la cote de
TOUS les books au même instant. Le verdict se lit alors sans interprétation :

  · le book détecté est seul, loin de tous les autres
        -> l'anomalie est de son côté : appariement d'événement douteux,
           marché mal reconnu, ligne décalée.
  · plusieurs books s'accordent avec lui, seule la ligne juste s'en écarte
        -> l'anomalie est du côté de la RÉFÉRENCE. C'est le cas Dundee United,
           et c'est celui qu'un plafond d'EV doit couper.
  · pas assez de books pour trancher
        -> on ne conclut pas. Dire « indéterminé » vaut mieux qu'un verdict
           inventé sur deux points.

Usage :
    python3 scripts/ev_outliers.py                 # EV >= 30 %, 48 h
    python3 scripts/ev_outliers.py magicbetting    # un book en particulier
    EV_MIN=60 EV_HOURS=24 python3 scripts/ev_outliers.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from statistics import median

DB = os.getenv("VALUEBET_DB",
               str(Path(__file__).resolve().parent.parent / "data" / "valuebet.db"))
EV_MIN = float(os.getenv("EV_MIN", "30"))
HOURS = int(os.getenv("EV_HOURS", "48"))
LIMIT = int(os.getenv("EV_LIMIT", "40"))

# En dessous, on ne tranche pas. Deux books peuvent se tromper ensemble — ils
# partagent souvent une plateforme — et un verdict tiré de deux points serait
# du bruit présenté comme une conclusion.
MIN_PEERS = 3
# Écart relatif au-delà duquel une cote est « loin » des autres.
OUTLIER = 0.25

SHARP = {"pinnacle", "smarkets"}


def verdict(own: float, peers: dict[str, float], fair: float) -> tuple[str, str]:
    """(étiquette, explication) — ou « indéterminé » si les données manquent."""
    others = [o for b, o in peers.items() if b not in SHARP and o > 1.01]
    if len(others) < MIN_PEERS:
        return ("INDÉTERMINÉ",
                f"seulement {len(others)} autre(s) book sur cette sélection")

    med = median(others)
    # L'écart du book détecté à ses pairs, et celui de la ligne juste aux mêmes
    # pairs. C'est la comparaison qui tranche : les deux se mesurent contre la
    # MÊME référence indépendante, le consensus des softbooks.
    ecart_book = abs(own - med) / med
    ecart_fair = abs(fair - med) / med if fair else 0.0

    if ecart_book > OUTLIER and ecart_book > ecart_fair:
        return ("BOOK SUSPECT",
                f"seul à {own:.2f} quand les {len(others)} autres tiennent "
                f"{med:.2f} — appariement ou marché douteux")
    if ecart_fair > OUTLIER and ecart_fair >= ecart_book:
        return ("RÉFÉRENCE SUSPECTE",
                f"{len(others)} books tiennent {med:.2f} et la ligne juste dit "
                f"{fair:.2f} — c'est la référence qui s'écarte")
    return ("PLAUSIBLE",
            f"{len(others)} books à {med:.2f}, juste {fair:.2f} — écart normal")


def main() -> int:
    book = sys.argv[1] if len(sys.argv) > 1 else None
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    # Répartition par seuil AVANT le détail : c'est elle qui dit si les EV
    # extrêmes viennent d'une source en particulier. `reference_book` vaut NULL
    # quand la juste est calculée sur Pinnacle — la colonne n'existe que pour
    # distinguer les replis, donc l'absence de valeur EST l'information.
    print(f"=== EV extrêmes par seuil et par référence, {HOURS} h ===")
    seuils = c.execute(f"""
        SELECT CASE WHEN ev_pct >= 130 THEN '>= 130%'
                    WHEN ev_pct >= 120 THEN '120-130%'
                    WHEN ev_pct >= 100 THEN '100-120%'
                    ELSE  '{EV_MIN:.0f}-100%' END        AS tranche,
               COALESCE(reference_book, 'pinnacle')      AS reference,
               COUNT(*)
        FROM value_bets
        WHERE ev_pct >= ? AND detected_at >= datetime('now', '-{HOURS} hours')
              {"AND book = ?" if book else ""}
        GROUP BY tranche, reference ORDER BY 1 DESC, 3 DESC
    """, (EV_MIN, book) if book else (EV_MIN,)).fetchall()
    if seuils:
        print(f"\n{'tranche':12} {'référence':14} {'n':>5}")
        for t, r, n in seuils:
            print(f"{t:12} {r:14} {n:>5}")
    print()

    rows = c.execute(f"""
        SELECT v.id, v.book, v.market, v.outcome_label, v.line, v.odd_taken,
               v.fair_odd, v.ev_pct, v.detected_at,
               e.home, e.away, e.league, e.start_time,
               COALESCE(v.reference_book, 'pinnacle')
        FROM value_bets v LEFT JOIN events e ON e.event_key = v.event_key
        WHERE v.ev_pct >= ? AND v.detected_at >= datetime('now', '-{HOURS} hours')
              {"AND v.book = ?" if book else ""}
        ORDER BY v.ev_pct DESC LIMIT {LIMIT}
    """, (EV_MIN, book) if book else (EV_MIN,)).fetchall()

    if not rows:
        print(f"Aucune détection au-dessus de {EV_MIN:.0f} % d'EV sur {HOURS} h"
              + (f" pour {book}" if book else "") + ".")
        return 0

    tally: dict[str, int] = {}
    for (vid, b, market, label, line, odd, fair, ev, at,
         home, away, league, start, ref) in rows:
        # Le PREMIER point de chaque book après la détection : c'est l'instant
        # où la comparaison a du sens. Un point plus tardif décrirait un marché
        # qui a déjà bougé.
        peers = {}
        for pb, po in c.execute(
            "SELECT book, odd FROM odds_history WHERE value_bet_id = ? "
            "ORDER BY seen_at", (vid,)
        ):
            peers.setdefault(pb, po)
        tag, why = verdict(odd, peers, fair or 0.0)
        tally[tag] = tally.get(tag, 0) + 1

        ligne = f" {line:+g}" if line is not None else ""
        print(f"\n{ev:+7.1f}%  [{tag}]  {b}")
        print(f"   {home} — {away}   ({league or '?'}, {str(start)[:16]})")
        print(f"   {market}/{label}{ligne} @ {odd:.2f}   juste {fair or 0:.2f}"
              f"   [réf. {ref}]")
        if peers:
            vue = "  ".join(f"{pb}:{po:.2f}" for pb, po in sorted(peers.items()))
            print(f"   marché : {vue}")
        print(f"   → {why}")

    print("\n=== bilan ===")
    for tag, n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {tag:20} {n:>3}")
    print("\nUn plafond d'EV ne doit couper que « RÉFÉRENCE SUSPECTE ». "
          "« BOOK SUSPECT »\nrelève de l'appariement, et se corrige en amont — "
          "le plafonner masquerait\nle défaut sans le réparer.")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
