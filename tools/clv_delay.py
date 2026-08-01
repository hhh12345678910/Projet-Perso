"""Le délai avant coup d'envoi vaut-il un filtre ?

    .venv/bin/python tools/clv_delay.py
    .venv/bin/python tools/clv_delay.py --min-ev 8 --min-odd 1.5 --max-odd 6
    .venv/bin/python tools/clv_delay.py --played-only

Pourquoi cet outil
------------------
`clv-report` ventile par book, marché, sport et tranche d'EV. Il ne ventile
pas par délai, et surtout il ne déduplique pas — or les deux sont
indispensables pour décider s'il faut couper les paris détectés loin du match.

Deux précautions que ce script prend et qu'il ne faut jamais sauter :

1. **Déduplication par opportunité.** Une même sélection est détectée sur
   plusieurs books. Ce sont des observations corrélées, pas indépendantes, et
   tu n'en joues qu'une — la suppression au niveau du marché silence les
   autres. Ne pas dédupliquer double artificiellement les effectifs et gonfle
   toutes les significativités.

   Le mode par défaut moyenne le CLV à l'intérieur de chaque opportunité.
   `--pick best` garderait la meilleure cote, ce qui biaise vers le haut :
   on sélectionnerait sur le prix, c'est-à-dire sur le numérateur même du CLV.
   `--pick first` reflète la réalité opérationnelle (la première alerte).

2. **La significativité, pas la moyenne.** Une tranche à −2,7 % sur 35 paris
   ne dit rien. Chaque ligne porte donc son σ, et la décision se juge sur la
   comparaison entre ce qu'on garderait et ce qu'on couperait, pas sur le
   classement des moyennes.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.matcher import parse_event_key  # noqa: E402


# Bornes en heures. Le découpage suit celui du §9 du HANDOVER pour rester
# comparable aux mesures précédentes.
BUCKETS = [
    ("< 2 h", 0.0, 2.0),
    ("2-6 h", 2.0, 6.0),
    ("6-12 h", 6.0, 12.0),
    ("12-24 h", 12.0, 24.0),
    ("24-48 h", 24.0, 48.0),
    ("> 48 h", 48.0, float("inf")),
]


def _norm_p(t: float) -> float:
    """p bilatéral approché par la loi normale — les effectifs sont assez
    grands pour que la différence avec Student soit sans portée ici."""
    return math.erfc(abs(t) / math.sqrt(2.0))


class Stats:
    def __init__(self, values: list[float]):
        self.v = values
        self.n = len(values)
        self.mean = statistics.fmean(values) if values else 0.0
        self.sd = statistics.stdev(values) if len(values) > 1 else 0.0
        self.se = self.sd / math.sqrt(self.n) if self.n > 1 else 0.0
        self.t = self.mean / self.se if self.se else 0.0
        self.median = statistics.median(values) if values else 0.0
        self.pos = sum(1 for x in values if x > 0) / self.n * 100 if values else 0.0


def welch(a: Stats, b: Stats) -> tuple[float, float]:
    """t de Welch entre deux groupes indépendants, et p bilatéral."""
    if a.n < 2 or b.n < 2:
        return 0.0, 1.0
    se = math.sqrt(a.se ** 2 + b.se ** 2)
    if se == 0:
        return 0.0, 1.0
    t = (a.mean - b.mean) / se
    return t, _norm_p(t)


def load(db: str, args) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    sql = (
        "SELECT vb.event_key, vb.market, vb.outcome_label, vb.line, vb.book, "
        "vb.odd_taken, vb.ev_pct, vb.detected_at, "
        "cs.pinnacle_odd AS closing_raw, cs.fair_odd AS closing_fair, "
        "e.sport AS sport, pb.dedup_key IS NOT NULL AS played "
        "FROM value_bets vb "
        "JOIN clv_snapshots cs ON cs.value_bet_id = vb.id AND cs.closing = 1 "
        "LEFT JOIN events e ON e.event_key = vb.event_key "
        "LEFT JOIN played_bets pb ON pb.value_bet_id = vb.id "
        "WHERE cs.fair_odd IS NOT NULL"
    )
    out = []
    for r in conn.execute(sql):
        d = dict(r)
        parsed = parse_event_key(d["event_key"])
        if parsed is None or not d["detected_at"]:
            continue
        try:
            det = datetime.fromisoformat(d["detected_at"])
        except ValueError:
            continue
        if det.tzinfo is None:
            det = det.replace(tzinfo=timezone.utc)
        d["delay_h"] = (parsed[0] - det).total_seconds() / 3600.0
        # Délai négatif = détection sur un match déjà commencé, comparée à une
        # ligne de référence prématch figée au coup d'envoi. Structurellement
        # invalide (§9), et jamais alertée. Toujours écartée.
        if d["delay_h"] <= 0:
            continue
        taken = float(d["odd_taken"])
        d["clv"] = (taken / float(d["closing_fair"]) - 1.0) * 100.0
        d["clv_old"] = (
            (taken / float(d["closing_raw"]) - 1.0) * 100.0 if d["closing_raw"] else None
        )
        if not (args.min_odd <= taken <= args.max_odd):
            continue
        if float(d["ev_pct"]) < args.min_ev:
            continue
        if args.played_only and not d["played"]:
            continue
        if args.sport and (d["sport"] or "") != args.sport:
            continue
        out.append(d)
    conn.close()
    return out


def dedup(rows: list[dict], how: str) -> list[dict]:
    """Une ligne par opportunité (match + marché + issue + ligne)."""
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["event_key"], r["market"], r["outcome_label"], r["line"])].append(r)
    out = []
    for g in groups.values():
        if how == "best":
            out.append(max(g, key=lambda r: r["odd_taken"]))
        elif how == "first":
            out.append(min(g, key=lambda r: r["detected_at"]))
        else:  # mean — moyenne intra-grappe, le moins biaisé
            base = dict(min(g, key=lambda r: r["detected_at"]))
            base["clv"] = statistics.fmean(r["clv"] for r in g)
            olds = [r["clv_old"] for r in g if r["clv_old"] is not None]
            base["clv_old"] = statistics.fmean(olds) if olds else None
            base["n_books"] = len(g)
            out.append(base)
    return out


def table(title: str, lines: list[tuple[str, Stats]]) -> None:
    print(f"\n{title}")
    print(f"  {'tranche':<10} {'n':>5} {'CLV moy':>9} {'médiane':>9} {'σ':>7} {'positifs':>9}")
    for label, s in lines:
        if s.n == 0:
            print(f"  {label:<10} {'—':>5}")
            continue
        print(f"  {label:<10} {s.n:>5} {s.mean:>+8.2f}% {s.median:>+8.2f}% "
              f"{s.t:>7.1f} {s.pos:>8.1f}%")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", default="data/valuebet.db")
    p.add_argument("--min-odd", type=float, default=1.0)
    p.add_argument("--max-odd", type=float, default=100.0)
    p.add_argument("--min-ev", type=float, default=0.0)
    p.add_argument("--sport", default="")
    p.add_argument("--played-only", action="store_true")
    p.add_argument("--pick", choices=("mean", "first", "best"), default="mean")
    args = p.parse_args()

    raw = load(args.db, args)
    if not raw:
        print("Aucun pari clôturé avec ligne dévigée sur ce filtre.")
        return
    rows = dedup(raw, args.pick)

    filt = []
    if args.min_odd > 1.0 or args.max_odd < 100.0:
        filt.append(f"cotes {args.min_odd}-{args.max_odd}")
    if args.min_ev:
        filt.append(f"EV ≥ {args.min_ev}%")
    if args.played_only:
        filt.append("joués seulement")
    if args.sport:
        filt.append(args.sport)
    print(f"Population : {len(raw)} lignes → {len(rows)} opportunités "
          f"(dédup « {args.pick} »)" + (f" — {', '.join(filt)}" if filt else ""))

    allv = Stats([r["clv"] for r in rows])
    print(f"\nEnsemble : n={allv.n}  CLV {allv.mean:+.2f}%  médiane {allv.median:+.2f}%  "
          f"σ={allv.t:.1f}  positifs {allv.pos:.1f}%")

    olds = [r["clv_old"] for r in rows if r["clv_old"] is not None]
    if olds:
        o = Stats(olds)
        print(f"Ancienne mesure (clôture brute, commission incluse) : "
              f"{o.mean:+.2f}%  → écart {allv.mean - o.mean:+.2f} points")

    table("CLV par délai avant coup d'envoi",
          [(lab, Stats([r["clv"] for r in rows if lo <= r["delay_h"] < hi]))
           for lab, lo, hi in BUCKETS])

    print("\nFaut-il couper ? — pour chaque règle : ce qu'on garde vs ce qu'on jette")
    print(f"  {'règle':<22} {'gardé n':>8} {'CLV':>8} {'jeté n':>7} {'CLV':>8} "
          f"{'écart':>8} {'σ':>6} {'p':>8}")
    for label, keep_fn in (
        ("couper > 24 h", lambda r: r["delay_h"] <= 24),
        ("couper > 48 h", lambda r: r["delay_h"] <= 48),
        ("couper 24-48 h", lambda r: not (24 < r["delay_h"] <= 48)),
        ("couper > 12 h", lambda r: r["delay_h"] <= 12),
    ):
        keep = Stats([r["clv"] for r in rows if keep_fn(r)])
        drop = Stats([r["clv"] for r in rows if not keep_fn(r)])
        if drop.n == 0:
            continue
        t, pv = welch(keep, drop)
        print(f"  {label:<22} {keep.n:>8} {keep.mean:>+7.2f}% {drop.n:>7} "
              f"{drop.mean:>+7.2f}% {keep.mean - drop.mean:>+7.2f} {t:>6.1f} {pv:>8.4f}")

    print("\n  Lecture : une règle ne vaut que si le groupe JETÉ est")
    print("  significativement moins bon — et pas seulement plus bas en moyenne.")
    print("  Un groupe jeté dont le CLV reste positif est du volume rentable perdu.")


if __name__ == "__main__":
    main()
