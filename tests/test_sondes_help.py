"""Toutes les sondes acceptent `--help` — la promesse du §4.1, vérifiée.

Le §4.1 affirme que les 20 sondes « se lancent en `.venv/bin/python -m
scripts.<nom>` et acceptent `--help` ». C'était FAUX pour trois d'entre elles :
`crossclose`, `book_health` et `check_tennis_totals` lisent un argument
POSITIONNEL et prenaient `--help` pour la donnée. `book_health --help`
répondait « Aucune détection pour --help sur la fenêtre » — un message qui
envoie chercher un book absent au lieu de dire comment s'en servir.

Une documentation qui promet une commande qui ne marche pas coûte plus qu'une
documentation muette : elle fait perdre le temps de la vérifier.

⚠️ `cycle_speed` et `magic_probe_report` n'ont volontairement AUCUN argument.
Ils lisent `valuebet.log` et le répertoire des sondes, donc ils ne peuvent pas
répondre hors de la VM — ils sont exclus de ce test, pas oubliés.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

RACINE = Path(__file__).resolve().parents[1]

# Les sondes qui prennent des arguments. `cycle_speed` et `magic_probe_report`
# n'en prennent aucun et dépendent d'un état de la VM : hors périmètre.
SONDES = [
    "clv_split", "clv_independence", "pnl_detections", "under_threshold",
    "crossclose", "market_supply", "market_expansion", "ev_outliers",
    "check_half_time", "check_tennis_totals", "discover_half_time",
    "handicap_conventions", "book_health", "scores_coverage", "repair_events",
    "repair_leagues", "clv_roi_matrix", "staking_curves", "closing_gap",
    "book_latency", "alert_cost", "noms_hostiles", "book_exclusif",
]


@pytest.mark.parametrize("sonde", SONDES)
def test_la_sonde_repond_a_help(sonde):
    r = subprocess.run([sys.executable, "-m", f"scripts.{sonde}", "--help"],
                       cwd=RACINE, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"{sonde} : code {r.returncode}\n{r.stdout}\n{r.stderr}"
    # Une aide qui ne dit rien ne vaut pas mieux qu'une absence d'aide.
    assert len(r.stdout.strip()) > 80, f"{sonde} : aide trop courte\n{r.stdout}"
