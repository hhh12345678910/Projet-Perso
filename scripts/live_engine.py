"""Moteur LIVE — observation locale, bornée. §PHASE 5, commit 2

AsianOdds fait le prix juste (lu dans `market_state`), Unibet LIVE le prend
(sondé en mémoire, hors de la boucle prématch).

    .venv/bin/python -m scripts.live_engine --minutes 10
    .venv/bin/python -m scripts.live_engine --minutes 10 --tout

N'ÉCRIT RIEN : ni en base, ni sur Telegram. Aucune alerte n'est envoyée à
cette étape, et le module `alerter` n'est même pas importé. Le lanceur
affiche une ligne par occasion et un compte rendu final.

Par défaut, seules les occasions NON dupliquées sont affichées. `--tout`
montre aussi les doublons, ce qui sert à vérifier que la déduplication
travaille au lieu de le supposer.
"""
from __future__ import annotations

import argparse
import time
from collections import Counter
from datetime import datetime, timezone

from src.live_value import (
    AGE_MAX_FAIR_SEC, AGE_MAX_PRENEUR_SEC, SEUIL_EV_PCT, Memoire, Statut,
    evaluer, resume)
from src.storage import Storage
from src.unibet_live import PERIODE_SEC, UnibetLive, apparier


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--minutes", type=float, default=10.0)
    p.add_argument("--periode", type=float, default=PERIODE_SEC)
    p.add_argument("--sport", default="soccer")
    p.add_argument("--db", default="data/valuebet.db")
    p.add_argument("--ev", type=float, default=SEUIL_EV_PCT,
                   help=f"seuil d'EV en %% (défaut : {SEUIL_EV_PCT:g})")
    p.add_argument("--age-fair", type=float, default=AGE_MAX_FAIR_SEC,
                   help=f"âge max d'une ligne AsianOdds (défaut : "
                        f"{AGE_MAX_FAIR_SEC:g} s)")
    p.add_argument("--age-preneur", type=float, default=AGE_MAX_PRENEUR_SEC,
                   help=f"âge max d'une cote Unibet (défaut : "
                        f"{AGE_MAX_PRENEUR_SEC:g} s)")
    p.add_argument("--tout", action="store_true",
                   help="afficher aussi les doublons")
    a = p.parse_args()

    storage = Storage(a.db)
    live = UnibetLive(a.sport)
    memoire = Memoire()
    total: Counter = Counter()
    cumul = None
    passages = erreurs = 0

    debut = datetime.now(timezone.utc)
    print(f"[live] démarrage {debut.isoformat()} — {a.minutes:g} min, "
          f"sondage {a.periode:g} s, EV > {a.ev:g} %, AUCUNE écriture, "
          f"AUCUNE alerte")
    fin = time.monotonic() + a.minutes * 60
    try:
        while time.monotonic() < fin:
            c = live.sonder()
            if c.erreur:
                erreurs += 1
                print(f"[live] sondage en erreur : {c.erreur}")
                time.sleep(a.periode)
                continue
            passages += 1
            maintenant = datetime.now(timezone.utc)
            app = apparier(live.instantane, storage, maintenant, a.sport)
            an = evaluer(app.quotes, storage, maintenant,
                         memoire=memoire,
                         preneur_pris_a=live.instantane.pris_a,
                         seuil_ev=a.ev, age_max_fair=a.age_fair,
                         age_max_preneur=a.age_preneur)
            for o in (an.opportunites if a.tout else an.nouvelles):
                print(o.ligne())
            total.update(an.par_statut)
            total["quotes"] += an.quotes_analysees
            total["sous_seuil"] += an.sous_seuil
            total["matchs"] = max(total["matchs"], an.matchs_analyses)
            total["apparies"] = max(total["apparies"], app.matchs_apparies)
            total["inversions"] += app.inversions
            cumul = an
            if time.monotonic() + a.periode > fin:
                break
            time.sleep(a.periode)
    except KeyboardInterrupt:
        print("\n[live] interrompu")
    finally:
        live.close()

    duree = (datetime.now(timezone.utc) - debut).total_seconds()
    print(f"\n[live] terminé en {duree:.0f} s — {passages} passage(s), "
          f"{erreurs} sondage(s) en erreur")
    if cumul is not None:
        print("\nDERNIER PASSAGE")
        print(resume(cumul))
    print("\nCUMUL DU RUN")
    print(f"  matchs analysés (max)     {total['matchs']}")
    print(f"  matchs appariés (max)     {total['apparies']}")
    print(f"  quotes analysées          {total['quotes']}")
    print(f"  EV sous le seuil          {total['sous_seuil']}")
    for s in Statut:
        print(f"  {s.value:<24}  {total.get(s.value, 0)}")
    return 0 if passages else 1


if __name__ == "__main__":
    raise SystemExit(main())
