"""Importe les résultats retrouvés à la main sur le web.

Chaque ligne de `scripts/resultats_web.csv` est un résultat confirmé par DEUX
sources indépendantes (URL dans le fichier, pour la trace). Le script retrouve
le match dans nos paris joués, puis écrit le résultat avec la source
`manuel-web` :

* SIMULATION par défaut — rien n'est écrit sans `--ecrire` ;
* jamais d'écrasement : un match qui a déjà un résultat est laissé tel quel ;
* une ligne qui ne désigne pas exactement UN match est refusée (aucun match,
  ou plusieurs clés possibles) — mieux vaut un pari sans résultat qu'un faux ;
* une ligne dont le sport contredit celui du match en base est refusée.

Ce que chaque sport écrit (`settle()` compare `home + away` à la ligne d'un
« totals », donc l'unité compte) :

* football : le score à 90 minutes ; le vainqueur s'en déduit ;
* basket : le score final, prolongation comprise ; le vainqueur s'en déduit ;
* hockey : le score d'un match gagné dans le temps réglementaire ;
* tennis : le vainqueur (colonne `vainqueur`, JAMAIS déduit : les jeux ne
  décident pas du match) et, s'ils sont connus, les JEUX de chaque camp ;
* volley : le vainqueur seul (des sets en `home_score` fausseraient un total
  de points).

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
# Sports dont le vainqueur se déduit du score (et où le score est écrit).
SCORE_DECIDE = {"soccer", "basketball", "hockey"}
SPORTS = SCORE_DECIDE | {"tennis", "volleyball"}


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


def vainqueur(dom: float, ext: float) -> str:
    return "home" if dom > ext else "away" if ext > dom else "draw"


def meme_sport(csv_sport: str, base: "str | None") -> bool:
    """`events.sport` vaut « basket » ou « basketball », « volley »… et
    « unknown » (ou rien) sur une ligne réparée : seul un sport AUTRE refuse."""
    base = (base or "").lower()
    if base in ("", "unknown", "?"):
        return True
    return base[:6] == csv_sport[:6]


def candidats(con, quand: datetime, dom: str, ext: str) -> list:
    """Les clés de match JOUÉES qui collent : noms (préfixes de la clé, comme
    la liste les tronque) dans le bon sens et coup d'envoi à ±26 h. La clé d'un
    clic « nu » (sans event_key) se lit en tête de son dedup_key."""
    trouves = {}
    for r in con.execute(
            "SELECT DISTINCT k, e.sport AS sport, e.start_time AS debut FROM ("
            "  SELECT COALESCE(event_key, CASE WHEN instr(dedup_key, '|') > 0 "
            "         THEN substr(dedup_key, 1, instr(dedup_key, '|') - 1) END) AS k "
            "  FROM played_bets) pb "
            "LEFT JOIN events e ON e.event_key = pb.k "
            "WHERE pb.k LIKE ?", (f"%::{dom}%__vs__{ext}%",)):
        k = r["k"]
        noms = k.split("::", 1)[1]
        h, a = noms.split("__vs__", 1)
        if not (h.startswith(dom) and a.startswith(ext)):
            continue
        instants = [t for t in (_instant(r["debut"]), _instant_cle(k)) if t]
        if any(abs(t - quand) <= TOLERANCE for t in instants):
            trouves[k] = r["sport"]
    return sorted(trouves.items())


def _nombre(brut: str) -> "float | None":
    brut = (brut or "").strip()
    return float(brut) if brut else None


def lire_ligne(ligne: dict) -> "tuple[str, str | None, float | None, float | None]":
    """(sport, vainqueur, home_score, away_score) à écrire — ou ValueError si
    la ligne est incohérente."""
    sport = (ligne.get("sport") or "soccer").strip().lower()
    if sport not in SPORTS:
        raise ValueError(f"sport « {sport} » inconnu")
    sd, se = _nombre(ligne.get("score_dom")), _nombre(ligne.get("score_ext"))
    donne = (ligne.get("vainqueur") or "").strip().lower() or None
    if sport in SCORE_DECIDE:
        if sd is None or se is None:
            raise ValueError("score manquant")
        v = vainqueur(sd, se)
        if sport != "soccer" and v == "draw":
            raise ValueError("match nul impossible dans ce sport")
        if donne and donne != v:
            raise ValueError(f"vainqueur « {donne} » contredit le score")
        return sport, v, sd, se
    if donne not in ("home", "away"):
        raise ValueError("vainqueur (home/away) obligatoire")
    if sport == "volleyball":
        return sport, donne, None, None
    if (sd is None) != (se is None):
        raise ValueError("jeux : les deux ou aucun")
    return sport, donne, sd, se


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
            libelle = (f"{ligne['date_utc']} {dom} - {ext} "
                       f"{ligne.get('score_dom', '')}-{ligne.get('score_ext', '')}")
            try:
                sport, v, sd, se = lire_ligne(ligne)
            except ValueError as e:
                print(f"  REFUS   {libelle} : {e}")
                refuses += 1
                continue
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
            cle, sport_base = cles[0]
            if not meme_sport(sport, sport_base):
                print(f"  REFUS   {libelle} : sport « {sport_base} » en base, "
                      f"« {sport} » dans le fichier")
                refuses += 1
                continue
            if con.execute("SELECT 1 FROM results WHERE event_key = ?",
                           (cle,)).fetchone():
                print(f"  DÉJÀ    {libelle} : résultat déjà en base, inchangé")
                deja += 1
                continue
            print(f"  {'ÉCRIT ' if args.ecrire else 'À ÉCRIRE'} {libelle} "
                  f"→ {v}  [{cle}]")
            if args.ecrire:
                con.execute(
                    "INSERT OR IGNORE INTO results(event_key, winner, home_score, "
                    "away_score, source, settled_at) VALUES (?,?,?,?,?,?)",
                    (cle, v, sd, se, SOURCE, maintenant))
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
