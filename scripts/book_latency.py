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
# `[sport]   ⏱ base 8.2 fetch 12.1 fair 0.4 reste 3.8 tot 24.5s`
RE_PHASES = re.compile(r"^\[(\w+)\]\s+⏱\s+(.*?)\s+tot\s+([\d.]+)s\s*$")


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

    # ⚠️ TOUT EST INDEXÉ PAR (SÉRIE, CYCLE), JAMAIS PAR CYCLE SEUL.
    #
    # Un redémarrage remet le compteur à 1, et un journal en contient plusieurs.
    # Indexer par numéro seul faisait entrer en collision le cycle 20 d'avant
    # et le cycle 20 d'après — et surtout, `--derniers N` prenait les N PLUS
    # GRANDS NUMÉROS, c'est-à-dire la fin de la série la plus LONGUE, donc la
    # plus ANCIENNE. Demander « les 40 derniers cycles » après un redémarrage
    # rendait exactement les 40 cycles d'avant le redémarrage : l'option censée
    # isoler le régime courant servait l'ancien, en silence.
    #
    # La série s'incrémente dès que le numéro n'augmente pas strictement.
    par_lot: dict[tuple, list] = defaultdict(list)
    phases: list[tuple[tuple, str, dict, float]] = []
    travail: dict[tuple, int] = {}
    ordre_lots: list[tuple] = []          # (série, cycle) dans l'ordre du fichier
    serie, dernier_num, cycle = 0, None, 0
    cle = (0, 0)
    for ligne in chemin.read_text(errors="replace").splitlines():
        m = RE_CYCLE.search(ligne)
        if m:
            num = int(m.group(1))
            if dernier_num is None or num <= dernier_num:
                serie += 1
            dernier_num, cycle = num, num
            cle = (serie, num)
            if cle not in ordre_lots:
                ordre_lots.append(cle)
            continue
        m = RE_FAIT.search(ligne)
        if m:
            # Rattaché à la série COURANTE, pas au numéro nu.
            travail[(serie, int(m.group(1)))] = int(m.group(2))
            continue
        m = RE_PHASES.match(ligne.strip())
        if m:
            if not (a.sport and m.group(1) != a.sport):
                paires = re.findall(r"([a-zéè]+) ([\d.]+)", m.group(2))
                phases.append((cle, m.group(1),
                               {k: float(v) for k, v in paires},
                               float(m.group(3))))
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
        par_lot[(cle, sp)].append((dt, book, n))

    # ⚠️ NE SORTIR QUE SI LES DEUX MANQUENT. La première version sortait dès
    # qu'aucune durée par book n'était trouvée — elle jetait donc les lignes de
    # phases qu'elle venait de lire, et un journal qui n'aurait que celles-ci
    # (filtre `--sport`, chemin de fetch court-circuité) n'affichait rien du
    # tout en ayant l'air de fonctionner. Trouvé en vérifiant que
    # l'avertissement « reste » sortait bien : il ne sortait jamais.
    if not par_lot and not phases:
        raise SystemExit(
            "Aucune durée par book dans ce journal.\n"
            "Elles sont écrites par `fetch_all_parallel` depuis le 03/09/2026 : "
            "un journal\nantérieur, ou un daemon pas encore redémarré sur cette "
            "version, n'en a pas.\nRedémarrer le daemon, laisser tourner "
            "quelques minutes, relancer.")

    if not par_lot:
        print(f"\nOÙ PASSE LE TEMPS DU FETCH — {chemin}")
        print("  Aucune durée par book (les lignes `→ N quotes`) — seules les "
              "phases sont lisibles.")
        _bloc_phases(phases)
        print("\nLecture seule — aucune écriture, aucun réglage modifié.")
        return 0

    if a.derniers:
        # Par ordre du FICHIER, et appliqué aux DEUX tableaux : filtrer les
        # books sans filtrer les phases faisait comparer deux périodes.
        gardes = set(ordre_lots[-a.derniers:])
        par_lot = {k: v for k, v in par_lot.items() if k[0] in gardes}
        phases = [p for p in phases if p[0] in gardes]
        if not par_lot and not phases:
            raise SystemExit(
                f"--derniers {a.derniers} ne garde aucun cycle mesuré.")

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
    cycles = [c for c in ordre_lots if any(k[0] == c for k in par_lot)]
    print(f"\nOÙ PASSE LE TEMPS DU FETCH — {chemin}")
    print(f"{n_lots} lots (cycle × sport), cycles {cycles[0][1]} à "
          f"{cycles[-1][1]} sur {len({c[0] for c in cycles})} série(s)"
          + (f", sport {a.sport}" if a.sport else ""))
    # ⚠️ UN JOURNAL COUVRE PLUSIEURS VERSIONS DU CODE. Les durées par book
    # remontent à leur mise en place, les lignes de phases à la leur : quand
    # les deux échantillons ont des tailles très différentes, le tableau des
    # books mélange l'avant et l'après d'un changement, et une optimisation
    # récente y est diluée dans des milliers de mesures anciennes.
    if phases and len(phases) * 4 < n_lots:
        c_ph = [c for c in ordre_lots if any(p[0] == c for p in phases)]
        print(f"\n⚠️ CE TABLEAU MÉLANGE PLUSIEURS RÉGIMES. Les durées par book "
              f"couvrent {n_lots} lots\n   (cycles {cycles[0][1]}–{cycles[-1][1]}), "
              f"les phases seulement {len(phases)}\n   (cycles "
              f"{c_ph[0][1]}–{c_ph[-1][1]}). Tout changement récent est donc noyé "
              f"dans l'ancien.\n   Relancer avec `--derniers "
              f"{max(1, len(c_ph))}` pour ne juger que le régime courant.")

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
    # ⚠️ COMPARAISON CORRIGÉE. La première version soustrayait la moyenne DU
    # FETCH SUR TOUS LES LOTS de la durée du CYCLE — or le cycle est le MAX sur
    # les sports, pas leur moyenne. L'écart gonflait mécaniquement, et la sonde
    # annonçait « le reste dépasse le fetch, le gros du temps est ailleurs »
    # quand le tableau des phases, sur les mêmes cycles, disait 74 % de fetch.
    # Une sonde qui contredit sa propre mesure est pire qu'une sonde muette.
    #
    # Le bon rapprochement est cycle contre MAX PAR CYCLE du total des phases,
    # puisque c'est exactement ce que le daemon attend.
    par_cycle: dict[tuple, float] = {}
    for c, _sp, _d, t in phases:
        par_cycle[c] = max(par_cycle.get(c, 0.0), t)
    communs = [c for c in par_cycle if c in travail]
    if communs:
        t_moy = st.median([travail[c] for c in communs])
        p_moy = st.median([par_cycle[c] for c in communs])
        print(f"\n  LE CYCLE ET LE SPORT LE PLUS LENT — {len(communs)} cycles "
              f"où les deux sont lisibles")
        print(f"     cycle annoncé      : {t_moy:.1f}s de médiane")
        print(f"     sport le plus lent : {p_moy:.1f}s de médiane")
        print(f"     écart              : {t_moy - p_moy:+.1f}s — ce que le "
              f"cycle coûte EN PLUS du\n                          sport le "
              f"plus lent (démarrage des fils, journal).")
    elif travail:
        print("\n  ⚠️ Aucun cycle ne porte À LA FOIS un `done in` et des lignes "
              "de phases : la part\n     du fetch dans le cycle n'est pas "
              "calculable sur ce journal.")
    # ── Le partage du temps HORS fetch ────────────────────────────────────
    # Le fetch était la seule chose mesurée, et il ne fait que la moitié du
    # cycle. Sans ce bloc on optimisait des scrapers en ignorant une part
    # égale du temps, simplement parce qu'elle n'avait pas de nom.
    _bloc_phases(phases)

    print("\nLecture seule — aucune écriture, aucun réglage modifié.")
    return 0


