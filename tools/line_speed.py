"""Vitesse de dérive des cotes — combien coûte une référence vieille de N secondes ?

    .venv/bin/python tools/line_speed.py --hours 6 --sport soccer

Pourquoi cet outil
------------------
Espacer les appels à Pinnacle soulage son quota mais périme la ligne de
référence. Le coût ne se devine pas : il se mesure. Ce script lit la table
`quotes` — déjà peuplée à chaque cycle — et répond à deux questions
distinctes qu'il ne faut pas confondre :

1. **Pinnacle** : de combien la ligne bouge-t-elle en 30/60/90/120 s ? C'est
   exactement l'erreur d'EV qu'introduit un espacement de cette durée. Les
   books soft, eux, restent interrogés à chaque cycle : l'espacement ne
   retarde aucune détection.

2. **Books belges** : à quelle vitesse bougent-ils, par book et par marché ?
   Cela ne concerne pas l'espacement mais la fenêtre pendant laquelle une
   occasion reste jouable — autre sujet, autre décision.

Les variations sont exprimées en pourcentage de la cote, ce qui les rend
directement comparables à une EV : une dérive médiane de 0,3 % sur 60 s
signifie qu'une référence vieille d'une minute fausse l'EV de 0,3 point.
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# Écarts de temps étudiés, en secondes, avec la tolérance autour de chaque
# cible. Le cycle n'est pas parfaitement régulier, donc une paire de captures
# ne tombe jamais exactement sur 60 s.
LAGS = [(30, 12), (60, 15), (90, 18), (120, 25), (300, 60)]


def _parse(ts: str) -> float | None:
    try:
        d = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.timestamp()


def series(conn: sqlite3.Connection, book: str, market: str, since: str,
           max_events: int) -> dict[tuple, list[tuple[float, float]]]:
    """{(event, issue, ligne): [(instant, cote), ...]} trié par instant."""
    events = [r[0] for r in conn.execute(
        "SELECT DISTINCT event_key FROM quotes "
        "WHERE book=? AND market=? AND fetched_at > ? LIMIT ?",
        (book, market, since, max_events),
    )]
    if not events:
        return {}
    marks = ",".join("?" * len(events))
    rows = conn.execute(
        f"SELECT event_key, outcome_label, line, decimal_odd, fetched_at "
        f"FROM quotes WHERE book=? AND market=? AND fetched_at > ? "
        f"AND event_key IN ({marks}) ORDER BY fetched_at",
        (book, market, since, *events),
    )
    out: dict[tuple, list[tuple[float, float]]] = defaultdict(list)
    for ek, label, line, odd, ts in rows:
        t = _parse(ts)
        if t is None or not odd or odd <= 1.0:
            continue
        out[(ek, label, line)].append((t, float(odd)))
    return out


def drift(sel: dict[tuple, list[tuple[float, float]]]) -> dict[int, list[float]]:
    """Variation relative de la cote, en %, pour chaque écart de temps visé."""
    per_lag: dict[int, list[float]] = {lag: [] for lag, _ in LAGS}
    for points in sel.values():
        if len(points) < 2:
            continue
        for i, (t0, o0) in enumerate(points):
            for lag, tol in LAGS:
                # Première capture qui tombe dans la fenêtre visée.
                for t1, o1 in points[i + 1:]:
                    dt = t1 - t0
                    if dt < lag - tol:
                        continue
                    if dt > lag + tol:
                        break
                    per_lag[lag].append(abs(o1 - o0) / o0 * 100.0)
                    break
    return per_lag


def show(title: str, per_lag: dict[int, list[float]]) -> None:
    print(f"\n=== {title} ===")
    print(f"{'écart':>7} {'paires':>8} {'médiane':>9} {'moyenne':>9} "
          f"{'p90':>7} {'inchangé':>9}")
    for lag, _ in LAGS:
        v = per_lag.get(lag) or []
        if not v:
            print(f"{lag:>6}s {'—':>8}")
            continue
        v.sort()
        still = sum(1 for x in v if x < 1e-9) / len(v) * 100
        p90 = v[int(len(v) * 0.9)]
        print(f"{lag:>6}s {len(v):>8} {statistics.median(v):>8.3f}% "
              f"{statistics.fmean(v):>8.3f}% {p90:>6.2f}% {still:>8.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=None)
    ap.add_argument("--hours", type=float, default=6.0)
    ap.add_argument("--market", default="h2h")
    ap.add_argument("--max-events", type=int, default=150,
                    help="Échantillon d'événements — la table fait des dizaines "
                         "de millions de lignes, tout lire est inutile.")
    args = ap.parse_args()

    db = args.db
    if db is None:
        from src.config import ScanConfig
        db = ScanConfig().db_path
    since = (datetime.now(timezone.utc)
             - timedelta(hours=args.hours)).isoformat()

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    books = [r[0] for r in conn.execute(
        "SELECT DISTINCT book FROM quotes WHERE fetched_at > ?", (since,))]

    print(f"Base {db} — {args.hours:g} h, marché {args.market}, "
          f"échantillon de {args.max_events} événements par book")

    if "pinnacle" in books:
        show("PINNACLE — coût d'une référence périmée",
             drift(series(conn, "pinnacle", args.market, since, args.max_events)))
        print("\n  Une médiane de X % à 60 s signifie qu'espacer les appels")
        print("  d'une minute fausse l'EV de X point en médiane.")

    print("\n\n########  VITESSE DES BOOKS BELGES  ########")
    print("Autre question : pendant combien de temps une occasion reste jouable.")
    for book in sorted(b for b in books if b != "pinnacle"):
        per_lag = drift(series(conn, book, args.market, since, args.max_events))
        if any(per_lag.values()):
            show(book, per_lag)


if __name__ == "__main__":
    main()
