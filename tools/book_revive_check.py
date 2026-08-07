"""Les books désactivés répondent-ils encore depuis la VM ?

Chaque book du registre porte un motif de désactivation daté, et ces motifs
vieillissent : BetFirst a été coupé sur un 403 depuis le VPS, Golden Palace
parce que le COMPTE était limité — ce qui n'empêche pas de lire ses prix. Les
réactiver à l'aveugle produirait soit des cycles qui échouent en silence, soit
un book qu'on croit absent alors qu'il fonctionne.

Cette sonde appelle chaque scraper une fois, sur un sport, et dit ce qu'il
renvoie. Elle ne modifie rien.

    .venv/bin/python tools/book_revive_check.py
    .venv/bin/python tools/book_revive_check.py --sport tennis
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_env_file  # noqa: E402

load_env_file()


def _probe(name: str, fn, sport: str) -> tuple[str, str]:
    t0 = time.monotonic()
    try:
        quotes = fn(sport)
    except Exception as e:                                   # noqa: BLE001
        return "ÉCHEC", f"{type(e).__name__}: {str(e)[:110]}"
    ms = (time.monotonic() - t0) * 1000
    if not quotes:
        # Zéro cote n'est pas une panne : hors-saison, ou sport non couvert.
        # Les confondre est l'erreur du §13.4, qui a coûté deux diagnostics.
        return "VIDE", f"0 cote en {ms:.0f} ms — répond, mais rien à ce sport"
    events = len({q.event_key for q in quotes})
    return "OK", f"{len(quotes)} cotes, {events} événements, {ms:.0f} ms"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sport", default="soccer")
    args = ap.parse_args()

    from src import main as m

    # Ceux qui sont commentés dans _fetch_all_parallel, avec le motif d'origine.
    candidates = [
        ("BetFirst",     m.fetch_betfirst_quotes,       "403 depuis le VPS (anti-bot IP datacenter)"),
        ("GoldenPalace", m.fetch_goldenpalace_quotes,   "compte limité — mais l'API ne demande pas d'auth"),
        ("711",          m.fetch_sevenelevenbe_quotes,  "jumeau Kambi d'Unibet (anti rate-limit)"),
        ("Bingoal",      m.fetch_bingoal_quotes,        "jumeau Kambi d'Unibet"),
        ("Scooore",      m.fetch_scooore_quotes,        "jumeau Kambi d'Unibet"),
        ("MeridianBet",  m.fetch_meridian_quotes,       "token anti-bot TrafficGuard"),
        ("Betcenter",    m.fetch_betcenter_quotes,      "cotes erronées — NE PAS réactiver"),
    ]

    print(f"Sport testé : {args.sport}\n")
    print(f"{'book':14}{'état':8}  détail")
    print("-" * 92)
    results = []
    for name, fn, why in candidates:
        state, detail = _probe(name, fn, args.sport)
        results.append((name, state, why))
        print(f"{name:14}{state:8}  {detail}")
        print(f"{'':14}{'':8}  [motif d'origine : {why}]")
    print("-" * 92)
    live = [n for n, s, _ in results if s == "OK"]
    print(
        f"\n{len(live)} book(s) répondent : {', '.join(live) if live else '—'}\n"
        "\nRappels avant de réactiver :\n"
        "  • Betcenter reste dehors quoi qu'il réponde — ses cotes sont fausses,\n"
        "    et un book faux pollue les surebets et la ligne de référence.\n"
        "  • 711 / Bingoal / Scooore servent les MÊMES prix qu'Unibet (Kambi) :\n"
        "    utiles comme données, sans valeur pour la détection. Les couper\n"
        "    dans /book évite de tripler les alertes sur un seul prix.\n"
        "  • Un book qui répond ici depuis la VM peut échouer en production sous\n"
        "    la charge du cycle. Vérifier `books-coverage` après réactivation."
    )


if __name__ == "__main__":
    main()
