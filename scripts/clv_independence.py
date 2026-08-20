"""La CLV mesure-t-elle autre chose que l'EV ? — §20.4

Pourquoi cet outil existe
-------------------------
Le §20.4 est la question ouverte la plus importante du projet : la CLV annonce
+9 à +10 % au tennis quand le ROI réalisé sort à −0,34 % sur 2 945 opportunités,
soit 3,8 σ. Trois explications y sont listées, aucune tranchée.

Cette sonde en teste une quatrième, mécanique. La CLV vaut
`odd_taken / fair_clôture − 1` et l'EV vaut `odd_taken / fair_détection − 1` :
les deux partagent le même numérateur, et ne diffèrent QUE par le déplacement
de la ligne juste de Pinnacle entre les deux instants. Si cette ligne ne bouge
pas, alors CLV = EV identiquement — la CLV ne confirme plus l'edge, elle
réénonce l'estimation du modèle par elle-même. Elle est alors nécessairement
belle, et le ROI n'a aucune raison de la suivre.

Ce n'est pas une hypothèse gratuite : `under_threshold` mesure une pente CLV/EV
de +0,86 (h2h), +0,71 (over) et +1,09 (under) — soit 70 à 110 % de l'edge
détecté qui se retrouve tel quel à la clôture.

Le chiffre qui tranche est le **R²**. S'il approche 1, la CLV est une fonction
déterministe de l'EV et n'apporte aucune information indépendante, quelle que
soit sa moyenne.

    .venv/bin/python -m scripts.clv_independence
    .venv/bin/python -m scripts.clv_independence --sport tennis

Mêmes sources que `clv-report` et `clv_split` : `Storage.all_closed_bets()` et
`clv.clv_pct` (§17.7).
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
from statistics import mean, median, pstdev

from src.clv import clv_pct
from src.config import ScanConfig
from src.storage import Storage


def _heures(a: str | None, b: str | None) -> float | None:
    """Écart en heures entre deux horodatages ISO, ou None si illisible."""
    if not a or not b:
        return None
    try:
        da, db = datetime.fromisoformat(a), datetime.fromisoformat(b)
    except ValueError:
        return None
    if (da.tzinfo is None) != (db.tzinfo is None):
        return None
    return (db - da).total_seconds() / 3600.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sport", default="", help="Restreindre à un sport.")
    ap.add_argument("--since", default="", help="Date de détection minimale (AAAA-MM-JJ).")
    ap.add_argument("--db", default=ScanConfig().db_path)
    args = ap.parse_args()

    rows = Storage(args.db).all_closed_bets()

    evs: list[float] = []
    clvs: list[float] = []
    immobiles = 0            # ligne juste STRICTEMENT inchangée
    deplacements: list[float] = []   # |fair_clôture / fair_détection − 1| en %
    delais: list[float] = []
    par_marche: dict[str, list[tuple[float, float]]] = defaultdict(list)

    for r in rows:
        if args.sport and (r["sport"] or "") != args.sport:
            continue
        if args.since and (r["detected_at"] or "")[:10] < args.since:
            continue
        f, c = r["fair_odd"], r["closing_fair_odd"]
        ev = r["ev_pct"]
        if not f or not c or f <= 0 or c <= 0 or ev is None:
            continue
        clv = clv_pct(r["odd_taken"], c) * 100.0
        evs.append(float(ev))
        clvs.append(clv)
        par_marche[(r["market"] or "?")].append((float(ev), clv))
        # Égalité stricte : une ligne juste rigoureusement identique à deux
        # heures d'intervalle n'est pas un marché calme, c'est la même cote
        # relue. À 1e-9 près pour absorber l'aller-retour en flottant.
        if abs(c - f) < 1e-9:
            immobiles += 1
        deplacements.append(abs(c / f - 1.0) * 100.0)
        d = _heures(r["detected_at"], r["closed_at"])
        if d is not None:
            delais.append(d)

    n = len(evs)
    if n < 10:
        print("Trop peu de paris clôturés pour conclure.")
        return

    print(f"\n{n} paris clôturés" + (f" ({args.sport})" if args.sport else ""))

    # 1. La ligne juste bouge-t-elle ?
    print("\n1. Déplacement de la ligne juste, détection → clôture")
    print(f"   inchangée au bit près : {immobiles:6d}  ({100.0*immobiles/n:5.1f} %)")
    print("     (une part est structurelle : quand la détection porte sur la\n"
          "      dernière cote avant le coup d'envoi, la clôture est la même.)")
    if deplacements:
        d = sorted(deplacements)
        print(f"   déplacement médian   : {median(d):6.2f} %")
        print(f"   déplacement moyen    : {mean(d):6.2f} %")
        print(f"   9e décile            : {d[int(0.9*len(d))]:6.2f} %")
    if delais:
        print(f"   délai médian         : {median(sorted(delais)):6.1f} h")

    # 2. Le R² : la CLV est-elle une simple fonction de l'EV ?
    mx, my = mean(evs), mean(clvs)
    sxx = sum((x - mx) ** 2 for x in evs)
    syy = sum((y - my) ** 2 for y in clvs)
    if sxx <= 0 or syy <= 0:
        print("\nEV ou CLV constante — R² non défini.")
        return
    sxy = sum((x - mx) * (y - my) for x, y in zip(evs, clvs))
    a = sxy / sxx
    r2 = (sxy ** 2) / (sxx * syy)

    print("\n2. La CLV est-elle autre chose que l'EV ?")
    print(f"   pente CLV/EV : {a:+.3f}")
    print(f"   R²           : {r2:.3f}")
    print(f"   CLV moyenne  : {my:+.2f} %   EV moyenne : {mx:+.2f} %")

    print("\n3. Par marché")
    print(f"   {'marché':<12s} {'n':>6s} {'pente':>7s} {'R²':>7s} {'CLV moy':>9s}")
    for m, lot in sorted(par_marche.items(), key=lambda kv: -len(kv[1])):
        if len(lot) < 25:
            continue
        xs = [x for x, _ in lot]
        ys = [y for _, y in lot]
        mmx, mmy = mean(xs), mean(ys)
        s1 = sum((x - mmx) ** 2 for x in xs)
        s2 = sum((y - mmy) ** 2 for y in ys)
        if s1 <= 0 or s2 <= 0:
            continue
        s12 = sum((x - mmx) * (y - mmy) for x, y in zip(xs, ys))
        print(f"   {m:<12.12s} {len(lot):6d} {s12/s1:+7.2f} {(s12**2)/(s1*s2):7.3f} {mmy:+8.2f} %")

    # 4. La lecture.
    print("\n" + "=" * 62)
    part = 100.0 * immobiles / n
    # 20 % : au-delà, l'explication structurelle ne suffit plus. Une détection
    # sur la dernière cote avant le coup d'envoi est un cas de bord, pas le cas
    # d'un pari sur cinq — le délai médian affiché plus haut permet de trancher.
    if part > 20.0:
        print(f"⚠️ {part:.0f} % des clôtures ont une ligne juste RIGOUREUSEMENT identique\n"
              "   à celle de la détection — trop pour la seule explication structurelle.\n"
              "   Une cote juste identique au bit près après plusieurs heures n'est pas\n"
              "   un marché calme : c'est la même cote relue. Panne de capture probable,\n"
              "   à élucider avant toute lecture des chiffres de CLV. Recouper avec le\n"
              "   délai médian ci-dessus : s'il est court, l'immobilité peut être réelle.")
    if r2 >= 0.90:
        print(f"⚠️ R² = {r2:.3f} : la CLV est une fonction quasi déterministe de l'EV.\n"
              "   Elle ne CONFIRME donc pas l'edge, elle le réénonce — et sa moyenne,\n"
              "   si flatteuse soit-elle, ne dit rien que l'EV ne disait déjà. Le\n"
              "   §20.4 (CLV +10 % contre ROI −0,34 %) s'explique alors sans mystère :\n"
              "   la CLV n'a jamais été une validation indépendante.")
    elif r2 >= 0.50:
        print(f"R² = {r2:.3f} : la CLV suit largement l'EV, mais garde une part propre.\n"
              "   Une partie de ce qu'elle mesure vient bien du marché.")
    else:
        print(f"R² = {r2:.3f} : la CLV s'écarte franchement de l'EV — elle mesure\n"
              "   bien le déplacement du marché, pas l'estimation de départ. C'est le\n"
              "   cas SAIN, celui où la CLV mérite son statut de KPI.")
    print("\nRappel : ce R² ne dit rien de la JUSTESSE de la référence. Une CLV\n"
          "indépendante peut rester fausse si le devig l'est (§20.4 lecture 3).")


if __name__ == "__main__":
    main()
