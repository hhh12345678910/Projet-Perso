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
* **paliers EV**      — une mise RONDE par bande d'EV (celles du projet :
                        <5, 5-8, 8-15, 15-35, 35+), jamais influencée par la
                        cote. « auto » : proportionnelle à l'EV médiane de la
                        bande, bornée entre ½ et 2 fois `--mise`, moyenne
                        ramenée à `--mise` — calculée sur les EV seules, JAMAIS
                        sur les résultats, donc pas ajustée au passé.
                        `--paliers 0:25,8:35,15:50` teste des montants précis ;
                        c'est le format de `STAKE_EV_PALIERS` dans `.env`.
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


def _bandes_ev() -> list[tuple[float, float | None]]:
    """Les bandes d'EV du projet (`main._EV_BUCKET_ORDER`), en bornes :
    (bas inclus, haut exclu). Relues, jamais recopiées."""
    from src.main import _EV_BUCKET_ORDER
    out = []
    for lab in _EV_BUCKET_ORDER:
        t = lab.replace("%", "").strip()
        if t.startswith("<"):
            out.append((0.0, float(t[1:])))
        elif t.endswith("+"):
            out.append((float(t[:-1]), None))
        else:
            lo, hi = t.split("-")
            out.append((float(lo), float(hi)))
    return out


def paliers_auto(evs, mise: float, arrondi) -> list[tuple[float, float]]:
    """Une mise par bande d'EV, PROPORTIONNELLE à l'EV médiane de la bande
    (c'est le numérateur de Kelly, sans son terme de cote), bornée entre ½ et
    2 fois `mise`, puis ramenée pour que la MOYENNE sur ces paris soit `mise`,
    enfin arrondie comme en production.

    ⚠️ Ne lit que les EV, JAMAIS les résultats : rien n'est ajusté à ce qui a
    gagné. Le plafond à 2× protège la bande 35 %+, où l'EV extrême est plus
    souvent une erreur de référence qu'un cadeau du book."""
    bandes = []
    for lo, hi in _bandes_ev():
        dedans = [e for e in evs if e >= lo and (hi is None or e < hi)]
        if dedans:
            bandes.append((lo, st.median(dedans), len(dedans)))
    if not bandes:
        return []
    n = sum(k for _lo, _med, k in bandes)
    bas, haut = 0.5 * mise, 2.0 * mise
    echelle = mise / (sum(med * k for _lo, med, k in bandes) / n)
    for _ in range(50):       # bornes + moyenne : quelques itérations suffisent
        brutes = [min(haut, max(bas, med * echelle)) for _lo, med, _k in bandes]
        moy = sum(b * k for b, (_lo, _m, k) in zip(brutes, bandes)) / n
        if abs(moy - mise) < 1e-6:
            break
        echelle *= mise / moy
    return [(lo, arrondi(b)) for (lo, _m, _k), b in zip(bandes, brutes)]


def decrire_paliers(paliers) -> str:
    morceaux = []
    for i, (seuil, m) in enumerate(paliers):
        suivant = paliers[i + 1][0] if i + 1 < len(paliers) else None
        borne = (f"{seuil:g}-{suivant:g} %" if suivant is not None else f"≥ {seuil:g} %")
        if i == 0:
            borne = f"< {suivant:g} %" if suivant is not None else "toute EV"
        morceaux.append(f"{borne} → {m:g} €")
    return " · ".join(morceaux)


