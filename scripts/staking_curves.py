#!/usr/bin/env python3
"""Plusieurs façons de miser sur la MÊME population, et leurs courbes d'équité.

La question n'est pas « laquelle gagne le plus » — une mise deux fois plus
grosse gagne deux fois plus sur un edge positif, ça ne prouve rien. La question
est ce que chacune coûte en EXPOSITION et en creux pour ce qu'elle rapporte.

LES SCHÉMAS, tous reconstruits depuis le CODE DE PRODUCTION :

* **fixe**            — `--mise` € sur chaque pari, sans exception.
* **Kelly f**         — `src.ev.kelly_stake(cote, 1/cote_juste, bankroll, f)`,
                        la formule des alertes et de l'Analytics, pour chaque
                        fraction de `--fractions` (1/4 par défaut), plafonnée
                        à `TELEGRAM_MAX_STAKE_PCT` et arrondie comme en
                        production.
* **Kelly f, même mise moyenne** — les mêmes mises Kelly, multipliées pour que
                        leur MOYENNE soit celle du fixe. Seule la RÉPARTITION
                        change (plus sur les grosses EV, moins ailleurs), pas
                        le capital engagé : c'est la comparaison à armes
                        égales qui répond à « mes creux seraient-ils plus
                        faibles ? ».
* **actuel**          — `alerter._advised_stake_eur` appelée telle quelle, donc
                        exactement ce que ton `.env` conseille aujourd'hui.

Aucune formule n'est recopiée : si tu changes un réglage, cette sonde change
avec lui (§17.7).

⚠️ LA COMPARAISON EN EUROS EST TROMPEUSE À ELLE SEULE. Le P&L ET le drawdown
grandissent tous deux avec le niveau de mise. Deux mesures ne dépendent pas de
ce niveau, et ce sont elles qui comparent les schémas :

  - **P&L / creux** : ce que le schéma rapporte par euro de pire baisse ;
  - **creux en mises** : le pire creux exprimé en mises moyennes du schéma.

⚠️ La bankroll est FIXE dans le calcul (pas de composition) : c'est ce qui rend
les courbes comparables. En composant, Kelly gagnerait mécaniquement sur une
série gagnante et le tableau ne dirait plus rien du schéma lui-même.

Les paris sont ordonnés par HEURE DE COUP D'ENVOI — l'instant où le résultat
tombe, donc où le capital bouge réellement.

Usage :
    # Tes paris JOUÉS (clics « Jouer »), 35 € contre Kelly 1/4 et 1/2
    .venv/bin/python -m scripts.staking_curves --joues --fractions 0.25,0.5 \\
        --bankroll 10000
    # Le flux premium entier
    .venv/bin/python -m scripts.staking_curves --premium --books kambi,ladbrokes_be
    # La courbe point par point, pour un tableur
    .venv/bin/python -m scripts.staking_curves --joues --out courbes.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
import statistics as st
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.clv import pnl as clv_pnl  # noqa: E402
from src.clv import settle as clv_settle  # noqa: E402
from src.config import load_env_file  # noqa: E402
from src.ev import kelly_stake  # noqa: E402
from src.reference import KAMBI_BOOKS  # noqa: E402

_ALIAS = {"kambi": tuple(b.value for b in KAMBI_BOOKS)}


def _books_demandes(brut):
    if not brut:
        return None
    out = set()
    for m in (x.strip().lower() for x in brut.split(",")):
        if m:
            out.update(_ALIAS.get(m, (m,)))
    return out or None


def _fractions(brut: str) -> list[float]:
    out = []
    for m in (x.strip() for x in str(brut).split(",")):
        if not m:
            continue
        if "/" in m:                       # « 1/4 » accepté autant que « 0.25 »
            n, d = m.split("/", 1)
            f = float(n) / float(d)
        else:
            f = float(m)
        if not 0 < f <= 1:
            raise SystemExit(f"Fraction de Kelly hors de ]0 ; 1] : {m}")
        out.append(f)
    if not out:
        raise SystemExit("--fractions : au moins une fraction.")
    return out


def _nom_fraction(f: float) -> str:
    for d in (1, 2, 3, 4, 5, 8, 10):
        if abs(f * d - round(f * d)) < 1e-9 and round(f * d) == 1:
            return "Kelly" if d == 1 else f"Kelly 1/{d}"
    return f"Kelly {f:g}"


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


def _sous_le_sommet(serie, dates) -> tuple[int, str, str]:
    """La plus longue période passée SOUS un sommet déjà atteint : (jours,
    date du sommet, date de retour au sommet ou « en cours »).

    Le creux dit COMBIEN on perd ; cette durée dit COMBIEN DE TEMPS on reste
    sous son meilleur niveau — ce qui use le moral bien plus que le montant.
    Le point de départ est 0 €, avant le premier pari."""
    pire = (0, "", "")
    pic, date_pic, sous = 0.0, (dates[0] if dates else ""), False
    for v, d in zip(serie, dates):
        if v < pic:
            sous = True
            continue
        if sous:
            duree = _jours(date_pic, d)
            if duree > pire[0]:
                pire = (duree, date_pic, d)
        pic, date_pic, sous = v, d, False
    if sous and dates:
        duree = _jours(date_pic, dates[-1])
        if duree > pire[0]:
            pire = (duree, date_pic, "en cours")
    return pire


def _e(v: float) -> str:
    """Un montant en euros, milliers séparés par une espace : « 11 180 »."""
    return f"{v:,.0f}".replace(",", " ")


def _jours(a: str, b: str) -> int:
    try:
        return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days
    except ValueError:
        return 0


def mesures(courbe, dates, mises, gains, bankroll) -> dict:
    """Les chiffres d'un schéma. Fonction pure : testée sans base."""
    total = sum(mises)
    pl = sum(gains)
    dd, sommet, pic_du_creux = _drawdown(courbe)
    moy = st.mean(mises) if mises else 0.0
    duree, depuis, jusqu = _sous_le_sommet(courbe, dates)
    return {
        "mise_moy": moy, "mise_max": max(mises) if mises else 0.0,
        "total": total, "pnl": pl,
        "roi": 100.0 * pl / total if total else 0.0,
        "dd": dd, "dd_pct": 100.0 * dd / bankroll if bankroll else 0.0,
        "dd_mises": dd / moy if moy else 0.0,
        # P&L par euro de pire creux. Indépendant du niveau de mise : doubler
        # toutes les mises double le P&L ET le creux.
        "pnl_par_creux": (pl / dd) if dd else None,
        "sommet": sommet, "pic_du_creux": pic_du_creux,
        "sous_sommet_jours": duree, "sous_sommet_depuis": depuis,
        "sous_sommet_jusqu": jusqu,
    }


