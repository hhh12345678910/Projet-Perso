"""Cadence des cycles prématch — LECTURE SEULE. §PHASE 5

Répond à une question précise : le LIVE a-t-il ralenti le daemon prématch ?

    # les 12 dernières heures, coupure automatique à la médiane
    .venv/bin/python -m scripts.cadence_prematch

    # comparer explicitement avant / après le démarrage du LIVE
    .venv/bin/python -m scripts.cadence_prematch --coupure 2026-08-26T19:00

IL N'EXISTE AUCUNE TABLE DE DURÉE DE CYCLE. Le daemon affiche seulement
« Cycle N done in Xs » dans son log, qui n'est pas structuré. On reconstruit
donc la cadence depuis les INSTANTS D'ÉCRITURE de `quotes` : un cycle écrit
ses cotes en rafale, donc un trou de plus de `--trou` secondes entre deux
écritures marque la frontière entre deux cycles.

La base est ouverte en LECTURE SEULE (`mode=ro`) : impossible de gêner le
daemon, même s'il écrit au même moment.
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone


def _stats(xs, nom, log=print) -> None:
    if not xs:
        log(f"  {nom:<24} aucun cycle")
        return
    d = sorted(xs)
    log(f"  {nom:<24} n={len(d):<5} médiane {statistics.median(d):6.1f} s"
        f"   p95 {d[int(0.95 * (len(d) - 1))]:6.1f} s   max {d[-1]:6.1f} s")


def cycles(base: str, heures: float, trou: float) -> list:
    """(instant de début, durée) pour chaque cycle de la fenêtre."""
    depuis = (datetime.now(timezone.utc) - timedelta(hours=heures)).isoformat()
    cx = sqlite3.connect(f"file:{base}?mode=ro", uri=True)
    try:
        ts = [datetime.fromisoformat(r[0]) for r in cx.execute(
            "SELECT DISTINCT fetched_at FROM quotes WHERE fetched_at > ?"
            " ORDER BY fetched_at", (depuis,))]
    finally:
        cx.close()
    if len(ts) < 4:
        return []
    debuts = [ts[0]] + [b for a, b in zip(ts, ts[1:])
                        if (b - a).total_seconds() > trou]
    return [(a, (b - a).total_seconds()) for a, b in zip(debuts, debuts[1:])]


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="data/valuebet.db")
    p.add_argument("--heures", type=float, default=12.0)
    p.add_argument("--trou", type=float, default=20.0,
                   help="secondes sans écriture qui séparent deux cycles")
    p.add_argument("--coupure", metavar="ISO",
                   help="instant UTC séparant AVANT et APRÈS "
                        "(ex. 2026-08-26T19:00). Défaut : la médiane.")
    a = p.parse_args()

    c = cycles(a.db, a.heures, a.trou)
    if len(c) < 3:
        print(f"moins de 3 cycles détectés sur {a.heures:g} h — "
              f"élargir --heures, ou le daemon ne tourne pas")
        return 1

    print(f"\n{'═' * 68}\nCADENCE DES CYCLES PRÉMATCH — {a.heures:g} dernières heures")
    print(f"{len(c)} cycles, du {c[0][0]:%d/%m %H:%M} au {c[-1][0]:%d/%m %H:%M}"
          f" UTC\n{'═' * 68}")
    _stats([d for _, d in c], "ensemble")

    if a.coupure:
        coupure = datetime.fromisoformat(a.coupure)
        if coupure.tzinfo is None:
            coupure = coupure.replace(tzinfo=timezone.utc)
        etiq = f"coupure donnée : {coupure:%d/%m %H:%M} UTC"
    else:
        coupure = c[len(c) // 2][0]
        etiq = (f"aucune coupure donnée — comparaison à la médiane temporelle "
                f"({coupure:%H:%M} UTC)")
    print(f"\n  {etiq}")
    avant = [d for t, d in c if t < coupure]
    apres = [d for t, d in c if t >= coupure]
    _stats(avant, "AVANT")
    _stats(apres, "APRÈS")

    if avant and apres:
        ma, mp = statistics.median(avant), statistics.median(apres)
        pct = 100 * (mp - ma) / ma
        print(f"\n  écart : {mp - ma:+.1f} s  ({pct:+.1f} %)")
        # 10 % est une borne de lecture, pas un test statistique : la cadence
        # depend aussi du nombre de matchs offerts, qui varie dans la journee.
        # Un ecart sous 10 % ne prouve pas l'absence d'effet, il dit qu'on ne
        # le distingue pas du bruit ordinaire.
        if pct > 10:
            print("  >>> le cycle a RALENTI de façon visible")
        elif pct < -10:
            print("  >>> le cycle a ACCÉLÉRÉ")
        else:
            print("  >>> pas d'écart distinguable du bruit ordinaire")
        print("\n  ⚠️ la cadence dépend aussi du NOMBRE DE MATCHS offerts, qui"
              "\n     varie selon l'heure. Comparer deux moments de la journée"
              "\n     n'isole pas l'effet du LIVE à lui seul.")
    print(f"{'═' * 68}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
