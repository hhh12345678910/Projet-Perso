"""Vitesse de dérive des cotes — combien coûte une référence vieille de N secondes ?

    .venv/bin/python tools/line_speed.py --hours 6 --sport soccer

⚠️ FAUSSÉ DEPUIS L'ÉCRITURE PARCIMONIEUSE — à repenser avant de s'y fier.

`quotes` ne reçoit plus que les marchés qui ont BOUGÉ : une cote inchangée
n'y laisse plus de ligne. Or c'est exactement ce que cet outil comptait pour
établir ses « 99,6 % de cotes identiques ». Il ne voit donc plus que les
changements, et surestimera massivement la vitesse de dérive.

La mesure reste faisable, mais autrement : le taux d'inchangé se déduit
désormais de l'intervalle ENTRE deux écritures d'un même marché, et non du
nombre de lignes identiques. Le battement de cœur (QUOTES_HEARTBEAT_SEC)
fixe la borne supérieure de cet intervalle et doit être retiré du calcul.

En attendant, `odds_history` porte la même information sous une forme déjà
correcte — une ligne par changement — et `export-curves` la sort.

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
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Lancé en script depuis tools/, la racine du projet n'est pas sur sys.path :
# `from src.config import ...` échoue alors avec ModuleNotFoundError.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def load_all(conn: sqlite3.Connection, market: str, since: str,
             max_events: int, max_rows: int,
             progress=None) -> dict[str, dict[tuple, list[tuple[float, float]]]]:
    """Tout charger en UNE passe bornée : {book: {(event, issue, ligne): série}}.

    La version d'origine faisait, par book, un `SELECT DISTINCT event_key
    WHERE book=? AND market=?` puis un second passage avec un `IN (…)`. Or
    `quotes` n'est indexée que sur `event_key` et `fetched_at` : il n'existe
    aucun index sur `book` ni sur `market`. Le planificateur partait donc en
    balayage complet — cent millions de lignes — et recommençait pour chaque
    book. La commande paraissait figée.

    Ici, une seule requête suit explicitement l'index sur `fetched_at`
    (`INDEXED BY`), s'arrête à `max_rows`, et la répartition par book se fait
    en mémoire. Le tri par book n'a jamais eu besoin d'être fait par SQLite.

    Prendre les lignes les PLUS ANCIENNES de la fenêtre est volontaire : il
    faut une tranche continue plus longue que le plus grand écart mesuré
    (300 s), pas un échantillon dispersé sur deux heures."""
    sql = ("SELECT book, event_key, outcome_label, line, decimal_odd, fetched_at "
           "FROM quotes INDEXED BY idx_quotes_fetched "
           "WHERE fetched_at > ? AND market = ? "
           "ORDER BY fetched_at LIMIT ?")
    try:
        cur = conn.execute(sql, (since, market, max_rows))
    except sqlite3.OperationalError:
        # Index absent (base ancienne) : on laisse le planificateur choisir.
        cur = conn.execute(sql.replace("INDEXED BY idx_quotes_fetched", ""),
                           (since, market, max_rows))

    out: dict[str, dict[tuple, list[tuple[float, float]]]] = defaultdict(
        lambda: defaultdict(list))
    seen_events: dict[str, set] = defaultdict(set)
    n = 0
    for book, ek, label, line, odd, ts in cur:
        n += 1
        if progress and n % 50_000 == 0:
            progress(n)
        t = _parse(ts)
        if t is None or not odd or odd <= 1.0:
            continue
        # Échantillon d'événements par book : une fois le quota atteint, on
        # continue de suivre CEUX-LÀ, sinon les séries seraient tronquées et
        # aucune paire ne se formerait.
        ev = seen_events[book]
        if ek not in ev:
            if len(ev) >= max_events:
                continue
            ev.add(ek)
        out[book][(ek, label, line)].append((t, float(odd)))
    if progress:
        progress(n, final=True)
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
                    help="Échantillon d'événements par book — la table fait des "
                         "dizaines de millions de lignes, tout lire est inutile.")
    ap.add_argument("--max-rows", type=int, default=400_000,
                    help="Plafond DUR de lignes lues. C'est lui qui borne la "
                         "durée : `quotes` n'a aucun index sur book ni market, "
                         "donc sans plafond la requête balaie la table entière.")
    args = ap.parse_args()

    db = args.db
    if db is None:
        try:
            from src.config import ScanConfig
            db = ScanConfig().db_path
        except ImportError:
            # Repli sur le chemin par défaut : l'outil doit rester utilisable
            # même détaché de son dépôt.
            db = str(Path(__file__).resolve().parent.parent / "data" / "valuebet.db")
    if not Path(db).exists():
        raise SystemExit(f"Base introuvable : {db} (utilise --db)")
    since = (datetime.now(timezone.utc)
             - timedelta(hours=args.hours)).isoformat()

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    print(f"Base {db} — {args.hours:g} h, marché {args.market}, "
          f"échantillon de {args.max_events} événements par book, "
          f"{args.max_rows:,} lignes max")

    # Retour visible : sans lui, une lecture de plusieurs centaines de milliers
    # de lignes est indiscernable d'un blocage, et on la tue à tort.
    def _tick(n: int, final: bool = False) -> None:
        end = "\n" if final else "\r"
        print(f"  lecture : {n:>9,} lignes…", end=end, flush=True)

    data = load_all(conn, args.market, since, args.max_events,
                    args.max_rows, progress=_tick)
    if not data:
        print("Aucune cote sur cette fenêtre — élargis --hours.")
        return
    span = max((t for d in data.values() for s in d.values() for t, _ in s),
               default=0) - min((t for d in data.values() for s in d.values()
                                 for t, _ in s), default=0)
    print(f"  couvre {span / 60:.0f} min de captures\n")
    if span < max(l for l, _ in LAGS):
        print(f"  ⚠️  tranche plus courte que le plus grand écart mesuré "
              f"({max(l for l, _ in LAGS)}s) : augmente --max-rows.\n")

    if "pinnacle" in data:
        show("PINNACLE — coût d'une référence périmée", drift(data["pinnacle"]))
        print("\n  Une médiane de X % à 60 s signifie qu'espacer les appels")
        print("  d'une minute fausse l'EV de X point en médiane.")

    print("\n\n########  VITESSE DES BOOKS BELGES  ########")
    print("Autre question : pendant combien de temps une occasion reste jouable.")
    for book in sorted(b for b in data if b != "pinnacle"):
        per_lag = drift(data[book])
        if any(per_lag.values()):
            show(book, per_lag)


if __name__ == "__main__":
    main()