def _requete(joues: bool) -> str:
    """Les paris RÉGLABLES (avec résultat). `--joues` : seulement ceux cliqués
    sur « Jouer », à la COTE DU CLIC — celle qui a réellement été prise."""
    if joues:
        return """
        SELECT vb.id, vb.book, vb.market, vb.outcome_label, vb.line,
               COALESCE(pb.odd_taken, vb.odd_taken) AS odd_taken,
               vb.fair_odd, vb.ev_pct, vb.kelly_pct, vb.detected_at,
               e.sport AS sport, e.league AS league,
               e.home AS home, e.away AS away, e.start_time AS start_time,
               r.winner, r.home_score, r.away_score
        FROM played_bets pb
        JOIN value_bets vb  ON vb.id = pb.value_bet_id
        JOIN results r      ON r.event_key = vb.event_key
        LEFT JOIN events e  ON e.event_key = vb.event_key
        """
    return """
        SELECT vb.id, vb.book, vb.market, vb.outcome_label, vb.line,
               vb.odd_taken, vb.fair_odd, vb.ev_pct, vb.kelly_pct, vb.detected_at,
               e.sport AS sport, e.league AS league,
               e.home AS home, e.away AS away, e.start_time AS start_time,
               r.winner, r.home_score, r.away_score
        FROM value_bets vb
        JOIN results r      ON r.event_key = vb.event_key
        LEFT JOIN events e  ON e.event_key = vb.event_key
    """


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--joues", action="store_true",
                    help="Seulement tes paris JOUÉS (clics « Jouer »), à la "
                         "cote du clic.")
    ap.add_argument("--premium", action="store_true",
                    help="Filtrer par la porte RÉELLE du canal premium.")
    ap.add_argument("--canal", default=None, metavar="NOM")
    ap.add_argument("--books", default=None, metavar="LISTE",
                    help="Books séparés par des virgules. Alias : kambi.")
    ap.add_argument("--depuis", default=None, metavar="AAAA-MM-JJ",
                    help="Coups d'envoi à partir de ce jour inclus.")
    ap.add_argument("--jusqu-a", default=None, metavar="AAAA-MM-JJ", dest="jusqu_a",
                    help="Coups d'envoi jusqu'à ce jour inclus.")
    ap.add_argument("--mise", type=float, default=35.0, metavar="EUR",
                    help="Mise du schéma fixe (défaut 35).")
    ap.add_argument("--fractions", default="0.25", metavar="LISTE",
                    help="Fractions de Kelly, ex. « 0.25,0.5 » ou « 1/4,1/2 » "
                         "(défaut 0.25).")
    ap.add_argument("--bankroll", type=float, default=None, metavar="EUR",
                    help="Capital pour le Kelly. Défaut : TELEGRAM_BANKROLL "
                         "de ton .env.")
    ap.add_argument("--out", default=None, metavar="CSV",
                    help="Écrire la courbe (un point par pari réglé).")
    a = ap.parse_args(argv)
    if a.canal:
        a.premium = True
    fractions = _fractions(a.fractions)
    load_env_file()   # AVANT d'importer alerter : ses réglages sont lus à l'import

    from src.alerter import (_MAX_STAKE_PCT, _STAKE_BASE_EUR, _STAKE_EV_MULT,
                             _STAKE_EV_TIER, _STAKE_MODE, _STAKE_PCT,
                             _advised_stake_eur, _round_stake)

    bankroll = a.bankroll if a.bankroll is not None else float(
        os.getenv("TELEGRAM_BANKROLL", "1000"))

    if a.premium:
        from scripts.pnl_detections import porte_de_canal
        porte, porte_desc = porte_de_canal(a.db, a.canal)
    else:
        porte, porte_desc = None, "aucune — toutes les détections"
    if a.joues:
        porte_desc = "tes paris JOUÉS (clics « Jouer »)" + (
            f", et porte {porte_desc}" if a.premium else "")
    books = _books_demandes(a.books)

    con = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = list(con.execute(_requete(a.joues)))
    if not rows:
        raise SystemExit("Aucun pari avec résultat"
                         + (" parmi tes paris joués" if a.joues else "")
                         + ". Lance `results-update`.")

    gardees = [r for r in rows
               if (books is None or (r["book"] or "").lower() in books)
               and (porte is None or porte(r))
               and (not a.depuis or (r["start_time"] or "")[:10] >= a.depuis)
               and (not a.jusqu_a or (r["start_time"] or "")[:10] <= a.jusqu_a)]

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

    regles = []
    for r in opp:
        statut = clv_settle(r["market"], r["outcome_label"], r["line"],
                            r["winner"], r["home_score"], r["away_score"])
        if clv_pnl(statut, float(r["odd_taken"]), 1.0) is None:
            continue          # résultat insuffisant pour ce marché
        regles.append((r, statut))
    if not regles:
        raise SystemExit("Aucun pari réglé dans cette population.")

    def kelly_brut(r, f):
        """La mise Kelly de production, plafonnée, NON arrondie."""
        try:
            juste = float(r["fair_odd"])
            cote = float(r["odd_taken"])
        except (TypeError, ValueError):
            return 0.0
        if juste <= 1.0 or cote <= 1.0:
            return 0.0
        brute = kelly_stake(cote, 1.0 / juste, bankroll, f)
        return min(brute, bankroll * _MAX_STAKE_PCT / 100.0)

    # (libellé, slug CSV, mise de chaque pari réglé, dans l'ordre)
    schemas = [(f"fixe {a.mise:g} €", "fixe", [_round_stake(a.mise)] * len(regles))]
    for f in fractions:
        brutes = [kelly_brut(r, f) for r, _s in regles]
        nom, slug = _nom_fraction(f), f"kelly_{f:g}"
        schemas.append((nom, slug, [_round_stake(m) for m in brutes]))
        moy = st.mean(brutes) if brutes else 0.0
        if moy > 0:
            echelle = a.mise / moy
            schemas.append((f"{nom} à {a.mise:g} € moy.", f"{slug}_egal",
                            [round(m * echelle, 2) for m in brutes]))
    schemas.append(("actuel (.env)", "actuel",
                    [_advised_stake_eur(float(r["ev_pct"]), r["kelly_pct"], bankroll) or 0.0
                     for r, _s in regles]))

    dates = [(r["start_time"] or "")[:10] for r, _s in regles]
    resultats, points = {}, []
    for lib, slug, mises in schemas:
        cumul, courbe, gains = 0.0, [], []
        for (r, statut), m in zip(regles, mises):
            g = clv_pnl(statut, float(r["odd_taken"]), m) or 0.0
            cumul += g
            gains.append(g)
            courbe.append(cumul)
        resultats[slug] = (lib, mesures(courbe, dates, mises, gains, bankroll), courbe, mises)
    for i, (r, statut) in enumerate(regles):
        pt = {"n": i + 1, "date": dates[i], "sport": r["sport"] or "?",
              "book": r["book"], "cote": float(r["odd_taken"]),
              "ev_pct": round(float(r["ev_pct"]), 2), "statut": statut}
        for slug, (_lib, _m, courbe, mises) in resultats.items():
            pt[f"mise_{slug}"] = round(mises[i], 2)
            pt[f"cumul_{slug}"] = round(courbe[i], 2)
        points.append(pt)

    print(f"\nSCHÉMAS DE MISE — population : {porte_desc}")
    print(f"Books : {', '.join(sorted(books)) if books else 'tous'}"
          f"   ·   bankroll Kelly {bankroll:.0f} € (fixe, sans composition)"
          f"   ·   {len(regles)} paris réglés, du {dates[0]} au {dates[-1]}")
    print(f"Réglages lus dans ton .env : STAKE_MODE={_STAKE_MODE}, "
          f"STAKE_PCT={_STAKE_PCT:g}, STAKE_BASE_EUR={_STAKE_BASE_EUR:g}, "
          f"STAKE_EV_TIER={_STAKE_EV_TIER:g}, STAKE_EV_MULT={_STAKE_EV_MULT:g}, "
          f"plafond Kelly={_MAX_STAKE_PCT:g} %\n")

    largeur = max(len(lib) for lib, _s, _m in schemas) + 2
    print("CE QUE ÇA ENGAGE ET RAPPORTE")
    e = (f"{'schéma':{largeur}}{'mise moy':>10}{'mise max':>10}{'total misé':>13}"
         f"{'P&L':>11}{'ROI':>9}")
    print(e)
    print("-" * len(e))
    for slug, (lib, m, _c, _mi) in resultats.items():
        print(f"{lib:{largeur}}{m['mise_moy']:9.1f}€{_e(m['mise_max']):>9}€"
              f"{_e(m['total']):>12}€{('+' if m['pnl'] >= 0 else '-') + _e(abs(m['pnl'])):>10}€"
              f"{m['roi']:+8.2f}%")

    print("\nCE QU'IL FAUT ENCAISSER — les creux")
    e = (f"{'schéma':{largeur}}{'creux max':>11}{'% bankroll':>12}{'en mises':>10}"
         f"{'P&L/creux':>11}   {'plus long sous le sommet'}")
    print(e)
    print("-" * len(e))
    for slug, (lib, m, _c, _mi) in resultats.items():
        ratio = "—" if m["pnl_par_creux"] is None else f"{m['pnl_par_creux']:.2f}"
        duree = (f"{m['sous_sommet_jours']} j ({m['sous_sommet_depuis']} → "
                 f"{m['sous_sommet_jusqu']})") if m["sous_sommet_jours"] else "—"
        print(f"{lib:{largeur}}{'-' + _e(m['dd']):>10}€{m['dd_pct']:11.1f}%"
              f"{m['dd_mises']:10.1f}{ratio:>11}   {duree}")

    fixe = resultats["fixe"][1]
    print()
    for f in fractions:
        egal = resultats.get(f"kelly_{f:g}_egal")
        if egal is None:
            continue
        lib, m = egal[0], egal[1]
        if m["dd"] and fixe["dd"]:
            ecart = 100.0 * (m["dd"] - fixe["dd"]) / fixe["dd"]
            if abs(ecart) < 1:
                verdict = "est le MÊME que celui"
            else:
                verdict = (f"est {abs(ecart):.0f} % "
                           f"{'PLUS FAIBLE' if ecart < 0 else 'PLUS FORT'} que celui")
            print(f"→ À mise moyenne égale ({a.mise:g} €), le pire creux de "
                  f"{_nom_fraction(f)} {verdict} de la mise fixe "
                  f"(-{_e(m['dd'])} € contre -{_e(fixe['dd'])} €).")
    print("\n⚠️ Comparer les P&L ou les creux en euros entre schémas qui ne misent pas "
          "autant\n   ne dit rien : Kelly à 3 % d'une grosse bankroll mise 4 à 5 fois 35 €, "
          "il gagne\n   ET creuse 4 à 5 fois plus. Ce qui compare : « P&L/creux », "
          "« en mises », et les\n   lignes « à … € moy. », qui engagent le même capital "
          "que la mise fixe.")
    print("⚠️ Bankroll FIXE, sans composition. Courbe ordonnée par coup d'envoi.")
    if a.joues:
        print("⚠️ Paris joués : la cote est celle du CLIC, la cote juste celle de la "
              "détection.\n   Les paris sans résultat exploitable (match non réglé, "
              "marché non réglable)\n   sont hors du calcul.")

    if a.out:
        champs = list(points[0].keys())
        with open(a.out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=champs)
            w.writeheader()
            w.writerows(points)
        print(f"\n✓ Courbe écrite : {a.out}  ({len(points)} points, "
              f"{len(champs)} colonnes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