def _bloc_phases(phases: list) -> None:
    """Le partage du temps d'un scan de sport, fetch compris."""
    if phases:
        noms = sorted({k for _c, _s, d, _t in phases for k in d},
                      key=lambda k: -sum(d.get(k, 0.0)
                                         for _c, _s, d, _t in phases))
        tot = st.mean([t for _c, _s, _d, t in phases])
        print(f"\nOÙ PASSE LE TEMPS D'UN SPORT — {len(phases)} scans mesurés")
        e2 = f"{'phase':10} {'médiane':>8} {'p90':>7} {'max':>7} {'part':>6}"
        print(e2)
        print("-" * len(e2))
        for k in noms:
            v = [d.get(k, 0.0) for _c, _s, d, _t in phases]
            print(f"{k:10} {st.median(v):7.1f}s {_pcent(v, .9):6.1f}s "
                  f"{max(v):6.1f}s {100 * st.mean(v) / tot if tot else 0:5.0f}%")
        print("-" * len(e2))
        tots = [t for _c, _s, _d, t in phases]
        print(f"{'TOTAL':10} {st.median(tots):7.1f}s {_pcent(tots, .9):6.1f}s "
              f"{max(tots):6.1f}s {'100%':>6}")
        reste = st.mean([d.get("reste", 0.0) for _c, _s, d, _t in phases])
        if tot and reste / tot > 0.25:
            print(f"\n  ⚠️ « reste » pèse {100 * reste / tot:.0f} % : c'est du "
                  f"temps qu'aucune phase ne revendique.\n     Tant qu'il "
                  f"domine, NOMMER une phase de plus rapporte davantage "
                  f"qu'optimiser\n     celles qu'on voit déjà.")
    else:
        print("\n  ⚠️ Aucune ligne de phases (`⏱`) dans ce journal : les 13,6 s "
              "hors fetch restent\n     un bloc opaque. Elles sont écrites "
              "depuis le 03/09/2026 — redémarrer le daemon.")


if __name__ == "__main__":
    raise SystemExit(main())
