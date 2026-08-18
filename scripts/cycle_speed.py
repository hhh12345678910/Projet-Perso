#!/usr/bin/env python3
"""Vitesse des cycles du daemon, lue dans valuebet.log.

La durée d'un cycle est le seul indicateur qui dise si la collecte suit le
marché. Un cycle qui s'allonge ne lève aucune erreur : les cotes arrivent
simplement plus vieilles, et les value bets se détectent après que le prix a
bougé — la panne est invisible dans les logs et coûteuse en CLV.

⚠️ Les redémarrages sont EXCLUS, et par le NUMÉRO de cycle, pas par la durée.
Un `systemctl restart` laisse souvent plusieurs minutes entre deux en-têtes —
mais pas toujours : mesuré, un redémarrage a produit un écart de 2 s entre le
cycle interrompu et le premier du nouveau processus. Un simple plafond de durée
laissait donc passer ces faux cycles courts, qui tiraient le minimum vers le
bas et faisaient croire à des cycles éclair. Le compteur repart à 1 : c'est ce
signal-là qui est fiable.

⚠️ DEUX mesures, et les confondre fait conclure de travers.

    PÉRIODE = écart entre deux en-têtes de cycle. Elle contient le travail ET
      le sommeil qui suit. C'est la fraîcheur réelle des cotes : le temps qui
      sépare deux relevés d'un même book.
    TRAVAIL = ce que le daemon annonce lui-même (`done in Xs`). C'est le coût
      du cycle, celui qui monte quand la base ralentit ou qu'un book traîne.

Période ≈ travail + 20 s. Cette version n'affichait que la période sous le nom
« durée d'un cycle » : on lisait 47 s là où le cycle en coûtait 26, et toute
comparaison avant/après une optimisation était faussée du même décalage.

⚠️ La sortie du daemon va dans `valuebet.log`, PAS dans journalctl — c'est
`scan-daemon.sh` qui redirige. Seuls l'ingestion et le listener écrivent au
journal.

Usage :
    .venv/bin/python scripts/cycle_speed.py
    CYCLE_LOG=/autre/chemin.log .venv/bin/python scripts/cycle_speed.py
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta
from pathlib import Path

LOG = Path(os.getenv("CYCLE_LOG",
                     str(Path(__file__).resolve().parent.parent / "valuebet.log")))
# Au-delà, ce n'est plus un cycle mais une coupure : redémarrage, veille, panne.
MAX_CYCLE_SEC = 600


def durations() -> list[float]:
    pat = re.compile(r"^══ CYCLE (\d+) — (\d{2}:\d{2}:\d{2}) UTC")
    ts: list[tuple[int, datetime]] = []
    with LOG.open(errors="ignore") as f:
        for line in f:
            m = pat.match(line)
            if m:
                ts.append((int(m.group(1)),
                           datetime.strptime(m.group(2), "%H:%M:%S")))
    out = []
    for (na, a), (nb, b) in zip(ts, ts[1:]):
        if nb <= na:                    # le compteur repart : redémarrage
            continue
        if b < a:                       # passage de minuit
            b += timedelta(days=1)
        s = (b - a).total_seconds()
        if 0 < s < MAX_CYCLE_SEC:
            out.append(s)
    return out


def work_durations() -> list[float]:
    """Durée annoncée par le daemon lui-même, sommeil exclu.

    Immunisée au piège des redémarrages : c'est le daemon qui chronomètre son
    propre travail, donc aucun écart entre deux processus ne peut s'y glisser.
    """
    pat = re.compile(r"^Cycle (\d+) done in (\d+)s")
    out = []
    with LOG.open(errors="ignore") as f:
        for line in f:
            m = pat.match(line)
            if m:
                v = float(m.group(2))
                if 0 < v < MAX_CYCLE_SEC:
                    out.append(v)
    return out


def main() -> int:
    if not LOG.exists():
        print(f"{LOG} introuvable. Le daemon a-t-il déjà tourné ?")
        return 1
    d = durations()
    if not d:
        print("Aucun cycle exploitable dans le journal.")
        return 1

    print("PÉRIODE — écart entre deux cycles (travail + sommeil). "
          "C'est la fraîcheur des cotes.")
    print(f"{'fenêtre':16} {'n':>4} {'médiane':>9} {'p90':>7} {'min':>6} {'max':>6}")
    print("-" * 52)
    for label, n in (("200 derniers", 200), ("60 derniers", 60),
                     ("20 derniers", 20)):
        x = sorted(d[-n:])
        if not x:
            continue
        print(f"{label:16} {len(x):>4} {x[len(x)//2]:>8.0f}s "
              f"{x[int(len(x)*0.9)]:>6.0f}s {x[0]:>5.0f}s {x[-1]:>5.0f}s")

    w = work_durations()
    if w:
        print("\nTRAVAIL — ce que le daemon annonce (`done in`), sommeil exclu. "
              "C'est le coût du cycle.")
        print(f"{'fenêtre':16} {'n':>4} {'médiane':>9} {'p90':>7} {'min':>6} {'max':>6}")
        print("-" * 52)
        for label, n in (("200 derniers", 200), ("60 derniers", 60),
                         ("20 derniers", 20)):
            x = sorted(w[-n:])
            if not x:
                continue
            print(f"{label:16} {len(x):>4} {x[len(x)//2]:>8.0f}s "
                  f"{x[int(len(x)*0.9)]:>6.0f}s {x[0]:>5.0f}s {x[-1]:>5.0f}s")

    recent = sorted(d[-20:])[len(d[-20:])//2]
    ancien = sorted(d[-200:-20])[len(d[-200:-20])//2] if len(d) > 40 else recent
    delta = recent - ancien
    print(f"\ntendance : {delta:+.0f}s entre les 20 derniers cycles et les "
          f"précédents")
    if recent > 60:
        print("⚠️ Au-delà d'une minute, les cotes ont le temps de bouger entre "
              "deux passages :\n   les détections arrivent après le mouvement, "
              "et la CLV en pâtit sans qu'aucune\n   erreur n'apparaisse.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
