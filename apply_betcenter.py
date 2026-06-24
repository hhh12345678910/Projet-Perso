#!/usr/bin/env python3
"""Reactive le scraper Betcenter : ajoute l'entree d'enum manquante, le nom
d'affichage, et rebranche le scraper dans le daemon (_fetch_all_parallel).

Touche 3 fichiers. Pour chacun : ignore s'il est deja patche, verifie que
chaque ancre est unique, sauvegarde, applique, recompile (rollback sinon).
Les fichiers sont traites independamment et le patcher est re-lancable.

Usage : python3 apply_betcenter.py   (depuis ~/Projet-Perso)
"""
from __future__ import annotations

import py_compile
import shutil
import time
from pathlib import Path

NEW_FUNC = (
    'def fetch_betcenter_quotes(sport: str) -> list[OddQuote]:\n'
    '    """Fetch + parse the Betcenter prematch offer (Cashpoint/Merkur platform,\n'
    '    own odds API on oddsservice.betcenter.be -- independent odds)."""\n'
    '    try:\n'
    '        with BetCenterScraper() as bc:\n'
    '            return bc.fetch_all_quotes(sport)\n'
    '    except httpx.HTTPError as e:\n'
    '        console.print(f"[yellow]Betcenter skipped:[/yellow] {e}")\n'
    '        return []\n'
)

FILES: dict[str, dict] = {
    # patche en premier : l'enum doit exister avant le cablage de main.py
    "src/models.py": {
        "marker": 'BETCENTER = "betcenter"',
        "patches": [
            (
                '    SMARKETS = "smarkets"',
                '    BETCENTER = "betcenter"              '
                '# Betcenter.be - Cashpoint/Merkur, cotes independantes\n'
                '    SMARKETS = "smarkets"',
            ),
        ],
    },
    "src/alerter.py": {
        "marker": 'Book.BETCENTER: "Betcenter"',
        "patches": [
            (
                '    Book.SMARKETS: "Smarkets",',
                '    Book.BETCENTER: "Betcenter",\n    Book.SMARKETS: "Smarkets",',
            ),
        ],
    },
    "src/main.py": {
        "marker": "fetch_betcenter_quotes",
        "patches": [
            (
                "from .scrapers.betano import BetanoAuthError, BetanoScraper, "
                "parse_overview as betano_parse_overview",
                "from .scrapers.betano import BetanoAuthError, BetanoScraper, "
                "parse_overview as betano_parse_overview\n"
                "from .scrapers.betcenter import BetCenterScraper",
            ),
            (
                "def _fetch_all_parallel(\n    sport: str,",
                NEW_FUNC + "\n\ndef _fetch_all_parallel(\n    sport: str,",
            ),
            (
                "lambda: fetch_napoleon_quotes(sport),",
                'lambda: fetch_napoleon_quotes(sport),\n'
                '        "Betcenter":     lambda: fetch_betcenter_quotes(sport),',
            ),
        ],
    },
}


def main() -> int:
    any_done = False
    for rel, spec in FILES.items():
        target = Path(rel)
        if not target.exists():
            print(f"SKIP  {rel} : introuvable")
            continue
        src = target.read_text()
        if spec["marker"] in src:
            print(f"SKIP  {rel} : deja patche")
            continue

        bad = False
        for old, _new in spec["patches"]:
            n = src.count(old)
            if n != 1:
                print(f"ABORT {rel} : ancre trouvee {n}x (attendu 1) -> {old[:45]!r}")
                bad = True
        if bad:
            continue

        bak = target.with_name(f"{target.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(target, bak)
        out = src
        for old, new in spec["patches"]:
            out = out.replace(old, new, 1)
        target.write_text(out)
        try:
            py_compile.compile(str(target), doraise=True)
        except py_compile.PyCompileError as e:
            shutil.copy2(bak, target)
            print(f"ROLLBACK {rel} : erreur compilation\n{e}")
            continue
        print(f"OK    {rel} patche (backup {bak.name})")
        any_done = True

    if any_done:
        print("\nTermine. Verifie puis redemarre :")
        print('  python3 -c "from src.models import Book; print(Book.BETCENTER)"')
        print("  sudo systemctl restart valuebet-daemon.service")
    else:
        print("\nRien modifie.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
