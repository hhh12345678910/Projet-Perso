#!/usr/bin/env python3
"""Exporte TOUS les matchs détectés qui n'ont pas de résultat en base.

Un fichier CSV, un match par ligne, trié par coup d'envoi :

    date_utc, sport, ligue, domicile, exterieur, detections, paris_joues, event_key

La population est celle de `results-update` et de l'Analytics : tout match
qui porte au moins une détection (value bet), joué ou non — pas seulement les
clics « Jouer ». Seuls les matchs commencés depuis plus de trois heures sont
listés : avant, un résultat absent est normal.

Un même match peut exister sous plusieurs clés (heure révisée, équipes dans
l'autre ordre au tennis). Il n'est listé qu'UNE fois, avec toutes ses clés
dans la dernière colonne, et seulement si AUCUNE de ses clés n'a de résultat
(même sport, mêmes équipes, à moins de 26 h).

⚠️ LECTURE SEULE. Rien n'est écrit en base ; seul le CSV est créé.

    .venv/bin/python -m scripts.export_sans_resultat
    .venv/bin/python -m scripts.export_sans_resultat --depuis 2026-08-01 --sortie ~/x.csv
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

GRACE = timedelta(hours=3)
MEME_MATCH = timedelta(hours=26)
COLONNES = ["date_utc", "sport", "ligue", "domicile", "exterieur",
            "detections", "paris_joues", "event_key"]


def _norm(nom: "str | None") -> str:
    return re.sub(r"[^a-z0-9]", "", (nom or "").lower())


def _instant(brut: str) -> datetime:
    dt = datetime.fromisoformat(str(brut).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def matchs_sans_resultat(db: str, maintenant: datetime,
                         depuis: "date | None" = None) -> list[dict]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    try:
        fin = (maintenant - GRACE).isoformat()
        debut = (datetime.combine(depuis, datetime.min.time(), tzinfo=timezone.utc)
                 .isoformat() if depuis else "")
        lignes = con.execute(
            "SELECT e.event_key, e.sport, e.league, e.home, e.away, e.start_time, "
            "       (SELECT COUNT(*) FROM value_bets v WHERE v.event_key = e.event_key) "
            "FROM events e "
            "WHERE e.start_time < ? AND e.start_time >= ? "
            "  AND EXISTS (SELECT 1 FROM value_bets v WHERE v.event_key = e.event_key)",
            (fin, debut)).fetchall()
        joues = dict(con.execute(
            "SELECT event_key, COUNT(*) FROM played_bets "
            "WHERE event_key IS NOT NULL GROUP BY event_key"))
        regles_cles = {k for (k,) in con.execute("SELECT event_key FROM results")}
    finally:
        con.close()

    # Les matchs réglés, par sport et paire d'équipes (sans ordre) : une autre
    # clé du même match, réglée, suffit à l'écarter de la liste.
    regles = defaultdict(list)
    for k, sp, _lg, h, a, t, _d in lignes:
        if k in regles_cles:
            regles[(sp, frozenset((_norm(h), _norm(a))))].append(_instant(t))

    groupes: dict = {}
    for k, sp, lg, h, a, t, det in lignes:
        if k in regles_cles:
            continue
        paire, quand = frozenset((_norm(h), _norm(a))), _instant(t)
        if any(abs(quand - x) <= MEME_MATCH for x in regles[(sp, paire)]):
            continue
        g = groupes.setdefault((sp, paire, quand.date()), {
            "quand": quand, "sport": sp, "ligue": lg, "domicile": h,
            "exterieur": a, "detections": 0, "paris_joues": 0, "cles": []})
        if quand < g["quand"]:
            g.update(quand=quand, domicile=h, exterieur=a)
        g["ligue"] = g["ligue"] or lg
        g["detections"] += det
        g["paris_joues"] += joues.get(k, 0)
        g["cles"].append(k)
    return sorted(groupes.values(), key=lambda g: (g["quand"], g["sport"]))


def ecrire(matchs: list[dict], sortie: Path) -> None:
    with open(sortie, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(COLONNES)
        for g in matchs:
            w.writerow([g["quand"].strftime("%Y-%m-%d %H:%M"), g["sport"],
                        g["ligue"], g["domicile"], g["exterieur"],
                        g["detections"], g["paris_joues"], " ".join(sorted(g["cles"]))])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--sortie", default=str(Path.home() / "matchs_sans_resultat.csv"))
    ap.add_argument("--depuis", default=None, metavar="AAAA-MM-JJ",
                    help="seulement les matchs à partir de cette date (UTC)")
    a = ap.parse_args(argv)
    depuis = date.fromisoformat(a.depuis) if a.depuis else None
    maintenant = datetime.now(timezone.utc)
    matchs = matchs_sans_resultat(a.db, maintenant, depuis)
    sortie = Path(a.sortie).expanduser()
    ecrire(matchs, sortie)
    print(f"{len(matchs)} matchs sans résultat, commencés avant "
          f"{(maintenant - GRACE):%d/%m %H:%M} UTC → {sortie}")
    par_sport = Counter(g["sport"] for g in matchs)
    joues = Counter(g["sport"] for g in matchs if g["paris_joues"])
    for sport, n in par_sport.most_common():
        print(f"  {sport:12} {n:7}   dont {joues[sport]} avec un pari joué")
    return 0


if __name__ == "__main__":
    sys.exit(main())
