"""Isolation partagée par toute la suite.

Ce fichier existe pour UNE raison : `src/teams.py` garde son registre dans des
variables de MODULE (`_DISPLAY`, `_STORAGE`). Elles survivent d'un test à
l'autre, et une dizaine de commandes de production appellent `teams.init(storage)`
sur la VRAIE base. Il suffit donc qu'un test invoque l'une d'elles pour que tous
les noms d'équipes de la base réelle se retrouvent en cache pour les tests
SUIVANTS.

⚠️ Cette fuite ne se voit que sur une machine dont la base contient l'équipe
concernée. Le 22/08, `tests/test_scan_command.py` attendait « Anderlecht » — le
repli `.capitalize()` — et recevait « RSC Anderlecht » sur la VM, parce que la
base de production porte ce nom d'affichage. Vert en développement, rouge en
production : le test ne prouvait rien là où ça compte.

`test_teams.py` avait déjà sa propre garde. La remonter ici la rend systématique
plutôt que réservée au fichier qui y avait pensé.
"""
from __future__ import annotations

import pytest

from src import teams


@pytest.fixture(autouse=True)
def _registre_equipes_vierge():
    """Chaque test part d'un registre vide, quel que soit l'ordre d'exécution."""
    teams.clear_cache()
    yield
    teams.clear_cache()
