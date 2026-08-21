"""Le correctif des totaux de jeux du tennis (§21.1-21.5) est-il en service,
et rend-il ce que le §21.6 bis a prédit ?

Remplace la commande du §21.6, qui ne pouvait pas tourner : elle appelle
`sqlite3`, absent de la VM (§20.12), et lit une colonne `outcome_line` qui
n'existe pas — elle s'appelle `line`.

    .venv/bin/python -m scripts.check_tennis_totals

Quatre questions, dans l'ordre où elles se conditionnent :

  A. Pinnacle publie-t-il des lignes de JEUX (15 a 25) en plus du 2,5 en sets ?
     Non => le correctif n'est pas deploye, rien d'autre ne peut marcher.
  B. Des books offrent-ils ces memes lignes ? Sans recouvrement, aucune
     detection n'est possible meme avec une reference parfaite.
  C. Combien de detections tennis/totals sur 24 h ? C'est la prediction
     falsifiable du §21.6 bis : moins de 2 => il reste un blocage,
     autour de 5 => le correctif fait son travail.
  D. Ces detections apparient-elles bien une echelle a elle-meme ? Un compte
     au-dessus du predit se lit d'abord comme la panne §21.3 — un total de
     jeux par set pris pour un total de match — avant de se lire comme une
     bonne nouvelle. D repond, et donne la concentration : un seul match mal
     price rend plusieurs detections.

La lecture est en mode `ro` : ce script ne peut rien ecrire, et le daemon
peut tourner pendant.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

# Au-dela de 15, une ligne de totaux ne peut etre que des jeux : les sets se
# jouent a 2,5 ou 3,5 (§21.3). C'est cette absence de recouvrement qui rend la
# regle sure.
GAMES_MIN_LINE = 15.0

# Les trois echelles que « totals » recouvre au tennis, et le trou entre elles.
# Un set se joue a 2,5/3,5 (4,5 en Bo5) ; un match en Bo3 se totalise a partir
# de ~14,5 jeux. Entre les deux, plus rien de legitime : une ligne qui tombe
# dans ce trou est un total de jeux PAR SET (6 a 13 jeux) qui a fuite dans un
# marche de match. C'est la signature §21.3, et le volet D la cherche.
SETS_MAX_LINE = 4.5
AMBIGUOUS_BAND = (SETS_MAX_LINE, 14.5)

# §21.6 bis : 77 detections de totaux football x 5,9 % de volume tennis.
EXPECTED_PER_DAY = (4, 5)
BLOCKED_BELOW = 2


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _supply(conn: sqlite3.Connection, hours: int) -> list[sqlite3.Row]:
    """Qui publie des totaux de tennis, et sur quelles lignes.

    Le balayage temporel de `quotes` que le §17.7 interdit portait sur le
    regime d'ecriture dense (~2,5 M lignes/h). Depuis l'ecriture parcimonieuse
    du §18.5 il en reste ~63 000/h, soit quelques secondes de lecture.
    """
    return conn.execute(
        """
        SELECT q.book,
               COUNT(*)                                        AS cotes,
               COUNT(DISTINCT q.event_key)                     AS events,
               COUNT(DISTINCT q.line)                          AS lignes,
               MIN(q.line)                                     AS ligne_min,
               MAX(q.line)                                     AS ligne_max,
               SUM(CASE WHEN q.line >= ? THEN 1 ELSE 0 END)    AS cotes_jeux
        FROM quotes q
        JOIN events e ON e.event_key = q.event_key
        WHERE e.sport = 'tennis'
          AND q.market = 'totals'
          AND q.fetched_at > datetime('now', ?)
        GROUP BY q.book
        ORDER BY cotes DESC
        """,
        (GAMES_MIN_LINE, f"-{hours} hours"),
    ).fetchall()


def _detections(conn: sqlite3.Connection, hours: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT v.book, COUNT(*) AS n, ROUND(AVG(v.ev_pct), 2) AS ev_moy
        FROM value_bets v
        JOIN events e ON e.event_key = v.event_key
        WHERE e.sport = 'tennis'
          AND v.market = 'totals'
          AND v.detected_at > datetime('now', ?)
        GROUP BY v.book
        ORDER BY n DESC
        """,
        (f"-{hours} hours",),
    ).fetchall()