def test_apparie(gains_schema, mises_schema, gains_fixe, mises_fixe) -> dict:
    """Le schéma bat-il VRAIMENT la mise fixe, ou est-ce la chance ?

    Test APPARIÉ : les deux schémas portent sur les MÊMES paris, donc la
    chance des résultats est commune ; seule compte la différence pari par
    pari. Le schéma est d'abord ramené au même capital total que la mise
    fixe — sinon il « gagnerait » juste en misant plus.

    Rend l'écart de P&L à capital égal, son erreur-type et t = écart/erreur.
    |t| ≥ 2 : environ 5 % de chances que l'écart soit du hasard ; sous 2, la
    période ne suffit pas à le prouver."""
    tf, ts = sum(mises_fixe), sum(mises_schema)
    if not tf or not ts or len(gains_fixe) < 2:
        return {"ecart": None, "erreur": None, "t": None}
    k = tf / ts
    d = [gs * k - gf for gs, gf in zip(gains_schema, gains_fixe)]
    n = len(d)
    erreur = st.stdev(d) * n ** 0.5
    ecart = sum(d)
    return {"ecart": ecart, "erreur": erreur, "t": (ecart / erreur) if erreur else None}


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
               r.winner, r.home_score, r.away_score,
               (SELECT cs.fair_odd FROM clv_snapshots cs WHERE cs.value_bet_id = vb.id
                 AND cs.closing = 1 AND cs.fair_odd > 0 ORDER BY cs.id DESC LIMIT 1)
                 AS cloture
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
               r.winner, r.home_score, r.away_score,
               (SELECT cs.fair_odd FROM clv_snapshots cs WHERE cs.value_bet_id = vb.id
                 AND cs.closing = 1 AND cs.fair_odd > 0 ORDER BY cs.id DESC LIMIT 1)
                 AS cloture
        FROM value_bets vb
        JOIN results r      ON r.event_key = vb.event_key
        LEFT JOIN events e  ON e.event_key = vb.event_key
    """


def par_periode(gains, mises, dates, debuts) -> list[dict]:
    """P&L, ROI et creux de chaque période [début ; début suivant[. Le creux
    est mesuré DANS la période, à partir de 0 € à son début."""
    bornes = sorted(debuts)
    out = []
    for i, d0 in enumerate(bornes):
        d1 = bornes[i + 1] if i + 1 < len(bornes) else None
        idx = [j for j, d in enumerate(dates) if d >= d0 and (d1 is None or d < d1)]
        g = [gains[j] for j in idx]
        m = [mises[j] for j in idx]
        courbe, cumul = [], 0.0
        for x in g:
            cumul += x
            courbe.append(cumul)
        out.append({"debut": d0, "fin": d1, "n": len(idx), "pnl": sum(g),
                    "roi": 100 * sum(g) / sum(m) if sum(m) else None,
                    "creux": _drawdown(courbe)[0] if courbe else 0.0})
    return out


def _afficher_periodes(resultats, gains_par_slug, dates, brut) -> None:
    debuts = [x.strip() for x in brut.split(",") if x.strip()]
    print("\nPAR PÉRIODE — P&L · ROI · pire creux DANS la période")
    tranches = None
    for slug, (lib, _m, _c, mises) in resultats.items():
        lignes = par_periode(gains_par_slug[slug], mises, dates, debuts)
        if tranches is None:
            tranches = lignes
            tete = "".join(f"{(l['debut'][5:] + ' → ' + (l['fin'][5:] if l['fin'] else 'fin')):>27}"
                           for l in lignes)
            print(f"{'méthode':24}{tete}")
            print(f"{'(paris)':24}" + "".join(f"{l['n']:>27}" for l in lignes))
        cases = []
        for l in lignes:
            if not l["n"]:
                cases.append(f"{'—':>27}")
                continue
            pl = ("+" if l["pnl"] >= 0 else "-") + _e(abs(l["pnl"])) + "€"
            cases.append(f"{pl:>9} {l['roi']:+6.1f}% {'-' + _e(l['creux']) + '€':>9}")
        print(f"{lib:24}" + "".join(f"{c:>27}" for c in cases))


def _afficher_validation(regles, mise, arrondi, k, paliers_testes) -> None:
    from scripts.valider_mises import SEUILS, valider
    paris = [{"ev": float(r["ev_pct"]), "cote": float(r["odd_taken"]), "statut": s_,
              "clv": (100.0 * (float(r["odd_taken"]) / float(r["cloture"]) - 1)
                      if r["cloture"] else None)} for r, s_ in regles]
    fixes = {}
    if paliers_testes:
        from src.alerter import mise_palier
        fixes["paliers testés"] = tuple(mise_palier(s_ + 1e-9, paliers_testes) for s_ in SEUILS)
    res = valider(paris, mise, arrondi, k=k, regles_fixes=fixes)
    n_test = len(res["fixe"]["gains"])
    print(f"\nVALIDATION HORS ÉCHANTILLON — {k} blocs chronologiques ; chaque méthode est "
          f"choisie sur les\n   blocs PASSÉS et jugée sur le suivant ({n_test} paris jugés, "
          "jamais vus au moment du choix),\n   à capital égal à la mise fixe.")
    largeur = max(len(n) for n in res) + 2
    print(f"{'méthode':{largeur}}{'P&L':>10}{'ROI':>9}{'creux':>10}{'P&L/creux':>11}"
          f"{'t vs fixe':>11}   règle par bloc (<8 · 8-15 · 15-35 · ≥35 %)")
    gf, mf = res["fixe"]["gains"], res["fixe"]["mises"]
    for nom, r in res.items():
        courbe, cumul = [], 0.0
        for g in r["gains"]:
            cumul += g
            courbe.append(cumul)
        dd = _drawdown(courbe)[0]
        pl = sum(r["gains"])
        roi = 100 * pl / sum(r["mises"]) if sum(r["mises"]) else 0.0
        t = "—" if nom == "fixe" else (
            lambda x: "—" if x["t"] is None else f"{x['t']:.2f}")(
            test_apparie(r["gains"], r["mises"], gf, mf))
        regles_txt = "  |  ".join("/".join(f"{x:g}" for x in g) for g in r["regles"])
        print(f"{nom:{largeur}}{('+' if pl >= 0 else '-') + _e(abs(pl)):>9}€{roi:+8.2f}%"
              f"{'-' + _e(dd):>9}€{(pl / dd if dd else 0):11.2f}{t:>11}   {regles_txt}")
    print("   Seule la courbe HORS ÉCHANTILLON juge une méthode. Une méthode « optimisée » "
          "qui brille\n   sur tout l'historique mais pas ici a appris le bruit.")


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
    ap.add_argument("--paliers", default=None, metavar="LISTE",
                    help="Paliers d'EV à tester, « seuil:mise », ex. "
                         "« 0:25,8:35,15:50,35:60 » (format de STAKE_EV_PALIERS).")
    ap.add_argument("--bankroll", type=float, default=None, metavar="EUR",
                    help="Capital pour le Kelly. Défaut : TELEGRAM_BANKROLL "
                         "de ton .env.")
    ap.add_argument("--periodes", default=None, metavar="DATES",
                    help="Découper le résultat par période : dates de DÉBUT séparées "
                         "par des virgules, ex. 2026-06-27,2026-07-27,2026-08-27,2026-09-27.")
    ap.add_argument("--valider", type=int, nargs="?", const=4, default=None, metavar="K",
                    help="Validation HORS ÉCHANTILLON : choisir chaque méthode sur le "
                         "passé, la juger sur la période suivante (K blocs, défaut 4).")
    ap.add_argument("--out", default=None, metavar="CSV",
                    help="Écrire la courbe (un point par pari réglé).")
    a = ap.parse_args(argv)
    if a.canal:
        a.premium = True
    fractions = _fractions(a.fractions)
    load_env_file()   # AVANT d'importer alerter : ses réglages sont lus à l'import

    from src.alerter import (_MAX_STAKE_PCT, _STAKE_BASE_EUR, _STAKE_EV_MULT,
                             _STAKE_EV_PALIERS, _STAKE_EV_TIER, _STAKE_MODE,
                             _STAKE_PCT, _advised_stake_eur, _round_stake,
                             lire_paliers, mise_palier)
    try:
        paliers_testes = lire_paliers(a.paliers) if a.paliers else []
    except ValueError as e:
        raise SystemExit(f"--paliers : {e}")

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
    evs = [float(r["ev_pct"]) for r, _s in regles]
    auto = paliers_auto(evs, a.mise, _round_stake)
    if auto:
        schemas.append(("paliers EV auto", "paliers_auto",
                        [mise_palier(ev, auto) for ev in evs]))
    if paliers_testes:
        schemas.append(("paliers EV testés", "paliers_testes",
                        [mise_palier(ev, paliers_testes) for ev in evs]))
    schemas.append(("actuel (.env)", "actuel",
                    [_advised_stake_eur(float(r["ev_pct"]), r["kelly_pct"], bankroll) or 0.0
                     for r, _s in regles]))

    dates = [(r["start_time"] or "")[:10] for r, _s in regles]
    resultats, points, gains_par_slug = {}, [], {}
    for lib, slug, mises in schemas:
        cumul, courbe, gains = 0.0, [], []
        for (r, statut), m in zip(regles, mises):
            g = clv_pnl(statut, float(r["odd_taken"]), m) or 0.0
            cumul += g
            gains.append(g)
            courbe.append(cumul)
        resultats[slug] = (lib, mesures(courbe, dates, mises, gains, bankroll), courbe, mises)
        gains_par_slug[slug] = gains
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
         f"{'P&L':>11}{'ROI':>9}{'montants':>10}")
    print(e)
    print("-" * len(e))
    for slug, (lib, m, _c, _mi) in resultats.items():
        print(f"{lib:{largeur}}{m['mise_moy']:9.1f}€{_e(m['mise_max']):>9}€"
              f"{_e(m['total']):>12}€{('+' if m['pnl'] >= 0 else '-') + _e(abs(m['pnl'])):>10}€"
              f"{m['roi']:+8.2f}%{len(set(round(x, 2) for x in _mi)):>10}")

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

    print("\n« montants » : nombre de mises différentes. Peu de montants ronds = des mises "
          "qui ressemblent\n   à celles d'un parieur ordinaire ; Kelly en produit des "
          "dizaines, corrélées à l'EV.")
    for titre, pal in (("auto", auto), ("testés", paliers_testes),
                       ("actuels (.env)", _STAKE_EV_PALIERS if _STAKE_MODE == "flat" else [])):
        if pal:
            print(f"\nPaliers {titre} : {decrire_paliers(pal)}")
            print(f"   dans .env : STAKE_EV_PALIERS={','.join(f'{x:g}:{y:g}' for x, y in pal)}")
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
    for slug, nom in (("paliers_auto", "les paliers EV auto"),
                      ("paliers_testes", "les paliers EV testés")):
        if slug not in resultats or not fixe["dd"]:
            continue
        m = resultats[slug][1]
        print(f"→ Avec {nom} (mise moyenne {m['mise_moy']:.1f} €) : P&L/creux "
              f"{'—' if m['pnl_par_creux'] is None else format(m['pnl_par_creux'], '.2f')} "
              f"contre {fixe['pnl_par_creux']:.2f} en fixe, pire creux "
              f"{m['dd_mises']:.1f} mises contre {fixe['dd_mises']:.1f}.")
    print("\nEST-CE PROUVÉ ? — test apparié contre la mise fixe, à capital égal")
    print(f"{'schéma':{largeur}}{'écart P&L':>12}{'± (1 σ)':>11}{'t':>7}   verdict")
    gf, mf = gains_par_slug["fixe"], resultats["fixe"][3]
    for slug, (lib, _m, _c, mises) in resultats.items():
        if slug == "fixe":
            continue
        t = test_apparie(gains_par_slug[slug], mises, gf, mf)
        if t["t"] is None:
            continue
        verdict = ("prouvé (|t| ≥ 3)" if abs(t["t"]) >= 3 else
                   "probable (2 ≤ |t| < 3)" if abs(t["t"]) >= 2 else
                   "pas prouvé sur cette période")
        print(f"{lib:{largeur}}{('+' if t['ecart'] >= 0 else '-') + _e(abs(t['ecart'])):>11}€"
              f"{_e(t['erreur']):>10}€{t['t']:7.2f}   {verdict}")
    print("   ⚠️ Plusieurs schémas sont comparés : le meilleur d'entre eux profite d'un peu de "
          "chance\n   par sélection. Exiger |t| ≥ 2, et de préférence ≥ 3.")

    print("\nLE FONDEMENT — CLV et ROI par tranche d'EV (mise fixe)")
    print("Si la CLV MONTE avec l'EV, miser plus sur les grosses EV est justifié même quand "
          "le ROI,\n   bien plus bruité, ne suffit pas encore à le prouver.")
    print(f"{'EV':>10}{'paris':>8}{'ROI':>9}{'± (1 σ)':>10}{'n CLV':>8}{'CLV moy':>10}{'± (1 σ)':>9}")
    for lo, hi in _bandes_ev():
        lot = [(r, s_) for r, s_ in regles
               if float(r["ev_pct"]) >= lo and (hi is None or float(r["ev_pct"]) < hi)]
        if not lot:
            continue
        rend = [clv_pnl(s_, float(r["odd_taken"]), 1.0) for r, s_ in lot]
        clvs = [100.0 * (float(r["odd_taken"]) / float(r["cloture"]) - 1)
                for r, _s in lot if r["cloture"]]
        nom = f"{lo:g}-{hi:g} %" if hi is not None else f"≥ {lo:g} %"
        roi = 100 * st.mean(rend)
        roi_e = 100 * st.stdev(rend) / len(rend) ** 0.5 if len(rend) > 1 else 0.0
        clv_txt = (f"{len(clvs):8d}{st.mean(clvs):+9.2f}%"
                   f"{(st.stdev(clvs) / len(clvs) ** 0.5 if len(clvs) > 1 else 0.0):8.2f}%"
                   if clvs else f"{0:8d}{'—':>10}{'—':>9}")
        print(f"{nom:>10}{len(lot):8d}{roi:+8.2f}%{roi_e:9.2f}%{clv_txt}")

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

    if a.periodes:
        _afficher_periodes(resultats, gains_par_slug, dates, a.periodes)

    if a.valider:
        _afficher_validation(regles, a.mise, _round_stake, a.valider, paliers_testes)

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
