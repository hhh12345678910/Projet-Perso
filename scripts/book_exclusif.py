#!/usr/bin/env python3
"""Ce que couper un book coûterait — et ce que ça ne coûterait PAS.

LA QUESTION QU'ELLE TRANCHE
---------------------------
Un book lent tient le chemin critique du cycle. Le couper rend des secondes.
Mais il ne rend des secondes que s'il ne coûte pas des paris — et « combien de
paris viennent de ce book » est la MAUVAISE question. La bonne se coupe en
deux, parce que les deux moitiés ont des conséquences opposées :

  * les opportunités où il est SEUL disparaîtraient entièrement ;
  * les opportunités qu'un autre book propose aussi survivraient — à la cote
    de cet autre book, donc avec MOINS d'EV et une CLV différente.

Cette sonde chiffre les deux, et la CLV de chacune. Un book qui n'apporte que
des doublons est gratuit à couper ; un book seul sur ses paris ne l'est
jamais, même s'il est lent.

⚠️ LA CLV, PAS L'EV. L'EV dit ce que le calcul croyait au moment de la
détection ; la CLV dit ce que le marché a fini par valider. Un book peut
sembler généreux (EV forte) parce que son prix est faux — c'est même le mode
de défaillance le plus courant. Les deux colonnes sont là, et quand elles
divergent c'est la CLV qui décide.

⚠️ CE QU'ELLE NE SAIT PAS. « Le même pari chez deux books » est identifié par
équipes + jour + marché + pari (§17.8) — jamais par `event_key`, dont le
tennis produit jusqu'à onze variantes pour un même match. Deux détections du
même jour comptent donc comme la même opportunité même si elles sont séparées
de plusieurs heures. La sonde dit combien sont dans ce cas plutôt que de le
taire : c'est le sens OPTIMISTE de l'erreur (elle fait paraître le book plus
remplaçable qu'il n'est), donc celui qu'il faut surveiller.

Usage :
    .venv/bin/python -m scripts.book_exclusif
    .venv/bin/python -m scripts.book_exclusif --book elitesports --avec unibet_be
    .venv/bin/python -m scripts.book_exclusif --jours 30 --premium
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics as st
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

RACINE = Path(__file__).resolve().parents[1]
if str(RACINE) not in sys.path:
    sys.path.insert(0, str(RACINE))

from src.clv import clv_pct  # noqa: E402

BASE = RACINE / "data" / "valuebet.db"

# ⚠️ MÊMES COLONNES QUE `clv_roi_matrix`. La clôture vient de `clv_snapshots`
# avec `closing = 1`, et c'est la fair odd DÉVIGÉE — jamais le prix affiché.
SQL = """
    SELECT vb.id, vb.book, vb.market, vb.outcome_label, vb.line,
           vb.odd_taken, vb.ev_pct, vb.detected_at,
           e.sport AS sport, e.home AS home, e.away AS away,
           e.start_time AS start_time,
           cs.fair_odd AS closing_fair_odd
    FROM value_bets vb
    LEFT JOIN clv_snapshots cs
           ON cs.value_bet_id = vb.id AND cs.closing = 1
    LEFT JOIN events e ON e.event_key = vb.event_key