def _detection_detail(conn: sqlite3.Connection, hours: int) -> list[sqlite3.Row]:
    """Chaque detection avec sa ligne, sa reference, et de quoi juger l'echelle.

    `reference_book` vaut NULL quand la reference est Pinnacle : storage.py ne
    l'ecrit que pour une reference de repli (§17). Un NULL est donc une bonne
    nouvelle, pas une donnee manquante.
    """
    return conn.execute(
        """
        SELECT v.event_key, v.book, v.line, v.outcome_label, v.odd_taken,
               ROUND(v.ev_pct, 2)                       AS ev,
               COALESCE(v.reference_book, 'pinnacle')   AS ref,
               (SELECT COUNT(DISTINCT q.outcome_label)
                  FROM quotes q
                 WHERE q.event_key = v.event_key
                   AND q.book      = 'pinnacle'
                   AND q.market    = 'totals'
                   AND q.line      = v.line)            AS ref_cotes
        FROM value_bets v
        JOIN events e ON e.event_key = v.event_key
        WHERE e.sport = 'tennis'
          AND v.market = 'totals'
          AND v.detected_at > datetime('now', ?)
        ORDER BY v.line, v.book
        """,
        (f"-{hours} hours",),
    ).fetchall()


def _scale(line: float | None) -> str:
    """SETS / JEUX / AMBIGU — l'echelle que la valeur de ligne trahit."""
    if line is None:
        return "?"
    lo, hi = AMBIGUOUS_BAND
    if line <= lo:
        return "sets"
    if line >= hi:
        return "jeux"
    return "AMBIGU"


def _poisson_tail(k: int, lam: float) -> float:
    """P(X >= k) pour un Poisson de moyenne lam, en pourcentage.

    La prediction du §21.6 bis est une moyenne, pas un plafond. Sans cette
    borne on lit un ecart de comptage comme une panne — ou l'inverse.
    """
    import math

    cdf = sum(math.exp(-lam) * lam ** i / math.factorial(i) for i in range(k))
    return max(0.0, 1.0 - cdf) * 100


def _overlap(conn: sqlite3.Connection, hours: int) -> tuple[int, int]:
    """Evenements ou Pinnacle ET au moins un book offrent une ligne de jeux.

    C'est la vraie borne du gisement : une reference sans book en face, ou
    l'inverse, ne produit rien.
    """
    row = conn.execute(
        """
        WITH jeux AS (
            SELECT DISTINCT q.event_key, q.book
            FROM quotes q
            JOIN events e ON e.event_key = q.event_key
            WHERE e.sport = 'tennis'
              AND q.market = 'totals'
              AND q.line >= ?
              AND q.fetched_at > datetime('now', ?)
        )
        SELECT
            COUNT(DISTINCT CASE WHEN book = 'pinnacle' THEN event_key END) AS ref,
            COUNT(DISTINCT CASE WHEN event_key IN (
                    SELECT event_key FROM jeux WHERE book = 'pinnacle')
                AND book <> 'pinnacle' THEN event_key END)                 AS avec_book
        FROM jeux
        """,
        (GAMES_MIN_LINE, f"-{hours} hours"),
    ).fetchone()
    return row["ref"] or 0, row["avec_book"] or 0


