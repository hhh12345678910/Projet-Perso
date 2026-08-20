"""Que jette-t-on ? — handicaps et mi-temps, mesurés sur l'API réelle. §21.10

Pourquoi cet outil existe
-------------------------
Vendre des value bets suppose du volume, et le projet n'exploite aujourd'hui
que deux marchés : `h2h` et `totals`. Sur 15 222 paris clôturés, aucun
handicap, aucune mi-temps. Deux exclusions volontaires en sont la cause, et
elles n'ont jamais été rouvertes depuis qu'elles ont été posées :

1. **Les handicaps sont jetés à la source** (`main.py:2069`) parce que les
   conventions de signe diffèrent — Pinnacle signe chaque côté (home −1.0 /
   away +1.0), les softs portent les deux côtés au même |ligne|. Les apparier
   fabrique des value bets fantômes. Le commentaire annonce **~45 % du payload
   de Pinnacle**, jamais revérifié.

2. **Les mi-temps sont filtrées** (`pinnacle.py:373`, `period != 0`). Et
   `circus.py:35` en conclut que « Pinnacle ne price que le match complet
   ici » — mais c'est notre propre filtre qui le fait croire. Raisonnement
   circulaire : personne n'a regardé le payload sans le filtre.

Cette sonde ne modifie RIEN. Elle lit l'API sans les deux filtres et compte ce
qu'ils écartent, pour que la décision d'élargir repose sur des nombres.

    .venv/bin/python -m scripts.market_expansion
    .venv/bin/python -m scripts.market_expansion --sport tennis

⚠️ Un volume écarté n'est pas un volume exploitable : il faut aussi un soft en
face. Ce que la sonde mesure est un PLAFOND, pas une prévision de détections.
Six hypothèses ont déjà été démenties par la mesure dans ce projet (§21.5).
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict

from src.scrapers.pinnacle import SPORT_IDS, PinnacleScraper


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sport", default="soccer,tennis",
                    help="Sports séparés par des virgules.")
    args = ap.parse_args()

    sc = PinnacleScraper()
    for sport in [s.strip() for s in args.sport.split(",") if s.strip()]:
        if sport not in SPORT_IDS:
            print(f"{sport}: sport inconnu, ignoré")
            continue
        print(f"\n{'=' * 64}\n{sport.upper()}\n{'=' * 64}")
        markets = sc._get(f"/sports/{SPORT_IDS[sport]}/markets/straight")
        if not isinstance(markets, list):
            print("payload inattendu")
            continue

        ouverts = [m for m in markets
                   if isinstance(m, dict) and m.get("status") in (None, "open")]
        print(f"{len(ouverts)} marchés ouverts au total")

        # 1. Ce que le filtre `period != 0` écarte.
        par_periode: Counter = Counter()
        pp_type: dict[object, Counter] = defaultdict(Counter)
        for m in ouverts:
            p = m.get("period")
            par_periode[p] += 1
            pp_type[p][m.get("type")] += 1

        print("\n1. Par période  ← `period != 0` est jeté aujourd'hui")
        print(f"   {'période':<10s} {'marchés':>8s}   détail")
        for p, n in sorted(par_periode.items(), key=lambda kv: -kv[1]):
            garde = " (GARDÉ)" if p == 0 else " (jeté)"
            detail = ", ".join(f"{t}={c}" for t, c in pp_type[p].most_common(4))
            print(f"   {str(p):<10s} {n:8d}{garde}   {detail}")

        jetes = sum(n for p, n in par_periode.items() if p != 0)
        if jetes:
            print(f"\n   → {jetes} marchés écartés par le filtre de période "
                  f"({100.0 * jetes / max(len(ouverts), 1):.1f} %).")
            print("   Le commentaire de circus.py:35 est donc à corriger : "
                  "Pinnacle\n     price bien autre chose que le match complet.")
        else:
            print("\n   → Aucun marché hors période 0. Le commentaire de "
                  "circus.py:35\n     est exact, et le filtre ne coûte rien.")

        # 2. Ce que le filtre handicap écarte, période 0 uniquement.
        p0 = [m for m in ouverts if m.get("period") == 0]
        par_type = Counter(m.get("type") for m in p0)
        print("\n2. Par type, en période 0  ← `spread` est jeté aujourd'hui")
        for t, n in par_type.most_common():
            garde = "jeté" if t == "spread" else "gardé"
            print(f"   {str(t):<14s} {n:6d}  ({100.0 * n / max(len(p0), 1):5.1f} %)  {garde}")

        # 3. La convention de signe, la raison même de l'exclusion.
        print("\n3. Convention de signe des `spread` — la raison de l'exclusion")
        exemples = 0
        signes: Counter = Counter()
        for m in p0:
            if m.get("type") != "spread":
                continue
            lignes = [pr.get("points") for pr in (m.get("prices") or [])
                      if isinstance(pr, dict)]
            lignes = [x for x in lignes if x is not None]
            if len(lignes) == 2:
                signes["opposés" if lignes[0] == -lignes[1] else "identiques"] += 1
                if exemples < 3:
                    print(f"   matchup {m.get('matchupId')} : points = {lignes}")
                    exemples += 1
        for k, v in signes.most_common():
            print(f"   côtés à lignes {k:<11s} : {v}")
        if signes.get("opposés"):
            print("\n   → Pinnacle signe bien chaque côté. Exploiter les handicaps\n"
                  "     suppose donc de normaliser la convention de CHAQUE soft\n"
                  "     avant tout appariement — c'est le travail réel, et c'est\n"
                  "     la classe de panne du §21.3 (appariement faux, silencieux).")


if __name__ == "__main__":
    main()
