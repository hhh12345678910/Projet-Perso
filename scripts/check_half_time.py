"""Les mi-temps : que produisent-elles, et leur CLV vaut-elle quelque chose ?

Pourquoi cet outil existe
-------------------------
Les marchés de mi-temps ont été ouverts le 20/08 (§21.14) en mode MUET : ils
sont collectés, devigués, stockés et suivis en CLV, mais aucun canal Telegram
ne les reçoit. Sans sonde dédiée, ce flux tournerait sans que personne ne sache
ce qu'il vaut — et la panne silencieuse est le mode de défaillance dominant du
projet (§11).

Elle répond à trois questions, dans l'ordre où elles se posent :

1. **Le flux existe-t-il ?** Pinnacle publie-t-il, les softs suivent-ils.
2. **Les échelles sont-elles saines ?** Une ligne de mi-temps se situe vers
   0,5–2,5 buts, celle d'un match plein vers 1,5–4,5. Si les deux se
   ressemblent, c'est que quelque chose les mélange — la panne du §21.3.
3. **La CLV suit-elle ?** C'est la seule qui décide si on allume les alertes.

    .venv/bin/python -m scripts.check_half_time

⚠️ Tant que l'effectif clôturé est faible, la CLV affichée ne distingue rien.
Le §21.12 a montré qu'un constat tiré de 437 paris pouvait s'inverser à 774.
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from statistics import mean, pstdev

from src.clv import clv_pct
from src.config import ScanConfig
from src.storage import Storage

_MI_TEMPS = ("h2h_h1", "totals_h1")
_PLEIN = {"h2h_h1": "h2h", "totals_h1": "totals"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=ScanConfig().db_path)
    ap.add_argument("--heures", type=float, default=24.0)
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    print(f"=== A. Cotes collectées, {args.heures:.0f} h ===")
    rows = con.execute(
        "SELECT market, book, COUNT(*) n, COUNT(DISTINCT event_key) ev,"
        "       MIN(line) lo, MAX(line) hi, COUNT(DISTINCT line) lignes "
        "FROM quotes WHERE market IN (?,?) "
        "  AND fetched_at > datetime('now', ?) "
        "GROUP BY market, book ORDER BY n DESC",
        (*_MI_TEMPS, f"-{args.heures} hours")).fetchall()
    if not rows:
        print("  AUCUNE cote de mi-temps.")
        print("  → Le correctif n'est pas en service, ou Pinnacle ne publie rien.")
        print("    Vérifier : git pull, puis redémarrage du daemon.")
    else:
        for r in rows:
            print(f"  {r['market']:<10s} {r['book']:<18s} {r['n']:6d} cotes  "
                  f"{r['ev']:4d} events  {r['lignes']:3d} lignes  "
                  f"[{r['lo']} .. {r['hi']}]")

    print(f"\n=== B. Échelles — mi-temps contre match plein ===")
    for mt in _MI_TEMPS:
        for marche in (mt, _PLEIN[mt]):
            r = con.execute(
                "SELECT COUNT(*) n, MIN(line) lo, MAX(line) hi, AVG(line) moy "
                "FROM quotes WHERE market=? AND book='pinnacle' "
                "  AND line IS NOT NULL AND fetched_at > datetime('now', ?)",
                (marche, f"-{args.heures} hours")).fetchone()
            if r and r["n"]:
                print(f"  {marche:<10s} n={r['n']:6d}  [{r['lo']} .. {r['hi']}]  "
                      f"moyenne {r['moy']:.2f}")
        # Le test qui compte : une mi-temps DOIT coter plus bas qu'un match plein.
        a = con.execute("SELECT AVG(line) m FROM quotes WHERE market=? AND book='pinnacle'"
                        " AND line IS NOT NULL AND fetched_at > datetime('now', ?)",
                        (mt, f"-{args.heures} hours")).fetchone()["m"]
        b = con.execute("SELECT AVG(line) m FROM quotes WHERE market=? AND book='pinnacle'"
                        " AND line IS NOT NULL AND fetched_at > datetime('now', ?)",
                        (_PLEIN[mt], f"-{args.heures} hours")).fetchone()["m"]
        if a is not None and b is not None and mt == "totals_h1":
            if a >= b:
                print(f"  ⚠️ ANOMALIE : la ligne moyenne de {mt} ({a:.2f}) n'est PAS "
                      f"sous celle de {_PLEIN[mt]} ({b:.2f}).")
                print("     Moins de buts se marquent en 45 min qu'en 90. Une mi-temps "
                      "qui\n     cote comme un match plein signale un mélange des deux "
                      "échelles (§21.3).")
            else:
                print(f"  OK : {mt} ({a:.2f}) cote bien sous {_PLEIN[mt]} ({b:.2f}).")

    print(f"\n=== C. Détections, {args.heures:.0f} h ===")
    rows = con.execute(
        "SELECT market, book, COUNT(*) n, AVG(ev_pct) ev FROM value_bets "
        "WHERE market IN (?,?) AND detected_at > datetime('now', ?) "
        "GROUP BY market, book ORDER BY n DESC",
        (*_MI_TEMPS, f"-{args.heures} hours")).fetchall()
    if not rows:
        print("  Aucune détection. Normal tant qu'aucun soft ne price la mi-temps\n"
              "  en face de Pinnacle : seul Betano (OUH1) est mappé à ce jour.")
    else:
        for r in rows:
            print(f"  {r['market']:<10s} {r['book']:<18s} {r['n']:4d}   EV moy {r['ev']:.2f} %")
    con.close()

    print("\n=== D. CLV — la seule question qui décide ===")
    par: dict[str, list[float]] = defaultdict(list)
    for r in Storage(args.db).all_closed_bets():
        if (r["market"] or "") in _MI_TEMPS:
            par[r["market"]].append(clv_pct(r["odd_taken"], r["closing_fair_odd"]) * 100.0)
    if not par:
        print("  Aucun pari de mi-temps clôturé. Rien à conclure — revenir\n"
              "  quand des matchs auront clôturé.")
    else:
        print(f"  {'marché':<12s} {'n':>5s} {'CLV moy':>9s} {'σ moy':>7s} {'positives':>10s}")
        for m, v in sorted(par.items()):
            sig = pstdev(v) / (len(v) ** 0.5) if len(v) > 1 else float("nan")
            pos = 100.0 * sum(1 for x in v if x > 0) / len(v)
            marque = "" if len(v) >= 25 else "   (sous 25, indicatif)"
            print(f"  {m:<12s} {len(v):5d} {mean(v):+8.2f} % {sig:6.2f} {pos:8.1f} %{marque}")
        print("\n  Comparer à la CLV du match plein :"
              "\n    .venv/bin/python -m scripts.clv_split --by market --min 20")
    print("\n⚠️ Ces marchés sont MUETS par construction (§21.14) : aucune alerte\n"
          "n'en sort, quels que soient ces chiffres. Le silence se lève dans\n"
          "alerter.py, et seulement quand la CLV aura tranché.")


if __name__ == "__main__":
    main()