def main(argv: list[str]) -> int:
    # ⚠️ `--help` était pris pour la DONNÉE positionnelle — un nom de book, un
    # chemin de base — et la sonde répondait « Aucune détection pour --help ».
    # Le §4.1 promettait pourtant que toutes acceptent `--help`. Promesse tenue.
    if any(a in ("-h", "--help") for a in argv[1:]):
        print(__doc__)
        return 0

    db = Path(argv[1] if len(argv) > 1 else "data/valuebet.db")
    if not db.exists():
        print(f"base introuvable : {db}", file=sys.stderr)
        return 2

    conn = _connect(db)
    supply_hours, det_hours = 1, 24

    print(f"=== A. Offre de totaux au tennis, {supply_hours} h ===")
    rows = _supply(conn, supply_hours)
    if not rows:
        print("  aucune cote de totaux au tennis. Le daemon tourne-t-il ?")
    pinnacle = None
    for r in rows:
        marker = ""
        if r["book"] == "pinnacle":
            pinnacle = r
            marker = "   <- la reference"
        print(
            f"  {r['book']:16} {r['cotes']:5} cotes  {r['events']:4} events  "
            f"{r['lignes']:3} lignes  [{r['ligne_min']} .. {r['ligne_max']}]  "
            f"dont jeux : {r['cotes_jeux']}{marker}"
        )

    print()
    if pinnacle is None:
        verdict_a = "ECHEC : Pinnacle ne publie aucun total de tennis."
    elif pinnacle["cotes_jeux"] == 0:
        verdict_a = (
            f"ECHEC : Pinnacle reste aux seuls sets "
            f"[{pinnacle['ligne_min']} .. {pinnacle['ligne_max']}]. "
            "Le correctif du §21.2 n'est PAS en service — verifier que la VM a "
            "bien pull et redemarre (§19.11)."
        )
    else:
        verdict_a = (
            f"OK : Pinnacle publie {pinnacle['cotes_jeux']} cotes en jeux "
            f"sur {pinnacle['lignes']} lignes distinctes. Correctif en service."
        )
    print(f"  {verdict_a}")

    ref, avec_book = _overlap(conn, supply_hours)
    print()
    print(f"=== B. Recouvrement, {supply_hours} h ===")
    print(f"  matchs avec une ligne de jeux chez Pinnacle : {ref}")
    print(f"  ... dont au moins un book en face          : {avec_book}")
    if ref and not avec_book:
        print("  ATTENTION : reference sans book en face, aucune detection possible.")

    print()
    print(f"=== C. Detections tennis/totals, {det_hours} h ===")
    dets = _detections(conn, det_hours)
    total = sum(r["n"] for r in dets)
    for r in dets:
        print(f"  {r['book']:16} {r['n']:4}   EV moy {r['ev_moy']}%")
    if not dets:
        print("  aucune")

    print()
    lo, hi = EXPECTED_PER_DAY
    print(f"  prediction §21.6 bis : {lo} a {hi} par jour")
    print(f"  observe              : {total}")
    if total < BLOCKED_BELOW:
        print("  VERDICT : sous le seuil — il reste un blocage, reprendre la chasse.")
    elif total <= hi + 3:
        print("  VERDICT : conforme — le correctif fait son travail.")
    else:
        print(
            "  VERDICT : nettement au-dessus du predit. Bonne nouvelle possible, "
            "mais verifier d'abord qu'aucune ligne de sets n'est lue comme des "
            "jeux (§21.3) — c'est la panne qui produirait ce profil."
        )

    if dets:
        print()
        print(f"=== D. Audit d'echelle des detections, {det_hours} h ===")
        detail = _detection_detail(conn, det_hours)
        for d in detail:
            ech = _scale(d["line"])
            flag = "  <-- SUSPECT" if ech == "AMBIGU" else ""
            manque = " (ref absente des quotes : purge)" if d["ref_cotes"] == 0 else ""
            if 0 < d["ref_cotes"] < 2:
                manque = "  <-- REFERENCE A UN SEUL COTE"
            print(
                f"  ligne {str(d['line']):>5} [{ech:6}]  {d['book']:16} "
                f"{d['outcome_label']:6} @ {d['odd_taken']:5}  EV {d['ev']:5}%  "
                f"ref {d['ref']}{manque}{flag}"
            )

        ambigus = [d for d in detail if _scale(d["line"]) == "AMBIGU"]
        unilateral = [d for d in detail if 0 < d["ref_cotes"] < 2]
        hors_pinnacle = [d for d in detail if d["ref"] != "pinnacle"]
        events = {d["event_key"] for d in detail}

        print()
        lo, hi = AMBIGUOUS_BAND
        if ambigus:
            print(
                f"  ECHEC : {len(ambigus)} detection(s) sur une ligne entre {lo} et "
                f"{hi} — ni sets ni jeux de match. C'est la panne du §21.3 : un "
                "total de jeux par set lu comme un total de match. Ne pas jouer "
                "ces paris, et remonter au scraper du book concerne."
            )
        else:
            print(
                f"  OK : aucune ligne dans la bande {lo}-{hi}. Toutes les "
                "detections apparient une echelle a elle-meme, donc la confusion "
                "jeux/sets du §21.3 ne les explique pas."
            )
        if unilateral:
            print(
                f"  ATTENTION : {len(unilateral)} reference(s) Pinnacle a un seul "
                "cote — le devig n'avait pas ses deux faces."
            )
        if hors_pinnacle:
            print(
                f"  NOTE : {len(hors_pinnacle)} detection(s) valorisee(s) contre une "
                "reference de repli, pas Pinnacle (§17)."
            )

        print()
        print(f"  detections : {len(detail)} sur {len(events)} match(s) distinct(s)")
        if events:
            print(f"  soit {len(detail) / len(events):.1f} detection(s) par match touche")
        print(
            "  Un match mal price rend une detection par book et par ligne : le "
            "comptage se concentre, il ne se distribue pas."
        )
        exp = sum(EXPECTED_PER_DAY) / 2
        print(
            f"  P(>= {len(detail)} | moyenne {exp}) = {_poisson_tail(len(detail), exp):.1f}% "
            "— et un Poisson SOUS-estime encore la dispersion vu ce groupement."
        )

    print()
    print("  Rappel : ce sont des DETECTIONS. Quatre books sur huit sont muets")
    print("  (§21.8), donc les alertes recues seront moins nombreuses.")
    print("  La CLV de ces detections reste inconnue tant qu'aucun match n'a")
    print("  cloture : .venv/bin/python -m scripts.clv_split --by sport,market --min 20")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
