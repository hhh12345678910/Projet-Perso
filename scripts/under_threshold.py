"""Quel seuil d'EV appliquer aux `under` — mesuré, pas choisi.

Pourquoi cet outil existe
-------------------------
Le §20.8 établit que les `under` rendent 2,7 points de CLV de moins que les
`over` (+3,80 % contre +6,50 %, z = 2,7, p ≈ 0,007), et que l'écart se
concentre sur les lignes hautes. Le §21.9 pt 4 en tire « relever le seuil d'EV
sur les under » — sans dire à combien, parce que personne ne l'a mesuré.

Relever un seuil ne se justifie QUE si la CLV monte avec l'EV à l'intérieur des
`under`. Si elle est plate, un seuil plus haut coupe du volume sans rien
améliorer : il faudrait alors exclure les lignes hautes, pas filtrer sur l'EV.
Le tableau « CLV par tranche d'EV » ci-dessous tranche cette question, et c'est
lui qu'il faut lire en premier — un balayage de seuils sur une pente plate
produit des chiffres d'apparence convaincante qui ne décrivent que du bruit.

    .venv/bin/python -m scripts.under_threshold
    .venv/bin/python -m scripts.under_threshold --sport soccer --min 30

CLV et lignes viennent de `clv.clv_pct` et `Storage.all_closed_bets()` : mêmes
chiffres que `clv-report` et `clv_split`, au découpage près (§17.7 — une sonde
qui recalcule autre chose que la production ment).
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from statistics import mean, pstdev

from src.clv import clv_pct
from src.config import ScanConfig
from src.storage import Storage

# Seuils candidats. 2.0 = `min_ev_pct` actuel, donc la première ligne du
# balayage décrit le flux tel qu'il est aujourd'hui.
_SEUILS = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)


def _stats(vals: list[float]) -> tuple[int, float, float, float]:
    """n, CLV moyenne, σ de la moyenne, taux de positives."""
    if not vals:
        return 0, float("nan"), float("nan"), float("nan")
    sigma = pstdev(vals) / (len(vals) ** 0.5) if len(vals) > 1 else float("nan")
    pos = 100.0 * sum(1 for x in vals if x > 0) / len(vals)
    return len(vals), mean(vals), sigma, pos


def _ligne(libelle: str, vals: list[float], largeur: int = 22) -> str:
    n, moy, sigma, pos = _stats(vals)
    if not n:
        return f"{libelle:<{largeur}.{largeur}s} {0:>6d}" + " " * 28 + "—"
    return (f"{libelle:<{largeur}.{largeur}s} {n:6d} {moy:+8.2f} % "
            f"{sigma:6.2f} {pos:8.1f} %")


def _pente(unders: list[tuple[float, float, float]]) -> tuple[float, float | None]:
    """Régression de la CLV sur l'EV, avec l'erreur standard de la pente.

    Moindres carrés ordinaires sur les paris individuels. Retourne
    (pente, σ de la pente) en points de CLV par point d'EV, ou (0, None) quand
    l'échantillon ne permet aucune estimation.
    """
    n = len(unders)
    if n < 10:
        return 0.0, None
    xs = [ev for ev, _, _ in unders]
    ys = [clv for _, _, clv in unders]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 0:
        return 0.0, None
    a = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    b = my - a * mx
    # variance résiduelle, n-2 degrés de liberté
    s2 = sum((y - (a * x + b)) ** 2 for x, y in zip(xs, ys)) / (n - 2)
    return a, (s2 / sxx) ** 0.5


def _entete(titre: str, largeur: int = 22) -> None:
    print(f"\n{titre}")
    print(f"{'':<{largeur}s} {'n':>6s} {'CLV moy':>9s} {'σ moy':>7s} {'positives':>10s}")
    print("-" * (largeur + 36))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sport", default="", help="Restreindre à un sport (défaut : tous).")
    ap.add_argument("--min", type=int, default=25,
                    help="Effectif minimum pour qu'une ligne soit lisible (défaut 25).")
    ap.add_argument("--since", default="", help="Date de détection minimale (AAAA-MM-JJ).")
    ap.add_argument("--db", default=ScanConfig().db_path)
    args = ap.parse_args()

    rows = Storage(args.db).all_closed_bets()

    reference: dict[str, list[float]] = defaultdict(list)   # h2h / over / under
    unders: list[tuple[float, float, float]] = []           # (ev, ligne, clv)

    for r in rows:
        d = (r["detected_at"] or "")[:10]
        if args.since and d < args.since:
            continue
        if args.sport and (r["sport"] or "") != args.sport:
            continue
        fair = r["closing_fair_odd"]
        if not fair or fair <= 0:
            continue
        clv = clv_pct(r["odd_taken"], fair) * 100.0
        marche = (r["market"] or "").lower()
        cote = (r["outcome_label"] or "").lower()

        if marche == "h2h":
            reference["h2h"].append(clv)
        elif marche == "totals" and cote in ("over", "under"):
            reference[cote.upper()].append(clv)
            if cote == "under":
                ev, ligne = r["ev_pct"], r["line"]
                if ev is not None and ligne is not None:
                    unders.append((float(ev), float(ligne), clv))

    if not unders:
        print("Aucun `under` clôturé sur cette fenêtre — rien à mesurer.")
        return

    # 1. Le constat du §20.8, recalculé sur les données d'aujourd'hui. S'il ne
    #    se reproduit plus, tout le reste de cette sonde est sans objet.
    _entete("1. Le constat du §20.8, sur les données actuelles")
    for cle in ("h2h", "OVER", "UNDER"):
        print(_ligne(cle, reference[cle]))

    # 2. Où l'écart se loge. Le §20.8 l'attribue aux lignes hautes ; si c'est
    #    vrai, le remède est une règle de ligne, pas une règle d'EV.
    _entete("2. Les `under` par ligne")
    bandes: dict[str, list[float]] = defaultdict(list)
    for ev, ligne, clv in unders:
        if ligne <= 2.5:
            bandes["≤ 2.5"].append(clv)
        elif ligne <= 3.5:
            bandes["3.5"].append(clv)
        elif ligne <= 4.5:
            bandes["4.5"].append(clv)
        else:
            bandes["≥ 5.5"].append(clv)
    for cle in ("≤ 2.5", "3.5", "4.5", "≥ 5.5"):
        marque = "" if len(bandes[cle]) >= args.min else f"   (sous {args.min}, indicatif)"
        print(_ligne(f"under {cle}", bandes[cle]) + marque)

    # 3. LA question. Un seuil d'EV ne trie que si la CLV monte avec l'EV.
    _entete("3. Les `under` par tranche d'EV  ← lire ceci en premier")
    tranches = ((2.0, 3.0), (3.0, 4.0), (4.0, 6.0), (6.0, 8.0), (8.0, 12.0), (12.0, 1e9))
    pente: list[tuple[float, float, int]] = []
    for bas, haut in tranches:
        vals = [c for ev, _, c in unders if bas <= ev < haut]
        libelle = f"EV {bas:.0f}–{haut:.0f} %" if haut < 1e9 else f"EV ≥ {bas:.0f} %"
        marque = "" if len(vals) >= args.min else f"   (sous {args.min}, indicatif)"
        print(_ligne(libelle, vals) + marque)
        if vals:
            pente.append((mean([ev for ev, _, c in unders if bas <= ev < haut]),
                          mean(vals), len(vals)))

    # 4. Le balayage : ce que chaque seuil garde et ce qu'il jette.
    print("\n4. Si on relevait le seuil des `under` à…")
    print(f"{'seuil':<10s} {'gardé':>6s} {'CLV gardé':>11s} "
          f"{'jeté':>6s} {'CLV jeté':>10s}")
    print("-" * 48)
    for t in _SEUILS:
        gardes = [c for ev, _, c in unders if ev >= t]
        jetes = [c for ev, _, c in unders if ev < t]
        ng, mg, _, _ = _stats(gardes)
        nj, mj, _, _ = _stats(jetes)
        actuel = "  ← seuil actuel" if abs(t - ScanConfig().min_ev_pct) < 1e-9 else ""
        g = f"{mg:+8.2f} %" if ng else "       —"
        j = f"{mj:+8.2f} %" if nj else "       —"
        print(f"{t:>6.1f} %   {ng:6d} {g:>11s} {nj:6d} {j:>10s}{actuel}")

    # 5. La lecture, énoncée plutôt que laissée au lecteur.
    print("\n" + "=" * 60)
    cible = _stats(reference["OVER"])[1] if reference["OVER"] else float("nan")
    print(f"Cible : la CLV des `over`, {cible:+.2f} %. Un seuil ne « marche » que\n"
          f"s'il amène les `under` gardés à ce niveau SANS vider la population.")
    # La pente se mesure sur les paris INDIVIDUELS, pas sur les moyennes de
    # tranches : six moyennes donnent six points d'apparence bien alignés dont
    # l'incertitude a disparu. Et elle se lit avec son erreur standard — sur une
    # population bruitée, une pente de +0,12 sort spontanément de données où EV
    # et CLV sont indépendantes. Sans barre d'erreur, cette sonde conseillerait
    # de relever le seuil sur du bruit : c'est la panne qu'elle existe pour
    # empêcher.
    a, err = _pente(unders)
    if err is None:
        print("\nPente : trop peu de paris, ou toutes les EV identiques — non mesurable.")
    else:
        print(f"\nPente mesurée : {a:+.2f} ± {err:.2f} point de CLV par point d'EV")
        print(f"                (±1 σ ; significative si |pente| > 2 σ, soit {2*err:.2f}).")
        if abs(a) <= 2 * err:
            print("→ INDISCERNABLE DE ZÉRO. Relever le seuil d'EV ne trierait rien :\n"
                  "  il couperait du volume au hasard. Si le tableau 2 montre les\n"
                  "  lignes hautes en cause, c'est une règle de LIGNE qu'il faut,\n"
                  "  pas une règle d'EV.")
        elif a < 0:
            print("→ NÉGATIVE, et significativement. Les `under` à forte EV rendent\n"
                  "  MOINS que les autres — relever le seuil aggraverait les choses.\n"
                  "  Résultat surprenant : à recouper avant d'agir.")
        else:
            print("→ La CLV monte avec l'EV, et l'écart dépasse le bruit. Un seuil trie\n"
                  "  donc quelque chose ; le tableau 4 dit lequel, au prix de quel volume.")
    print(f"\n⚠️ Une ligne sous {args.min} paris ne distingue rien. Et la σ affichée est\n"
          "celle de la MOYENNE : deux lignes se distinguent si leur écart dépasse\n"
          "deux ou trois σ, pas moins.")


if __name__ == "__main__":
    main()
