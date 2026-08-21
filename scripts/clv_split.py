"""CLV découpé selon les axes qu'on veut, et séparé jouable / muet.

Pourquoi cet outil existe
-------------------------
Quatre books sur huit sont en sourdine dans `/book` (§21.8), soit 45 % des
détections. `clv-report` mesure la population entière : ses moyennes décrivent
donc un flux que personne ne reçoit. Un book muet à CLV faible tire les
chiffres vers le bas et fait croire à une dégradation pendant que le flux
réellement alerté s'améliore — et rien dans la sortie ne le signale.

`clv-report` n'accepte par ailleurs ni `--sport` ni `--market`. Répondre à
« la CLV de StarCasino est-elle mauvaise partout ou seulement au tennis ? »
supposait donc d'exporter un CSV et de l'ouvrir à la main.

    .venv/bin/python -m scripts.clv_split --by book,sport
    .venv/bin/python -m scripts.clv_split --by sport,market --min 30
    .venv/bin/python -m scripts.clv_split --by book --since 2026-08-10

La CLV vient de `clv.clv_pct` et les lignes de `Storage.all_closed_bets()` :
mêmes chiffres que `clv-report`, au découpage près (§17.7 — une sonde qui
recalcule autre chose que la production ment).

⚠️ Le taux de CLV positives est plus robuste que la moyenne : la CLV a des
queues épaisses, et une poignée de gros gagnants déplace la moyenne sans rien
dire de la régularité. La colonne σ dit à partir de quand l'écart est lisible.
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from statistics import mean, pstdev

from src.clv import clv_pct
from src.config import ScanConfig
from src.storage import Storage

_AXES = ("book", "sport", "market", "league", "played", "alerte", "cote")

# ⚠️ Les MÊMES bornes que `pnl_detections`, et ce n'est pas un détail de
# présentation. Le §21.17 a trouvé que la CLV et le P&L se contredisent sur les
# grosses cotes — CLV plate, P&L à −20 % sur 4,0-6,0. Les comparer suppose des
# tranches identiques ; deux découpages différents rendraient la contradiction
# illisible, et c'est précisément ce qu'on cherche à mesurer.
#
# Le §9 et le §17.3 concluaient « la cote n'est pas un critère » — mais sur la
# CLV seule. Cet axe sert à refaire cette lecture EN FACE du P&L.
_BANDES_COTE = ((1.0, 1.8), (1.8, 2.3), (2.3, 3.0),
                (3.0, 4.0), (4.0, 6.0), (6.0, float("inf")))


def _bande_cote(odd: float | None) -> str:
    """La tranche de cote d'un pari, dans les bornes de `pnl_detections`."""
    if not odd or odd <= 0:
        return "?"
    for bas, haut in _BANDES_COTE:
        if bas <= odd < haut:
            return f"{bas:.1f}-{haut:.1f}" if haut != float("inf") else "> 6.0"
    return "?"


def _muted(db_path: str) -> set[str]:
    """Les books coupés dans /book. Lus à la même table que l'alerter."""
    try:
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE IF NOT EXISTS book_alerts_off "
                    "(book TEXT PRIMARY KEY, disabled_at TEXT NOT NULL)")
        rows = con.execute("SELECT book FROM book_alerts_off").fetchall()
        con.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--by", default="book",
                    help=f"Axes séparés par des virgules, parmi {', '.join(_AXES)}. "
                         "« alerte » sépare les books muets des books actifs, "
                         "« cote » reprend les tranches de `pnl_detections` "
                         "pour que les deux tables se comparent (§21.17).")
    ap.add_argument("--min", type=int, default=20,
                    help="Effectif minimum pour afficher une ligne (défaut 20).")
    ap.add_argument("--since", default="", help="Date de détection minimale (AAAA-MM-JJ).")
    ap.add_argument("--until", default="", help="Date de détection maximale, incluse.")
    ap.add_argument("--db", default=ScanConfig().db_path)
    args = ap.parse_args()

    axes = [a.strip() for a in args.by.split(",") if a.strip()]
    inconnus = [a for a in axes if a not in _AXES]
    if inconnus:
        raise SystemExit(f"Axe inconnu : {inconnus}. Choix : {', '.join(_AXES)}")

    muets = _muted(args.db)
    rows = Storage(args.db).all_closed_bets()

    groupes: dict[tuple, list[float]] = defaultdict(list)
    for r in rows:
        d = (r["detected_at"] or "")[:10]
        if args.since and d < args.since:
            continue
        if args.until and d > args.until:
            continue
        fair = r["closing_fair_odd"]
        if not fair or fair <= 0:
            continue
        cle = []
        for a in axes:
            if a == "alerte":
                cle.append("muet" if r["book"] in muets else "actif")
            elif a == "played":
                cle.append("joué" if r["played"] else "détecté")
            elif a == "cote":
                cle.append(_bande_cote(r["odd_taken"]))
            else:
                cle.append(r[a] or "?")
        groupes[tuple(cle)].append(clv_pct(r["odd_taken"], fair) * 100.0)

    if not groupes:
        print("Aucun pari clôturé sur cette fenêtre.")
        return

    entete = "  ".join(f"{a:<16s}" for a in axes)
    print(f"\n{entete}  {'n':>6s} {'CLV moy':>9s} {'σ moy':>7s} {'médiane':>8s} {'positives':>10s}")
    print("-" * (18 * len(axes) + 45))

    petits = 0
    # Une table de cotes se lit dans l'ordre des cotes, pas des effectifs.
    ordre_cote = [f"{b:.1f}-{h:.1f}" if h != float("inf") else "> 6.0"
                  for b, h in _BANDES_COTE] + ["?"]
    if axes == ["cote"]:
        tri = lambda kv: ordre_cote.index(kv[0][0]) if kv[0][0] in ordre_cote else 99
    else:
        tri = lambda kv: -len(kv[1])
    for cle, vals in sorted(groupes.items(), key=tri):
        if len(vals) < args.min:
            petits += len(vals)
            continue
        v = sorted(vals)
        moy = mean(v)
        # σ de la MOYENNE, pas de l'échantillon : c'est elle qui dit si l'écart
        # entre deux lignes est lisible ou du bruit.
        sigma = pstdev(v) / (len(v) ** 0.5) if len(v) > 1 else float("nan")
        med = v[len(v) // 2]
        pos = 100.0 * sum(1 for x in v if x > 0) / len(v)
        libelle = "  ".join(f"{c:<16.16s}" for c in cle)
        print(f"{libelle}  {len(v):6d} {moy:+8.2f} % {sigma:6.2f} {med:+7.2f} % {pos:9.1f} %")

    if petits:
        print(f"\n({petits} paris dans des groupes sous {args.min}, masqués — "
              "une CLV moyenne sur quelques dizaines de paris ne distingue rien.)")
    if "alerte" not in axes and muets:
        print(f"\n⚠️ {len(muets)} books sont muets dans /book ({', '.join(sorted(muets))}). "
              "Leurs détections sont comptées ici alors qu'elles n'atteignent aucun canal.\n"
              "   Ajoute « alerte » aux axes pour les séparer.")


if __name__ == "__main__":
    main()
