"""L'interface web Analytics — une couche de PRÉSENTATION, et rien d'autre.

Elle compose l'application de la Phase 2 sans la modifier : `creer_app` prend
l'app FastAPI validée et lui monte des fichiers statiques. Aucune logique
métier, aucune formule, aucun accès à la base — le navigateur n'appelle que
`/api/filters`, `/api/analyse` et `/api/detail`.
"""
from .app import creer_app  # noqa: F401

__all__ = ["creer_app"]
