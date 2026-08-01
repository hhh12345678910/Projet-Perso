"""Réglages de src.main lus dans l'environnement.

Ces défauts sont évalués à l'import, donc une régression ne se voit ni au
démarrage ni dans les logs : la commande tourne, simplement avec la mauvaise
valeur. Pour la purge, cela signifie une base qui double sans que rien ne le
signale.
"""
from __future__ import annotations

import importlib
import inspect
import sys


def _prune_default(monkeypatch, value: str | None) -> int:
    """Recharge src.main avec PRUNE_DAYS fixé, et lit le défaut de --days."""
    if value is None:
        monkeypatch.delenv("PRUNE_DAYS", raising=False)
    else:
        monkeypatch.setenv("PRUNE_DAYS", value)
    for name in [n for n in list(sys.modules) if n == "src.main"]:
        del sys.modules[name]
    module = importlib.import_module("src.main")
    param = inspect.signature(module.prune).parameters["retention_days"]
    return param.default.default


def test_prune_retention_defaults_to_two_days(monkeypatch):
    assert _prune_default(monkeypatch, None) == 2


def test_prune_retention_reads_the_environment(monkeypatch):
    """La rétention est le seul levier sur la taille de la base, et elle
    n'était réglable qu'en éditant une unité systemd — donc en pratique
    jamais. Mesuré le 01/08 : deux jours pèsent 23 Go de données utiles.

    Une journée suffit dès lors que close-lines tourne toutes les heures, la
    ligne de clôture étant capturée dans l'heure suivant le coup d'envoi."""
    assert _prune_default(monkeypatch, "1") == 1
    assert _prune_default(monkeypatch, "7") == 7
