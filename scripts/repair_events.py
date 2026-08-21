#!/usr/bin/env python3
"""Recréer les lignes `events` manquantes des paris orphelins.

Le daemon ne construisait ses lignes `events` qu'à partir des événements de
Pinnacle. Tout match qu'il ne price pas — donc précisément ceux où la référence
de repli se déclenche — restait sans ligne. Ces paris sortaient sans nom
d'équipe, sans ligue et sans heure de coup d'envoi : invérifiables, sans CLV
possible, et comptés quand même dans les moyennes. Vu le 16/08 : une détection
à +90,2 % affichée « None — None ».

La cause est corrigée dans `build_event_rows`. Reste l'existant, réparable :
l'`event_key` porte déjà la date et les deux équipes —
`202608161500::hodd__vs__strommen` — donc rien n'est à deviner.

⚠️ Le SPORT n'est pas dans la clé et ne se déduit d'aucune table. Il est écrit
« unknown » plutôt qu'inventé : une valeur fausse fausserait les ventilations
par sport de clv-report sans que rien ne le signale, ce qui est pire que de ne
pas savoir. Les nouvelles lignes, elles, portent le bon sport.

Usage :
    .venv/bin/python scripts/repair_events.py           # simulation, n'écrit rien
    .venv/bin/python scripts/repair_events.py --apply
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import ScanConfig                        # noqa: E402
from src.matcher import parse_event_key                  # noqa: E402


def main() -> int:
    # ⚠️ AVANT toute lecture d'argument : `--help` déclenchait l'analyse au
    # lieu de l'afficher. Inoffensif ici parce que l'écriture exige `--apply`,
    # mais c'est la seule sonde du projet qui écrit en base — une aide qui
    # EXÉCUTE est le pire endroit où poser ce défaut (§4.1).
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(__doc__)
        return 0

    apply = "--apply" in sys.argv
    db = ScanConfig().db_path

    c = sqlite3.connect(db)
    orphelins = [r[0] for r in c.execute("""
        SELECT DISTINCT v.event_key FROM value_bets v
        LEFT JOIN events e ON e.event_key = v.event_key
        WHERE e.event_key IS NULL
    """)]
    if not orphelins:
        print("Aucun pari orphelin : chaque event_key a son match.")
        return 0

    rows, illisibles = [], []
    for ek in orphelins:
        parsed = parse_event_key(ek)
        if parsed is None:
            illisibles.append(ek)
            continue
        start, home, away = parsed
        rows.append((ek, "unknown", "", home, away, start.isoformat()))

    print(f"{len(orphelins)} event_key sans match rattaché")
    print(f"  {len(rows):>4} reconstructibles depuis la clé")
    print(f"  {len(illisibles):>4} clés illisibles (laissées telles quelles)\n")
    for ek, _, _, home, away, start in sorted(rows, key=lambda r: r[5])[:15]:
        print(f"  {start[:16]}  {home} — {away}")
    if len(rows) > 15:
        print(f"  … et {len(rows) - 15} autres")
    for ek in illisibles[:5]:
        print(f"  ⚠ illisible : {ek}")

    if not apply:
        print("\nSimulation — rien n'a été écrit. Relance avec --apply.")
        return 0

    # INSERT OR IGNORE : si une ligne est apparue entre-temps (le daemon
    # tourne), la sienne fait foi — elle porte le vrai sport, pas « unknown ».
    with c:
        c.executemany(
            "INSERT OR IGNORE INTO events(event_key, sport, league, home, away, "
            "start_time) VALUES (?, ?, ?, ?, ?, ?)", rows,
        )
    reste = c.execute("""
        SELECT COUNT(DISTINCT v.event_key) FROM value_bets v
        LEFT JOIN events e ON e.event_key = v.event_key
        WHERE e.event_key IS NULL
    """).fetchone()[0]
    print(f"\n{len(rows)} lignes écrites. Paris encore orphelins : {reste}")
    c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
