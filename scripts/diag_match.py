"""Diagnostic d'un match suspect : alertes, cotes stockées par book, flux live.

Née d'une alerte Vivatbet « Faroe Islands vs Slovakia » à l'EV aberrante, où
les cotes semblaient inversées. Répond en une commande aux trois questions qui
tranchent entre les causes possibles :

  1. **les alertes** du book sur ce match : issue, cote prise, cote juste, EV ;
  2. **les dernières cotes 1X2 stockées**, book par book, dans le repère de
     Pinnacle (après réalignement) — si le book dit l'inverse des autres, le
     retournement home/away s'est trompé ;
  3. **ce que le book sert EN CE MOMENT** (Vivatbet seulement) : l'ordre des
     équipes et les cotes par type, pour voir si l'inversion vient du flux.

Lecture seule : aucune écriture en base.

    .venv/bin/python -m scripts.diag_match faro slova
    .venv/bin/python -m scripts.diag_match faro slova --book vivatbet
    .venv/bin/python -m scripts.diag_match slova slova   # un seul nom : toutes
                                                         # les orthographes de l'autre
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone

BOOKS_TEMOINS = ("pinnacle", "golden_palace", "unibet_be", "magicbetting",
                 "ladbrokes_be", "starcasino_sport")


def _alertes(con, book: str, t1: str, t2: str) -> None:
    # TOUS les books : l'alerte suspecte ne vient pas forcément de celui qu'on
    # croit, et c'est justement ce qu'il faut pouvoir voir.
    print("== Détections sur ce match (tous books ; * = le book étudié) ==")
    rows = con.execute(
        "SELECT id, book, event_key, market, outcome_label, line, odd_taken, "
        "round(fair_odd, 2), round(ev_pct, 1), substr(detected_at, 1, 16) "
        "FROM value_bets WHERE event_key LIKE ? AND event_key LIKE ? "
        "ORDER BY id DESC LIMIT 15", (f"%{t1}%", f"%{t2}%")).fetchall()
    for r in rows:
        print("  ", "*" if r[1] == book else " ", r)
    if not rows:
        print("   (aucune)")
    envoyees = con.execute(
        "SELECT book, event_key, market, outcome_label, line, round(ev_pct, 1), "
        "substr(notified_at, 1, 16) FROM notified_value_bets "
        "WHERE event_key LIKE ? AND event_key LIKE ? ORDER BY id DESC LIMIT 10",
        (f"%{t1}%", f"%{t2}%")).fetchall()
    print("\n== Alertes réellement envoyées sur Telegram ==")
    for r in envoyees:
        print("   ", r)
    if not envoyees:
        print("   (aucune)")


def _cotes(con, book: str, t1: str, t2: str) -> None:
    print("\n== Dernières cotes 1X2 stockées (repère Pinnacle) ==")
    cles = [r[0] for r in con.execute(
        "SELECT DISTINCT event_key FROM quotes WHERE event_key LIKE ? AND event_key LIKE ?",
        (f"%{t1}%", f"%{t2}%"))]
    if not cles:
        print("   (aucune cote stockée pour ce match)")
    for k in cles:
        print("  clé :", k)
        for b in (book,) + tuple(x for x in BOOKS_TEMOINS if x != book):
            rows = con.execute(
                "SELECT outcome_label, decimal_odd FROM quotes WHERE event_key = ? "
                "AND book = ? AND market = 'h2h' AND fetched_at = (SELECT max(fetched_at) "
                "FROM quotes WHERE event_key = ? AND book = ? AND market = 'h2h')",
                (k, b, k, b)).fetchall()
            if rows:
                print(f"    {b:16s}", dict(rows))


def _flux_vivatbet(t1: str, t2: str) -> None:
    print("\n== Ce que Vivatbet sert en ce moment ==")
    try:
        from src.scrapers.vivatbet import VivatbetScraper
        trouves: dict = {}

        def chercher(lot: list) -> None:
            for g in lot or []:
                n1 = (g.get("opponent1") or {}).get("fullNameEng") or ""
                n2 = (g.get("opponent2") or {}).get("fullNameEng") or ""
                nom = f"{n1} {n2}".lower()
                if t1 in nom and t2 in nom:
                    trouves[g["id"]] = (g, n1, n2)

        with VivatbetScraper() as sc:
            chercher(sc.fetch_top("soccer"))
            if not trouves:
                for lid, _nom, _n in sc.fetch_leagues("soccer"):
                    chercher(sc.fetch_league("soccer", lid))
                    if trouves:
                        break
        for gid, (g, n1, n2) in trouves.items():
            debut = datetime.fromtimestamp(g["startTs"], timezone.utc)
            cotes = [(ev.get("type"), ev.get("cf")) for eg in g.get("eventGroups") or []
                     if eg.get("groupId") == 1 for col in eg.get("events") or [] for ev in col]
            print(f"   {gid} {n1} vs {n2} · {debut:%d/%m %H:%M} UTC · "
                  f"ligue {(g.get('liga') or {}).get('nameEng')}")
            print(f"     1X2 (type 1 = {n1}, 2 = nul, 3 = {n2}) : {cotes}")
            print(f"     homeAwayFlag={g.get('homeAwayFlag')!r} dopInfo={g.get('dopInfo')!r} "
                  f"kind={g.get('kind')!r}")
        if not trouves:
            print("   (absent du flux : déjà commencé, ou dans une compétition tronquée)")
    except Exception as e:                                        # noqa: BLE001
        print("   flux injoignable :", e)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Diagnostic d'un match suspect : alertes du book, dernières "
                    "cotes 1X2 stockées par book (repère Pinnacle), et flux live "
                    "de Vivatbet. Lecture seule.")
    ap.add_argument("equipe1", help="fragment du nom de la 1re équipe, ex. faro")
    ap.add_argument("equipe2", help="fragment du nom de la 2e équipe, ex. slova")
    ap.add_argument("--book", default="vivatbet", help="book alerté (défaut : vivatbet)")
    ap.add_argument("--db", default="data/valuebet.db")
    args = ap.parse_args(argv)
    t1, t2 = args.equipe1.lower(), args.equipe2.lower()
    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    _alertes(con, args.book, t1, t2)
    _cotes(con, args.book, t1, t2)
    if args.book == "vivatbet":
        _flux_vivatbet(t1, t2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
