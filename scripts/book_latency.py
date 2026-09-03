#!/usr/bin/env python3
"""Où passe le temps d'un cycle, et qu'est-ce qui le raccourcirait vraiment.

LE POINT QUI DÉCIDE DE TOUT
---------------------------
Les books d'un sport sont interrogés en parallèle et `fetch_all_parallel`
attend `as_completed` sur TOUS les futures. **Le fetch coûte donc le book le
plus lent, jamais la moyenne ni la somme.** Accélérer un book qui répond en 2 s
quand un autre en met 15 ne change RIEN à la durée du cycle : le seul book qui
compte est celui du chemin critique, et le gain qu'il offre est l'écart avec le
deuxième, pas sa propre durée.

C'est ce que cette sonde calcule. Elle classe les books non pas par lenteur
moyenne — un classement qui désigne le mauvais coupable — mais par le temps
qu'ils ont réellement fait perdre : combien de fois chacun a tenu le chemin
critique, et combien de secondes il a coûté AU-DESSUS du deuxième.

⚠️ ELLE NE MESURE QUE LE FETCH. Un cycle vaut le fetch PLUS l'analyse, les
écritures en base et les alertes. Si la somme des chemins critiques est très
inférieure à la durée annoncée par `Cycle N done in Xs`, le temps est ailleurs
et aucun book n'est en cause — la sonde le dit explicitement.

⚠️ LES CYCLES SONT REGROUPÉS PAR NUMÉRO, pas par horodatage. Un redémarrage
remet le compteur à 1 ; les cycles d'avant et d'après ne se mélangent pas.

⚠️ Elle a besoin des durées par book, ajoutées à `fetch_all_parallel` le
03/09/2026. Un journal antérieur n'en a aucune et la sonde le dira plutôt que
d'afficher un tableau vide.

Usage :
    .venv/bin/python -m scripts.book_latency
    .venv/bin/python -m scripts.book_latency --sport soccer
    CYCLE_LOG=/autre/chemin.log .venv/bin/python -m scripts.book_latency
"""
from __future__ import annotations

import argparse
import os
import re
import statistics as st
from collections import defaultdict
from pathlib import Path

LOG = os.getenv("CYCLE_LOG", "valuebet.log")

RE_CYCLE = re.compile(r"══ CYCLE (\d+) —")
# `[sport]   →  1234 quotes  Ladbrokes       3.2s`
RE_OK = re.compile(r"^\[(\w+)\]\s+→\s+(\d+) quotes\s+(\S+)\s+([\d.]+)s\s*$")
# `[sport]   Ladbrokes      15.1s skipped: ...`
RE_KO = re.compile(r"^\[(\w+)\]\s+(\S+)\s+([\d.]+)s skipped:")
RE_FAIT = re.compile(r"Cycle (\d+) done in (\d+)s")


