"""Importe les résultats de football retrouvés à la main sur le web.

Chaque ligne de `scripts/resultats_web.csv` est un score à 90 minutes confirmé
par DEUX sources indépendantes (URL dans le fichier, pour la trace). Le script
retrouve le match dans nos paris joués, puis écrit le résultat avec la source
`manuel-web` :

* SIMULATION par défaut — rien n'est écrit sans `--ecrire` ;
* jamais d'écrasement : un match qui a déjà un résultat est laissé tel quel ;
* une ligne qui ne désigne pas exactement UN match est refusée (aucun match,
  ou plusieurs clés possibles) — mieux vaut un pari sans résultat qu'un faux ;
* football uniquement : le vainqueur se déduit du score.

    .venv/bin/python -m scripts.import_resultats_web            # simulation
    .venv/bin/python -m scripts.import_resultats_web --ecrire   # écrit
    .venv/bin/python -m src.main track-update                   # puis toujours
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SOURCE = "manuel-web"
FICHIER = Path(__file__).with_name("resultats_web.csv")
TOLERANCE = timedelta(hours=26)


def _instant(brut) -> "datetime | None":
    if not brut:
        return None
    try:
        dt = datetime.fromisoformat(str(brut).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _instant_cle(cle: str) -> "datetime | None":
    try:
        return datetime.strptime(cle.split("::", 1)[0], "%Y%m%d%H%M").replace(
            tzinfo=timezone.utc)
    except ValueError:
        return None


def vainqueur(dom: int, ext: int) -> str:
    return "home" if dom > ext else "away" if ext > dom else "draw"


def candidats(con, quand: datetime, dom: str, ext: str) -> list:
    """Les clés de match JOUÉES qui collent : noms (préfixes de la clé, comme
    la liste les tronque) dans le bon sens et coup d'envoi à ±26 h."""
    trouves = {}
    for r in con.execute(
            "SELECT DISTINCT pb.event_key AS k, e.sport AS sport, "
            "e.start_time AS debut FROM played_bets pb "
            "LEFT JOIN events e ON e.event_key = pb.event_key "
            "WHERE pb.event_key LIKE ?", (f"%::{dom}%__vs__{ext}%",)):
        k = r["k"]
        noms = k.split("::", 1)[1]
        h, a = noms.split("__vs__", 1)
        if not (h.startswith(dom) and a.startswith(ext)):
            continue
        instants = [t for t in (_instant(r["debut"]), _instant_cle(k)) if t]
        if any(abs(t - quand) <= TOLERANCE for t in instants):
            trouves[k] = r["sport"]
    return sorted(trouves.items())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--fichier", default=str(FICHIER))
    ap.add_argument("--ecrire", action="store_true",
                    help="écrire vraiment (sinon simulation)")
    args = ap.parse_args(argv)

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    maintenant = datetime.now(timezone.utc).isoformat()
    ecrits = deja = refuses = 0
    with open(args.fichier, newline="", encoding="utf-8") as f:
        for ligne in csv.DictReader(f):
            quand = datetime.strptime(ligne["date_utc"], "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc)
            dom, ext = ligne["domicile"].strip(), ligne["exterieur"].strip()
            sd, se = int(ligne["score_dom"]), int(ligne["score_ext"])
            libelle = f"{ligne['date_utc']} {dom} - {ext} {sd}-{se}"
            if not (ligne.get("source_1") and ligne.get("source_2")):
                print(f"  REFUS   {libelle} : il faut deux sources")
                refuses += 1
                continue
            cles = candidats(con, quand, dom, ext)
            exacts = [c for c in cles
                      if c[0].split("::", 1)[1] == f"{dom}__vs__{ext}"]
            if len(cles) > 1 and len(exacts) == 1:
                cles = exacts      # « odense » écrit en entier ≠ « odensew »
            if len(cles) != 1:
                print(f"  REFUS   {libelle} : {len(cles)} match(s) trouvé(s) "
                      f"{[k for k, _ in cles]}")
                refuses += 1
                continue
            cle, sport = cles[0]
            if (sport or "").lower() not in ("soccer", "unknown", ""):
                print(f"  REFUS   {libelle} : sport « {sport} », pas du football")
                refuses += 1
                continue
            if con.execute("SELECT 1 FROM results WHERE event_key = ?",
                           (cle,)).fetchone():
                print(f"  DÉJÀ    {libelle} : résultat déjà en base, inchangé")
                deja += 1
                continue
            print(f"  {'ÉCRIT ' if args.ecrire else 'À ÉCRIRE'} {libelle} "
                  f"→ {vainqueur(sd, se)}  [{cle}]")
            if args.ecrire:
                con.execute(
                    "INSERT OR IGNORE INTO results(event_key, winner, home_score, "
                    "away_score, source, settled_at) VALUES (?,?,?,?,?,?)",
                    (cle, vainqueur(sd, se), sd, se, SOURCE, maintenant))
            ecrits += 1
    if args.ecrire:
        con.commit()
    con.close()
    verbe = "écrit(s)" if args.ecrire else "à écrire (simulation)"
    print(f"\n{ecrits} {verbe} · {deja} déjà en base · {refuses} refusé(s)")
    if args.ecrire and ecrits:
        print("→ maintenant : .venv/bin/python -m src.main track-update")
    elif not args.ecrire and ecrits:
        print("→ relance avec --ecrire pour écrire")
    return 0


if __name__ == "__main__":
    sys.exit(main())
