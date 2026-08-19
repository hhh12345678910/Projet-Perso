"""Où s'arrête un marché qui ne produit rien.

« Zéro détection » a quatre causes possibles, indiscernables dans les logs —
et c'est le mode de défaillance dominant du projet (§11) :

  1. Pinnacle ne price pas le marché             → pas de référence
  2. Il le price d'un seul côté                  → pas de devig
  3. Aucun book n'est en face à la MÊME ligne    → rien à comparer
  4. Tout est là, mais l'écart ne dépasse jamais le seuil d'EV

Les trois premières sont des pannes ; la quatrième est un marché efficace,
c'est-à-dire une bonne nouvelle correctement mesurée. Les confondre fait
chercher un bug là où il n'y en a pas, ou pire, croire un marché sain quand la
collecte est morte — ce qu'ont fait les totaux de tennis pendant des mois.

Cet outil rejoue la chaîne réelle sur les cotes DÉJÀ EN BASE : mêmes
`build_fair_lines`, `_devig_group` et `find_value_bets` que le daemon. Une
sonde qui recoderait le calcul mesurerait autre chose que la production (§17.7).

    python -m scripts.market_supply --sport tennis --market totals --hours 2
    python -m scripts.market_supply --sport tennis --market totals --min-line 10
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from src.config import ScanConfig
from src.main import build_fair_lines, find_value_bets
from src.models import Book, MarketType, OddQuote, Outcome


def _rows(db_path: str, sport: str, market: str, since: str, min_line: float | None):
    """La dernière cote connue de chaque (événement, book, marché, ligne, issue).

    ⚠️ L'écriture des cotes est PARCIMONIEUSE (§18.5) : une cote inchangée n'est
    pas réécrite. Prendre « les lignes de la dernière minute » raterait donc
    tout ce qui est stable, c'est-à-dire l'essentiel. On prend le dernier relevé
    de chaque clé sur toute la fenêtre.
    """
    db = sqlite3.connect(db_path)
    sql = """
      SELECT q.event_key, q.book, q.market, q.outcome_label, q.line,
             q.decimal_odd, MAX(q.fetched_at), q.source_event_id
      FROM quotes q JOIN events e ON e.event_key = q.event_key
      WHERE e.sport = ? AND q.market = ? AND q.fetched_at > ?
    """
    params: list = [sport, market, since]
    if min_line is not None:
        sql += " AND q.line >= ?"
        params.append(min_line)
    sql += " GROUP BY q.event_key, q.book, q.market, q.line, q.outcome_label"
    try:
        return db.execute(sql, params).fetchall()
    finally:
        db.close()


def _quotes(rows) -> list[OddQuote]:
    out: list[OddQuote] = []
    for ek, book, market, label, line, odd, fetched, src in rows:
        try:
            b, m = Book(book), MarketType(market)
        except ValueError:
            continue                      # book retiré du modèle : on l'ignore
        out.append(OddQuote(
            event_key=ek, book=b, market=m,
            outcome=Outcome(label=label, line=line),
            decimal_odd=odd,
            fetched_at=datetime.fromisoformat(fetched),
            source_event_id=src or "",
        ))
    return out


def _percentiles(values: list[float]) -> str:
    if not values:
        return "—"
    v = sorted(values)
    def p(x): return v[min(len(v) - 1, int(x * len(v)))]
    return (f"min {v[0]:+.1f}  p10 {p(.10):+.1f}  médiane {p(.50):+.1f}  "
            f"p90 {p(.90):+.1f}  max {v[-1]:+.1f}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sport", default="tennis")
    ap.add_argument("--market", default="totals")
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--min-line", type=float, default=None,
                    help="Ne garder que les lignes >= X. Au tennis, 10 sépare "
                         "les totaux de JEUX (15-25) de ceux de SETS (2,5).")
    ap.add_argument("--db", default="data/valuebet.db")
    args = ap.parse_args()

    cfg = ScanConfig(sport=args.sport, db_path=args.db)
    since = (datetime.now(timezone.utc) - timedelta(hours=args.hours)).isoformat()
    quotes = _quotes(_rows(args.db, args.sport, args.market, since, args.min_line))
    if not quotes:
        print(f"Aucune cote {args.sport}/{args.market} sur {args.hours} h. "
              f"Étape 1 : la collecte elle-même ne produit rien.")
        return

    pin = [q for q in quotes if q.book == Book.PINNACLE]
    soft = [q for q in quotes if q.book != Book.PINNACLE]
    fair = build_fair_lines(pin, cfg.devig_method)

    pin_groups = defaultdict(set)
    for q in pin:
        pin_groups[(q.event_key, q.market, q.outcome.line)].add(q.outcome.label)
    deux_cotes = {k for k, v in pin_groups.items() if len(v) >= 2}

    print(f"\n=== {args.sport} / {args.market} sur {args.hours} h "
          f"{'(lignes >= ' + str(args.min_line) + ')' if args.min_line else ''} ===\n")
    print(f"1. couples (événement, ligne) cotés par Pinnacle : {len(pin_groups)}")
    print(f"2.   … dont des DEUX côtés (devigables)          : {len(deux_cotes)}")
    print(f"3.   … dont une ligne juste effectivement calculée: {len(fair)}"
          f"   {'⚠️ écart = références écartées pour marge aberrante' if len(fair) < len(deux_cotes) else ''}")

    # 4. combien de cotes de books tombent sur une ligne juste, et pourquoi les
    #    autres n'y tombent pas — c'est CE compteur qui manquait.
    soft_labels: dict[tuple, set[str]] = defaultdict(set)
    for q in soft:
        soft_labels[(q.event_key, q.market, q.outcome.line, q.book)].add(q.outcome.label)

    sans_juste = incomplet = comparables = 0
    for q in soft:
        fl = fair.get((q.event_key, q.market, q.outcome.line))
        if fl is None:
            sans_juste += 1
        elif len(soft_labels[(q.event_key, q.market, q.outcome.line, q.book)]) != len(fl.outcomes):
            incomplet += 1
        else:
            comparables += 1
    print(f"\n4. cotes de books : {len(soft)}")
    print(f"     sans ligne juste en face (autre ligne)   : {sans_juste}")
    print(f"     book incomplet sur cette ligne           : {incomplet}")
    print(f"     COMPARABLES                              : {comparables}")

    if not comparables:
        print("\n→ La chaîne s'arrête avant l'EV. Ce n'est pas un marché efficace, "
              "c'est une absence d'appariement.")
        return

    # 5. la distribution d'EV entière, seuil retiré : c'est elle qui distingue
    #    « pas d'edge » de « edge coupé par le seuil ».
    ouvert = ScanConfig(sport=args.sport, db_path=args.db,
                        min_ev_pct=-1000.0, max_ev_pct=1e9,
                        devig_method=cfg.devig_method)
    ouvert.scan_live_value_bets = True     # on veut la distribution, pas l'alerte
    toutes = find_value_bets(quotes, fair, ouvert)
    evs = [b.ev_pct for b in toutes]
    print(f"\n5. EV calculées : {len(evs)}")
    print(f"     {_percentiles(evs)}")
    for seuil in (0.0, cfg.min_ev_pct, 5.0, 10.0):
        n = sum(1 for e in evs if e >= seuil)
        print(f"     au-dessus de {seuil:+5.1f} % : {n:5d}  ({100 * n / len(evs):5.1f} %)")

    print(f"\n   ⚠️ seuil de production = {cfg.min_ev_pct:+.1f} %, "
          f"et les paris déjà commencés sont exclus "
          f"(scan_live_value_bets={ScanConfig().scan_live_value_bets}).")

    top = sorted(toutes, key=lambda b: b.ev_pct, reverse=True)[:10]
    if top:
        print("\n6. les dix meilleures :")
        for b in top:
            print(f"     {b.ev_pct:+6.1f} %  {b.book.value:16s} "
                  f"{b.outcome.label:5s} {b.outcome.line}  @ {b.odd_taken:.2f} "
                  f"(juste {b.fair_odd:.2f})  {b.event_key[:46]}")


if __name__ == "__main__":
    main()