def _pcent(vals: list[float], p: float) -> float:
    """Le p-centile, sans dépendance externe (numpy n'est pas sur la VM)."""
    if not vals:
        return 0.0
    xs = sorted(vals)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", default=LOG, help=f"Journal à lire (défaut : {LOG}).")
    ap.add_argument("--sport", default=None, help="Un seul sport.")
    ap.add_argument("--derniers", type=int, default=0, metavar="N",
                    help="Ne garder que les N derniers cycles.")
    a = ap.parse_args()

    chemin = Path(a.log)
    if not chemin.exists():
        raise SystemExit(
            f"Journal introuvable : {chemin}\n"
            "La sortie du daemon va dans valuebet.log, PAS dans journalctl — "
            "c'est scan-daemon.sh qui redirige.")

    # (cycle, sport) → [(durée, book, n_cotes)] ; -1 en n_cotes = échec
    par_lot: dict[tuple, list] = defaultdict(list)
    travail: dict[int, int] = {}
    cycle = 0
    for ligne in chemin.read_text(errors="replace").splitlines():
        m = RE_CYCLE.search(ligne)
        if m:
            cycle = int(m.group(1))
            continue
        m = RE_FAIT.search(ligne)
        if m:
            travail[int(m.group(1))] = int(m.group(2))
            continue
        m = RE_OK.match(ligne.strip())
        if m:
            sp, n, book, dt = m.group(1), int(m.group(2)), m.group(3), float(m.group(4))
        else:
            m = RE_KO.match(ligne.strip())
            if not m:
                continue
            sp, book, dt, n = m.group(1), m.group(2), float(m.group(3)), -1
        if a.sport and sp != a.sport:
            continue
        par_lot[(cycle, sp)].append((dt, book, n))

    if not par_lot:
        raise SystemExit(
            "Aucune durée par book dans ce journal.\n"
            "Elles sont écrites par `fetch_all_parallel` depuis le 03/09/2026 : "
            "un journal\nantérieur, ou un daemon pas encore redémarré sur cette "
            "version, n'en a pas.\nRedémarrer le daemon, laisser tourner "
            "quelques minutes, relancer.")

    if a.derniers:
        gardes = sorted({c for c, _ in par_lot})[-a.derniers:]
        par_lot = {k: v for k, v in par_lot.items() if k[0] in gardes}

    # Par book : ses durées, ses passages en chemin critique, le temps qu'il a
    # réellement coûté (l'écart avec le deuxième — le gain qu'il offrirait).
    durees: dict[str, list[float]] = defaultdict(list)
    critique: dict[str, int] = defaultdict(int)
    cout: dict[str, float] = defaultdict(float)
    echecs: dict[str, int] = defaultdict(int)
    somme_crit = 0.0
    for lot in par_lot.values():
        for dt, book, n in lot:
            durees[book].append(dt)
            if n < 0:
                echecs[book] += 1
        pire = max(lot)
        second = sorted(lot, reverse=True)[1][0] if len(lot) > 1 else 0.0
        critique[pire[1]] += 1
        cout[pire[1]] += pire[0] - second
        somme_crit += pire[0]

    n_lots = len(par_lot)
    cycles = sorted({c for c, _ in par_lot})
    print(f"\nOÙ PASSE LE TEMPS DU FETCH — {chemin}")
    print(f"{n_lots} lots (cycle × sport), cycles {cycles[0]} à {cycles[-1]}"
          + (f", sport {a.sport}" if a.sport else ""))

    ent = (f"{'book':14} {'n':>4} {'méd.':>6} {'p90':>6} {'max':>6}   "
           f"{'critique':>8} {'%':>5}   {'coût total':>10} {'par lot':>8}  éch.")
    print()
    print(ent)
    print("-" * len(ent))
    for book in sorted(durees, key=lambda b: -cout[b]):
        d = durees[book]
        print(f"{book:14} {len(d):4} {st.median(d):5.1f}s {_pcent(d, .9):5.1f}s "
              f"{max(d):5.1f}s   {critique[book]:8} {100*critique[book]/n_lots:4.0f}%"
              f"   {cout[book]:9.0f}s {cout[book]/n_lots:7.1f}s"
              f"  {echecs[book] or '':>4}")
    print("-" * len(ent))
    print(f"{'TOTAL':14} {'':4} {'':6} {'':6} {'':6}   {n_lots:8} {'100%':>5}"
          f"   {sum(cout.values()):9.0f}s {sum(cout.values())/n_lots:7.1f}s")

    print("\nCE QUE CHAQUE LEVIER RAPPORTERAIT VRAIMENT")
    print(f"  Durée moyenne du fetch (le chemin critique) : "
          f"{somme_crit / n_lots:.1f}s par lot.")
    classement = sorted(cout.items(), key=lambda kv: -kv[1])[:3]
    for book, c in classement:
        if c <= 0:
            continue
        print(f"  · rendre « {book} » instantané ferait gagner "
              f"{c / n_lots:.1f}s par lot en moyenne\n"
              f"    ({100 * c / somme_crit:.0f} % du temps de fetch), et ne "
              f"servirait que sur les {critique[book]} lots\n"
              f"    ({100 * critique[book] / n_lots:.0f} %) où il tient le "
              f"chemin critique.")
    if not classement or classement[0][1] <= 0:
        print("  Aucun book ne se détache : le fetch est limité par plusieurs "
              "books à la fois,\n  et en accélérer un seul ne rendrait presque "
              "rien.")

    # Le fetch n'est qu'une part du cycle. Sans cette comparaison, on
    # optimiserait des books alors que le temps est en base ou en analyse.
    communs = [c for c in cycles if c in travail]
    if communs:
        f_moy = somme_crit / n_lots
        t_moy = st.mean([travail[c] for c in communs])
        sports = len({s for _, s in par_lot})
        print(f"\n  ⚠️ LE FETCH N'EST PAS LE CYCLE. Cycle annoncé : "
              f"{t_moy:.0f}s de médiane sur {len(communs)} cycles ;\n"
              f"     fetch du lot le plus lent : ~{f_moy:.1f}s "
              f"({sports} sport(s) en parallèle).\n"
              f"     Reste ~{max(0.0, t_moy - f_moy):.0f}s hors fetch — "
              f"analyse, écritures en base, alertes.")
        if t_moy - f_moy > f_moy:
            print("     Ce reste DÉPASSE le fetch : optimiser les books ne "
                  "peut pas rendre plus que\n     la moitié, et le gros du "
                  "temps est ailleurs.")
    else:
        print("\n  ⚠️ Aucune ligne « Cycle N done in Xs » lisible : impossible "
              "de dire quelle part\n     du cycle le fetch représente.")
    print("\nLecture seule — aucune écriture, aucun réglage modifié.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
