#!/usr/bin/env python3
"""Montrer la FORME d'une sonde MagicBetting, pas son contenu.

`magic_probe_report.py` répond « combien d'événements » ; celui-ci répond
« comment c'est fait ». Il sert à raccorder un endpoint qu'on ne connaît pas
encore — la liste des championnats, celle des tournois — sans deviner la
structure d'après le nom des paramètres. Ceux-ci se contredisent d'ailleurs
d'un endpoint à l'autre : `champId=1135` chez `geteventslist` désigne la même
chose que `tournamentId=1135` chez `getmixedsportandeventslistwithoutright`.

Usage :
    .venv/bin/python scripts/magic_probe_show.py championshiplist
    .venv/bin/python scripts/magic_probe_show.py getmixedtournaments
    .venv/bin/python scripts/magic_probe_show.py sportlist
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PROBE_DIR = Path(os.getenv(
    "MAGIC_INGEST_DIR",
    str(Path(__file__).resolve().parent.parent / "data" / "magicbetting"),
)) / "probes"

MAX_DEPTH = 3
MAX_ITEMS = 3


def describe(node, depth: int = 0, path: str = "") -> None:
    pad = "  " * depth
    if isinstance(node, dict):
        print(f"{pad}{path or '(objet)'} — {len(node)} clés")
        if depth >= MAX_DEPTH:
            return
        for k, v in list(node.items())[:20]:
            if isinstance(v, (dict, list)):
                describe(v, depth + 1, str(k))
            else:
                s = repr(v)
                print(f"{pad}  {k} = {s[:70]}")
    elif isinstance(node, list):
        print(f"{pad}{path or '(liste)'} — {len(node)} éléments")
        if depth >= MAX_DEPTH or not node:
            return
        # Une seule entrée suffit à montrer la forme ; les suivantes sont
        # identiques et noieraient la sortie.
        describe(node[0], depth + 1, f"{path}[0]")
    else:
        print(f"{pad}{path} = {repr(node)[:70]}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        if PROBE_DIR.exists():
            print("Sondes disponibles :")
            for f in sorted(PROBE_DIR.glob("*.json")):
                print(f"  {f.stem}")
        return 1

    motif = sys.argv[1].lower()
    hits = [f for f in sorted(PROBE_DIR.glob("*.json")) if motif in f.name.lower()]
    if not hits:
        print(f"Aucune sonde ne contient {motif!r} dans son nom.")
        print("Noms disponibles :")
        for f in sorted(PROBE_DIR.glob("*.json")):
            print(f"  {f.stem}")
        return 1

    for f in hits[:MAX_ITEMS]:
        rec = json.loads(f.read_text(encoding="utf-8"))
        print("=" * 78)
        print(rec.get("url", "?"))
        print(f"? {rec.get('query', '')}")
        print("-" * 78)
        if rec.get("clear") is None:
            print(f"non déchiffrée : {rec.get('note')}")
            continue
        describe(rec["clear"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
