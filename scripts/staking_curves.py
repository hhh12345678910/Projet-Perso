#!/usr/bin/env python3
"""Trois façons de miser sur la MÊME population, et leurs courbes d'équité.

La question n'est pas « laquelle gagne le plus » — une mise deux fois plus
grosse gagne deux fois plus sur un edge positif, ça ne prouve rien. La question
est ce que chacune coûte en EXPOSITION et en creux pour ce qu'elle rapporte.
D'où trois chiffres côte à côte : le ROI (qui normalise), le drawdown maximal
(qui dit ce qu'il faut encaisser), et le capital réellement engagé.

LES TROIS SCHÉMAS, tous reconstruits depuis le CODE DE PRODUCTION :

* **fixe**            — `--mise` € sur chaque pari, sans exception.
* **Kelly 1/4**       — `value_bets.kelly_pct`, qui est DÉJÀ du quart de Kelly
                        (`detection.py:266` : `kelly_fraction × 0,25 × 100`),
                        plafonné à `TELEGRAM_MAX_STAKE_PCT` comme en production.
* **actuel**          — `alerter._advised_stake_eur` appelée telle quelle, donc
                        exactement ce que ton `.env` conseille aujourd'hui. En
                        mode `flat` c'est ta règle 35 € / 45 € au-dessus de
                        `STAKE_EV_TIER`.

Aucune formule n'est recopiée : si tu changes un réglage, cette sonde change
avec lui (§17.7).

⚠️ LA COMPARAISON EN EUROS EST TROMPEUSE À ELLE SEULE. Le quart de Kelly mise
typiquement 2 à 3 fois la mise fixe sur ce portefeuille (EV 12 % à cote 2,5
donne ~2 % de bankroll, soit ~75 € pour 3 720 € de capital). Son P&L plus gros
n'est donc pas une supériorité, c'est un levier. Lis le ROI et le drawdown,
pas le P&L final.

⚠️ La bankroll est FIXE dans le calcul (pas de composition) : c'est ce qui rend
les trois courbes comparables. En composant, Kelly gagnerait mécaniquement sur
une série gagnante et le tableau ne dirait plus rien du schéma lui-même.

Les paris sont ordonnés par HEURE DE COUP D'ENVOI — l'instant où le résultat
tombe, donc où le capital bouge réellement. Les ordonner par détection
mélangerait des paris déjà réglés avec d'autres qui ne le sont pas encore.

Usage :
    .venv/bin/python -m scripts.staking_curves --premium --books kambi,ladbrokes_be
    .venv/bin/python -m scripts.staking_curves --premium --books kambi,ladbrokes_be \\
        --out courbes.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.clv import pnl as clv_pnl  # noqa: E402
from src.clv import settle as clv_settle  # noqa: E402
from src.config import load_env_file  # noqa: E402
from src.reference import KAMBI_BOOKS  # noqa: E402
from scripts.pnl_detections import porte_de_canal  # noqa: E402

_ALIAS = {"kambi": tuple(b.value for b in KAMBI_BOOKS)}


def _books_demandes(brut):
    if not brut:
        return None
    out = set()
    for m in (x.strip().lower() for x in brut.split(",")):
        if m:
            out.update(_ALIAS.get(m, (m,)))
    return out or None


def _drawdown(serie):
    """Plus forte baisse pic-à-creux, le sommet global, et le sommet d'où
    cette baisse est partie.

    ⚠️ Les deux derniers ne sont PAS la même chose, et les confondre rend le
    tableau incohérent : une colonne « pic » qui vaut 1 644 € en face d'un P&L
    final de +11 180 € se lit comme un bug alors qu'elle décrit seulement le
    sommet d'où le pire creux a démarré. Elles sont donc rendues séparément."""
    pic = 0.0
    dd_max = 0.0
    pic_du_max = 0.0
    for v in serie:
        if v > pic:
            pic = v
        creux = pic - v
        if creux > dd_max:
            dd_max, pic_du_max = creux, pic
    return dd_max, pic, pic_du_max


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--premium", action="store_true",
                    help="Filtrer par la porte RÉELLE du canal premium.")
    ap.add_argument("--canal", default=None, metavar="NOM")
    ap.add_argument("--books", default=None, metavar="LISTE",
                    help="Books séparés par des virgules. Alias : kambi.")
    ap.add_argument("--mise", type=float, default=35.0, metavar="EUR",
                    help="Mise du schéma fixe (défaut 35).")
    ap.add_argument("--bankroll", type=float, default=None, metavar="EUR",
                    help="Capital pour le Kelly. Défaut : TELEGRAM_BANKROLL "
                         "de ton .env.")
    ap.add_argument("--out", default=None, metavar="CSV",
                    help="Écrire la courbe (un point par pari réglé).")
    a = ap.parse_args()
    if a.canal:
        a.premium = True
    load_env_file()   # AVANT d'importer alerter : ses réglages sont lus à l'import

    from src.alerter import (_MAX_STAKE_PCT, _STAKE_BASE_EUR, _STAKE_EV_MULT,
                             _STAKE_EV_TIER, _STAKE_MODE, _STAKE_PCT,
                             _advised_stake_eur, _round_stake)

    bankroll = a.bankroll if a.bankroll is not None else float(
        os.getenv("TELEGRAM_BANKROLL", "1000"))

    porte, porte_desc = (porte_de_canal(a.db, a.canal) if a.premium
                         else (None, "aucune — toutes les détections"))
    books = _books_demandes(a.books)

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = list(con.execute("""
        SELECT vb.id, vb.book, vb.market, vb.outcome_label, vb.line,
               vb.odd_taken, vb.ev_pct, vb.kelly_pct, vb.detected_at,
               e.sport AS sport, e.league AS league,
               e.home AS home, e.away AS away, e.start_time AS start_time,
               r.winner, r.home_score, r.away_score
        FROM value_bets vb
        JOIN results r      ON r.event_key = vb.event_key
        LEFT JOIN events e  ON e.event_key = vb.event_key
    """))
    if not rows:
        raise SystemExit("Aucune détection avec résultat. Lance `results-update`.")

    gardees = [r for r in rows
               if (books is None or (r["book"] or "").lower() in books)
               and (porte is None or porte(r))]

    # Même clé que `pnl_detections` : équipes + jour + marché + pari (§17.8).
    best = {}
    for r in gardees:
        cle = ((r["home"] or "").lower(), (r["away"] or "").lower(),
               (r["start_time"] or "")[:10], r["market"], r["outcome_label"],
               r["line"])
        prev = best.get(cle)
        if prev is None or float(r["odd_taken"]) > float(prev["odd_taken"]):
            best[cle] = r

    # Ordre chronologique du COUP D'ENVOI : c'est là que le capital bouge.
    opp = sorted(best.values(), key=lambda r: (r["start_time"] or ""))

    def mise_fixe(r):
        return _round_stake(a.mise)

    def mise_kelly(r):
        k = r["kelly_pct"]
        if k is None:
            return None
        return _round_stake(min(float(k), _MAX_STAKE_PCT) / 100.0 * bankroll)

    def mise_actuelle(r):
        return _advised_stake_eur(float(r["ev_pct"]), r["kelly_pct"], bankroll)

    # (libellé affiché, slug pour le CSV, fonction). Le slug est distinct :
    # « cumul_Kelly 1/4 » comme nom de colonne est illisible par un tableur et
    # pénible à relire par un script.
    SCHEMAS = [(f"fixe {a.mise:g} €", "fixe", mise_fixe),
               ("Kelly 1/4", "kelly", mise_kelly),
               ("actuel (.env)", "actuel", mise_actuelle)]

    courbes = {s: [] for _, s, _f in SCHEMAS}
    mises = {s: [] for _, s, _f in SCHEMAS}
    gains = {s: [] for _, s, _f in SCHEMAS}
    cumuls = {s: 0.0 for _, s, _f in SCHEMAS}
    points = []
    regles = 0

    for r in opp:
        statut = clv_settle(r["market"], r["outcome_label"], r["line"],
                            r["winner"], r["home_score"], r["away_score"])
        if clv_pnl(statut, float(r["odd_taken"]), 1.0) is None:
            continue          # résultat insuffisant pour ce marché
        regles += 1
        pt = {"n": regles, "date": (r["start_time"] or "")[:10],
              "sport": r["sport"] or "?", "cote": float(r["odd_taken"]),
              "ev_pct": round(float(r["ev_pct"]), 2), "statut": statut}
        for _lib, slug, calc in SCHEMAS:
            m = calc(r) or 0.0
            g = clv_pnl(statut, float(r["odd_taken"]), m) or 0.0
            cumuls[slug] += g
            mises[slug].append(m)
            gains[slug].append(g)
            courbes[slug].append(cumuls[slug])
            pt[f"mise_{slug}"] = round(m, 2)
            pt[f"cumul_{slug}"] = round(cumuls[slug], 2)
        points.append(pt)

    if not regles:
        raise SystemExit("Aucun pari réglé dans cette population.")

    print(f"\nTROIS SCHÉMAS DE MISE — porte : {porte_desc}")
    print(f"Books : {', '.join(sorted(books)) if books else 'tous'}"
          f"   ·   bankroll {bankroll:.0f} €   ·   {regles} paris réglés, "
          f"du {points[0]['date']} au {points[-1]['date']}")
    print(f"Réglages lus dans ton .env : STAKE_MODE={_STAKE_MODE}, "
          f"STAKE_PCT={_STAKE_PCT:g}, STAKE_BASE_EUR={_STAKE_BASE_EUR:g}, "
          f"STAKE_EV_TIER={_STAKE_EV_TIER:g}, STAKE_EV_MULT={_STAKE_EV_MULT:g}, "
          f"plafond Kelly={_MAX_STAKE_PCT:g} %\n")

    e = (f"{'schéma':16}{'mise moy':>10}{'mise max':>10}{'total misé':>13}"
         f"{'P&L':>11}{'ROI':>9}{'drawdown max':>15}{'% bankroll':>12}"
         f"{'sommet':>11}{'creux parti de':>16}")
    print(e)
    print("-" * len(e))
    for lib, slug, _f in SCHEMAS:
        total = sum(mises[slug])
        pl = sum(gains[slug])
        dd, sommet, pic_du_creux = _drawdown(courbes[slug])
        roi = 100.0 * pl / total if total else 0.0
        print(f"{lib:16}{st.mean(mises[slug]):9.1f}€{max(mises[slug]):9.0f}€"
              f"{total:12,.0f}€{pl:+10.0f}€{roi:+8.2f}%"
              f"{-dd:14,.0f}€{100.0 * dd / bankroll:11.1f}%"
              f"{sommet:10,.0f}€{pic_du_creux:15,.0f}€".replace(",", " "))

    print("\n⚠️ Le P&L le plus gros n'est PAS le meilleur schéma : Kelly mise "
          "plus, donc\n   il gagne plus sur un edge positif ET creuse plus. Ce "
          "qui se compare, c'est\n   le ROI (à euro risqué égal) et le drawdown "
          "(ce qu'il faut encaisser).")
    print("⚠️ Bankroll FIXE, sans composition — sinon la comparaison mesurerait "
          "la\n   composition et non le schéma. Et le drawdown est celui du "
          "P&L cumulé,\n   ordonné par coup d'envoi.")

    if a.out:
        champs = list(points[0].keys())
        with open(a.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=champs)
            w.writeheader()
            w.writerows(points)
        print(f"\n✓ Courbe écrite : {a.out}  ({len(points)} points, "
              f"{len(champs)} colonnes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
