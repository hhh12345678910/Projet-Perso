#!/usr/bin/env python3
"""Un book est-il traité comme tous les autres, ou seulement collecté ?

Ajouter un scraper ne suffit pas : une cote doit ensuite traverser la détection
de value, le suivi de trajectoire, la notification, puis la capture de clôture.
Chacune de ces étapes peut manquer un book SANS lever la moindre erreur — c'est
la panne dominante du projet (§11). Ce script parcourt la chaîne et dit à quel
maillon ça s'arrête.

Écrit en constatant sur MagicBetting qu'on ne savait pas répondre à « est-ce
qu'il est suivi comme les autres ? » autrement qu'en relisant le code. Vaut
pour n'importe quel book qu'on vient de brancher.

Usage :
    .venv/bin/python scripts/book_health.py magicbetting
    .venv/bin/python scripts/book_health.py            # tous les books, vue d'ensemble
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

DB = os.getenv("VALUEBET_DB",
               str(Path(__file__).resolve().parent.parent / "data" / "valuebet.db"))
WINDOW_H = int(os.getenv("BOOK_HEALTH_HOURS", "48"))


def main() -> int:
    book = sys.argv[1] if len(sys.argv) > 1 else None
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    cut = f"datetime('now', '-{WINDOW_H} hours')"

    print(f"=== Chaîne de traitement, {WINDOW_H} dernières heures ===\n")

    # ⚠️ On ne touche JAMAIS à `quotes` : elle pèse des dizaines de Go et un
    # COUNT par book n'y rendrait pas la main (§18.7). Tout se lit dans les
    # tables d'analyse, qui sont petites — et c'est suffisant, puisque la
    # question porte sur le TRAITEMENT, pas sur la collecte brute.
    rows = c.execute(f"""
        SELECT book,
               COUNT(*)                                   AS detections,
               SUM(CASE WHEN ev_pct >= 5 THEN 1 ELSE 0 END) AS ev5,
               ROUND(AVG(ev_pct), 2)                      AS ev_moy,
               ROUND(MAX(ev_pct), 1)                      AS ev_max
        FROM value_bets
        WHERE detected_at >= {cut} {"AND book = ?" if book else ""}
        GROUP BY book ORDER BY detections DESC
    """, (book,) if book else ()).fetchall()

    if not rows:
        print(f"Aucune détection pour {book or 'aucun book'} sur la fenêtre.")
        print("Soit le book ne produit pas de value, soit il n'atteint pas la "
              "détection. Les deux se ressemblent — regarde valuebet.log.")
        return 1

    ids = {}
    print(f"{'book':22} {'détect.':>8} {'EV≥5%':>7} {'EV moy':>7} {'EV max':>7}")
    print("-" * 60)
    for b, n, ev5, moy, mx in rows:
        print(f"{b:22} {n:>8,} {ev5 or 0:>7,} {moy or 0:>7} {mx or 0:>7}")
        ids[b] = n

    for b in ids:
        print(f"\n--- {b} ---")
        # 1. Suivi de trajectoire : le book est-il dans bet_corrections ?
        n_corr, n_align = c.execute(f"""
            SELECT COUNT(*), SUM(CASE WHEN aligned_at IS NOT NULL THEN 1 ELSE 0 END)
            FROM bet_corrections WHERE book = ? AND detected_at >= {cut}
        """, (b,)).fetchone()
        print(f"  suivi de correction  : {n_corr or 0:,} suivis, "
              f"{n_align or 0:,} alignés")

        # 2. Courbes de cotes. Borné par value_bet_id pour rester sur le
        #    préfixe de l'index idx_oh_bet : sans cette borne, un COUNT par
        #    book balaierait toute la table.
        floor_id = c.execute(
            f"SELECT MIN(id) FROM value_bets WHERE detected_at >= {cut}"
        ).fetchone()[0] or 0
        n_hist, n_books = c.execute("""
            SELECT COUNT(*), COUNT(DISTINCT h.book)
            FROM odds_history h JOIN value_bets v ON v.id = h.value_bet_id
            WHERE h.value_bet_id >= ? AND v.book = ?
        """, (floor_id, b)).fetchone()
        print(f"  courbes odds_history : {n_hist or 0:,} points sur "
              f"{n_books or 0} books suivis")
        n_own = c.execute("""
            SELECT COUNT(*) FROM odds_history
            WHERE value_bet_id >= ? AND book = ?
        """, (floor_id, b)).fetchone()[0]
        print(f"    dont sa propre cote : {n_own:,}")

        # 3. Clôture : sans elle, pas de CLV — le seul KPI qui compte.
        n_clv = c.execute(f"""
            SELECT COUNT(*) FROM clv_snapshots s JOIN value_bets v ON v.id = s.value_bet_id
            WHERE v.book = ? AND s.closing = 1 AND v.detected_at >= {cut}
        """, (b,)).fetchone()[0]
        print(f"  clôtures capturées   : {n_clv:,} "
              f"({100 * n_clv / max(ids[b], 1):.0f} % des détections)")

        # 4. Notifications : un book peut être coupé sans que rien ne le dise.
        off = c.execute(
            "SELECT disabled_at FROM book_alerts_off WHERE book = ?", (b,)
        ).fetchone()
        print(f"  alertes              : "
              + (f"COUPÉES depuis {off[0]}" if off else "actives"))

    print("\nLecture : « détections » sans « suivi » = la trajectoire ne le voit "
          "pas.\n« suivi » sans « clôtures » = pas de CLV, donc aucun moyen de "
          "savoir\nsi ce book est rentable. Les clôtures n'arrivent qu'APRÈS le "
          "coup d'envoi :\nun book branché ce matin en aura peu, c'est normal.")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
