"""Collecteur Unibet LIVE — lanceur autonome, borné. §PHASE 5

DÉLIBÉRÉMENT HORS DE `main.py` ET HORS DE systemd, comme le collecteur
AsianOdds. Il N'ÉCRIT RIEN : ni en base, ni sur Telegram. Il sonde, mesure et
affiche.

    .venv/bin/python -m scripts.live_unibet --minutes 5
    .venv/bin/python -m scripts.live_unibet --minutes 5 --apparier

`--apparier` ajoute le rapprochement avec nos `events` (LECTURE SEULE) pour
mesurer combien de matchs Unibet on sait retrouver. Sans lui, seul le sondage
est mesuré.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from src.unibet_live import PERIODE_SEC, collecter, resume_global
from src.storage import Storage


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--minutes", type=float, default=5.0)
    p.add_argument("--periode", type=float, default=PERIODE_SEC,
                   help=f"secondes entre deux sondages (défaut : {PERIODE_SEC:g})")
    p.add_argument("--sport", default="soccer")
    p.add_argument("--apparier", action="store_true",
                   help="rapprocher avec nos events (lecture seule)")
    p.add_argument("--db", default="data/valuebet.db")
    a = p.parse_args()

    debut = datetime.now(timezone.utc)
    print(f"[ub] démarrage {debut.isoformat()} — {a.minutes:.0f} min, "
          f"sondage toutes les {a.periode:.0f} s, AUCUNE écriture")
    cycles = collecter(duree_sec=a.minutes * 60, sport=a.sport,
                       periode_sec=a.periode,
                       storage=Storage(a.db) if a.apparier else None)
    duree = (datetime.now(timezone.utc) - debut).total_seconds()
    print(f"\n[ub] terminé en {duree:.0f} s")
    print(resume_global(cycles))
    return 0 if any(not c.erreur for c in cycles) else 1


if __name__ == "__main__":
    raise SystemExit(main())
