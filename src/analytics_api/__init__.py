"""Couche HTTP au-dessus de `src.analytics`. Elle ne calcule RIEN.

Tout ce que cette couche fait, c'est traduire une requête HTTP en `Filtres`,
appeler `src.analytics`, et rendre du JSON. Aucun SQL, aucune définition de
CLV, de ROI ou de règlement n'y apparaît — et un test le vérifie en
interceptant les appels.
"""
from .app import creer_app  # noqa: F401

__all__ = ["creer_app"]
