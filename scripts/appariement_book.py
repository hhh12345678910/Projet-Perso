"""Les détections d'un book sont-elles bien appariées ? LECTURE SEULE.

Un faux appariement (Féroé contre Angleterre, 28/09) donne une fausse EV ET
une fausse CLV du même montant : la clôture est lue sur le même mauvais
match. La CLV moyenne ne le voit donc pas — il faut regarder les paris.

Pour chaque détection du book depuis une date, la sonde lit ce que la
production a enregistré au moment même (`bet_features`) : le score du
rapprochement des noms (100 = égalité exacte de clé, moins = rapprochement
flou) et le décalage d'horaire avec la référence. Elle compare la CLV des
appariements exacts et flous, puis liste les détections à vérifier :

  - appariement flou (score < 100) ;
  - horaire décalé d'une heure ou plus ;
  - EV ≥ 20 % (la signature d'un faux appariement : Féroé était à +424 %) ;
  - CLV ≤ −10 % (un match inversé ou une mauvaise équipe).

Le match affiché est celui de la RÉFÉRENCE (Pinnacle). Pour comparer avec
les noms et les cotes du book : `scripts/diag_match.py <équipe1> <équipe2>`.

    .venv/bin/python -m scripts.appariement_book --book vivatbet --depuis 2026-09-29
"""
from __future__ import annotations

import argparse
import sqlite3

from src.clv import clv_pct

REQUETE = """
SELECT vb.id, substr(vb.detected_at, 1, 16) AS vu, e.home, e.away,
       vb.market, vb.outcome_label AS pari, vb.line, vb.odd_taken AS cote,
       vb.fair_odd AS juste, vb.ev_pct AS ev, bf.match_score AS score,
       bf.time_shift_min AS decalage,
       (SELECT cs.fair_odd FROM clv_snapshots cs WHERE cs.value_bet_id = vb.id
         AND cs.closing = 1 AND cs.fair_odd > 0 ORDER BY cs.id DESC LIMIT 1) AS cloture
FROM value_bets vb
LEFT JOIN events e ON e.event_key = vb.event_key
LEFT JOIN bet_features bf ON bf.value_bet_id = vb.id
WHERE vb.book = ? AND vb.detected_at >= ?
ORDER BY vb.detected_at
"""

EV_SUSPECTE = 20.0
CLV_SUSPECTE = -10.0
DECALAGE_MIN = 60.0


def clv(r) -> "float | None":
    return clv_pct(float(r["cote"]), float(r["cloture"])) * 100 if r["cloture"] else None


def motifs(r) -> list[str]:
    m = []
    if r["score"] is not None and r["score"] < 100:
        m.append(f"appariement flou {r['score']:.0f}")
    if r["decalage"] and abs(r["decalage"]) >= DECALAGE_MIN:
        m.append(f"horaire décalé {r['decalage']:+.0f} min")
    if r["ev"] >= EV_SUSPECTE:
        m.append(f"EV {r['ev']:+.1f} %")
    c = clv(r)
    if c is not None and c <= CLV_SUSPECTE:
        m.append(f"CLV {c:+.1f} %")
    return m


def _moyenne(lot) -> str:
    v = [clv(r) for r in lot if clv(r) is not None]
    return f"CLV {sum(v) / len(v):+.2f} % sur {len(v)} clôtures" if v else "aucune clôture"


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--book", default="vivatbet")
    ap.add_argument("--depuis", default="2026-09-29", metavar="AAAA-MM-JJ",
                    help="Détections à partir de ce jour (UTC) inclus.")
    ap.add_argument("--db", default="data/valuebet.db")
    a = ap.parse_args(argv)
    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(REQUETE, (a.book, a.depuis)).fetchall()

    exacts = [r for r in rows if r["score"] is not None and r["score"] >= 100]
    flous = [r for r in rows if r["score"] is not None and r["score"] < 100]
    print(f"{a.book} depuis le {a.depuis} : {len(rows)} détections")
    print(f"  appariement exact (score 100)  : {len(exacts):5}   {_moyenne(exacts)}")
    print(f"  appariement flou  (score < 100): {len(flous):5}   {_moyenne(flous)}")
    print(f"  sans score enregistré          : {len(rows) - len(exacts) - len(flous):5}")

    a_voir = [(r, motifs(r)) for r in rows if motifs(r)]
    print(f"\nÀ VÉRIFIER : {len(a_voir)} détection(s) — match de la référence · pari · "
          "cote prise / cote juste · CLV · motifs\n")
    for r, m in a_voir:
        c = clv(r)
        pari = r["pari"] + (f" {r['line']:g}" if r["line"] is not None else "")
        print(f"  #{r['id']:<7} {r['vu']}  {(r['home'] or '?')[:22]:>22} - "
              f"{(r['away'] or '?')[:22]:<22} {r['market']:<6} {pari:<10} "
              f"{r['cote']:5.2f}/{r['juste']:5.2f}  "
              f"CLV {'—' if c is None else f'{c:+.1f} %':>8}   ← {', '.join(m)}")
    if not a_voir:
        print("  aucune — rien d'anormal dans ce que la production a enregistré.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