"""


def _cle(r) -> tuple:
    """Équipes + jour + marché + pari (§17.8). JAMAIS `event_key`."""
    return ((r["home"] or "").lower(), (r["away"] or "").lower(),
            (r["start_time"] or "")[:10], r["market"], r["outcome_label"],
            r["line"])


def _ts(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _clv(r) -> float | None:
    """La CLV d'une ligne, ou None. JAMAIS zéro à la place d'inconnu."""
    v = r["closing_fair_odd"]
    if not v or float(v) <= 0:
        return None
    return clv_pct(float(r["odd_taken"]), float(v)) * 100.0


def _resume(rows: list) -> dict:
    clvs = [c for c in (_clv(r) for r in rows) if c is not None]
    evs = [float(r["ev_pct"]) for r in rows if r["ev_pct"] is not None]
    return {
        "n": len(rows),
        "n_clv": len(clvs),
        "clv": st.mean(clvs) if clvs else None,
        # σ de la MOYENNE, pas de la population : c'est elle qui dit si l'écart
        # entre deux lignes du tableau veut dire quelque chose.
        "sigma": (st.stdev(clvs) / len(clvs) ** 0.5
                  if len(clvs) > 1 else None),
        "ev": st.mean(evs) if evs else None,
    }


def _ligne(nom: str, d: dict) -> str:
    clv = f"{d['clv']:+7.2f} %" if d["clv"] is not None else "      —"
    sig = f"±{d['sigma']:.2f}" if d["sigma"] is not None else "    —"
    ev = f"{d['ev']:+7.2f} %" if d["ev"] is not None else "      —"
    return (f"  {nom:<28} {d['n']:>6} {d['n_clv']:>9} {clv} {sig:>7} {ev}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(BASE), help=f"Défaut : {BASE}")
    ap.add_argument("--book", default="elitesports",
                    help="Le book qu'on envisage de couper (défaut : "
                         "elitesports).")
    ap.add_argument("--avec", default="unibet_be",
                    help="Le book compagnon détaillé à part (défaut : "
                         "unibet_be).")
    ap.add_argument("--jours", type=float, default=0.0, metavar="N",
                    help="Ne garder que les N derniers jours de détection.")
    ap.add_argument("--sport", default=None, help="Un seul sport.")
    a = ap.parse_args()

    chemin = Path(a.db)
    if not chemin.exists():
        raise SystemExit(f"Base introuvable : {chemin}")
    cible, compagnon = a.book.lower(), a.avec.lower()

    con = sqlite3.connect(f"file:{chemin}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = list(con.execute(SQL))
    con.close()
    if not rows:
        raise SystemExit("Aucune détection en base.")
    total_brut = len(rows)

    if a.sport:
        rows = [r for r in rows if (r["sport"] or "").lower() == a.sport.lower()]
    fenetre = ""
    if a.jours:
        # ⚠️ SUR `detected_at`, qui ne bouge jamais (§14.5) : la fenêtre
        # découpe QUAND LE PRIX EST APPARU, pas quand on l'a revu.
        limite = datetime.now(timezone.utc).timestamp() - a.jours * 86400
        rows = [r for r in rows if (_ts(r["detected_at"]) or 0.0) >= limite]
        fenetre = f"Fenêtre : {a.jours:g} derniers jours"
    if not rows:
        raise SystemExit("Aucune détection après filtrage.")

    # Une opportunité = une clé §17.8, et la liste des books qui l'ont vue.
    par_cle: dict[tuple, list] = defaultdict(list)
    for r in rows:
        par_cle[_cle(r)].append(r)

    # Pour chaque opportunité, la MEILLEURE ligne de chaque book : un book qui
    # a redétecté le même pari trois fois ne pèse pas trois fois.
    opportunites: list[dict] = []
    for cle, lot in par_cle.items():
        meilleur: dict[str, sqlite3.Row] = {}
        for r in lot:
            b = (r["book"] or "").lower()
            if b not in meilleur or float(r["odd_taken"]) > float(meilleur[b]["odd_taken"]):
                meilleur[b] = r
        if cible in meilleur:
            opportunites.append({"cle": cle, "books": meilleur})

    if not opportunites:
        raise SystemExit(
            f"Aucune détection pour « {cible} ». Books en base : "
            + ", ".join(sorted({(r['book'] or '').lower() for r in rows})))

    seules = [o for o in opportunites if len(o["books"]) == 1]
    avec_compagnon = [o for o in opportunites if compagnon in o["books"]]
    accompagnees = [o for o in opportunites if len(o["books"]) > 1]

    print(f"\nCOUPER « {cible.upper()} » — CE QUE ÇA COÛTERAIT — {chemin}")
    if fenetre:
        print(fenetre + (f", sport {a.sport}" if a.sport else ""))
    print(f"{len(opportunites)} opportunités où il est présent "
          f"(sur {total_brut} lignes en base)")

    print("\n── SA CLV, SELON QU'IL EST SEUL OU ACCOMPAGNÉ ──")
    print(f"  {'situation':<28} {'n':>6} {'clôtures':>9} {'CLV moy':>9} "
          f"{'σmoy':>7} {'EV moy':>9}")
    lignes_cible = [o["books"][cible] for o in opportunites]
    print(_ligne("globale", _resume(lignes_cible)))
    print(_ligne("SEUL (aucun autre book)",
                 _resume([o["books"][cible] for o in seules])))
    print(_ligne(f"avec {compagnon}",
                 _resume([o["books"][cible] for o in avec_compagnon])))
    print(_ligne("avec un autre book, quel qu'il soit",
                 _resume([o["books"][cible] for o in accompagnees])))

    # ── LA DÉCISION ──────────────────────────────────────────────────
    # Les opportunités accompagnées ne disparaissent pas : elles se rabattent
    # sur le MEILLEUR autre book. C'est cette cote-là qu'il faut comparer, pas
    # zéro — sinon la coupure paraît catastrophique alors qu'elle ne coûte
    # souvent que quelques centièmes de cote.
    ecarts, clv_avant, clv_apres = [], [], []
    for o in accompagnees:
        mien = o["books"][cible]
        autres = [r for b, r in o["books"].items() if b != cible]
        remplacant = max(autres, key=lambda r: float(r["odd_taken"]))
        ecarts.append(float(remplacant["odd_taken"]) / float(mien["odd_taken"]) - 1.0)
        # La clôture du remplaçant si elle existe, sinon celle de n'importe
        # quelle ligne du groupe : c'est le MÊME match et le MÊME pari, donc
        # la même ligne de clôture.
        cl = remplacant["closing_fair_odd"] or mien["closing_fair_odd"]
        if cl and float(cl) > 0:
            clv_avant.append(clv_pct(float(mien["odd_taken"]), float(cl)) * 100)
            clv_apres.append(clv_pct(float(remplacant["odd_taken"]), float(cl)) * 100)

    print("\n── CE QUE LA COUPURE COÛTERAIT VRAIMENT ──")
    pc_seules = 100 * len(seules) / len(opportunites)
    r_seules = _resume([o["books"][cible] for o in seules])
    print(f"  · {len(seules)} opportunités DISPARAÎTRAIENT ({pc_seules:.0f} % "
          f"des siennes) —")
    if r_seules["clv"] is not None:
        print(f"    CLV moyenne {r_seules['clv']:+.2f} % sur "
              f"{r_seules['n_clv']} clôtures. C'est la perte sèche.")
    else:
        print("    aucune clôture capturée : leur valeur est INCONNUE, pas "
              "nulle.")
    if accompagnees:
        moy_ecart = 100 * st.mean(ecarts)
        print(f"  · {len(accompagnees)} seraient rabattues sur le meilleur "
              f"autre book :")
        print(f"    cote {moy_ecart:+.2f} % en moyenne "
              f"({sum(1 for e in ecarts if e >= 0)} fois aussi bonne ou "
              f"meilleure, {sum(1 for e in ecarts if e < 0)} fois pire)")
        if clv_avant:
            d = st.mean(clv_apres) - st.mean(clv_avant)
            print(f"    CLV {st.mean(clv_avant):+.2f} % → "
                  f"{st.mean(clv_apres):+.2f} % ({d:+.2f} point(s)), sur "
                  f"{len(clv_avant)} clôtures")
    else:
        print("  · aucune n'est proposée ailleurs : TOUT disparaîtrait.")

    # ⚠️ LE SENS OPTIMISTE DE L'ERREUR, DIT PLUTÔT QUE TU. Deux détections du
    # même jour comptent comme la même opportunité même à des heures très
    # différentes — ce qui fait paraître le book PLUS remplaçable qu'il n'est.
    lointaines = 0
    for o in accompagnees:
        t = _ts(o["books"][cible]["detected_at"])
        autres = [_ts(r["detected_at"]) for b, r in o["books"].items()
                  if b != cible]
        autres = [x for x in autres if x is not None]
        if t is not None and autres and min(abs(t - x) for x in autres) > 3600:
            lointaines += 1
    if accompagnees:
        print(f"\n  ⚠️ {lointaines} des {len(accompagnees)} « accompagnées » "
              f"({100 * lointaines / len(accompagnees):.0f} %) ont leur "
              f"jumelle\n     détectée à plus d'une heure d'écart. Elles "
              f"comptent comme remplaçables\n     alors que le prix n'était "
              f"peut-être plus là. L'erreur va dans le sens\n     qui FLATTE "
              f"la coupure — la vraie perte est donc au moins celle-ci.")

    print("\nLecture seule — base ouverte en mode read-only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
