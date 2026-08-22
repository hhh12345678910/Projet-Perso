"""La console partagée du projet.

Extraite de `main.py` au moment du découpage : les modules du moteur
(`detection.py`, et ceux qui suivront) impriment des avertissements — une
référence écartée, un book muet — et doivent le faire par le MÊME objet que la
CLI. Deux `Console()` distinctes écriraient bien toutes les deux sur la sortie
standard, mais toute mise en forme ou redirection posée sur l'une ignorerait
l'autre, en silence.
"""
from __future__ import annotations

from rich.console import Console

console = Console()
