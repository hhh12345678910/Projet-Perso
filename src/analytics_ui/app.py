"""Montage de l'interface au-dessus de l'API Analytics.

POURQUOI UN MODULE SÉPARÉ PLUTÔT QU'UN MONTAGE DANS `analytics_api`
-------------------------------------------------------------------
`src/analytics_api/app.py` a été validé sur la base de production : 99 tests,
16 contrôles HTTP, une analyse de référence au centième près. Y ajouter un
montage de fichiers statiques rouvrirait cette validation pour une raison qui
n'a rien à voir avec l'API. On COMPOSE donc : l'app de la Phase 2 sort intacte
de son module, et c'est ici qu'on lui attache une façade.

⚠️ L'ORDRE DU MONTAGE EST LA SEULE CHOSE FRAGILE ICI. Starlette résout les
routes dans l'ordre d'enregistrement : monter les fichiers statiques sur « / »
AVANT que `/api/*` n'existe masquerait toute l'API derrière un 404 de fichier
introuvable. `creer_api()` est donc appelée d'abord, toujours, et un test
vérifie que `/api/health`, `/api/analyse` et `/docs` répondent encore après le
montage.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi.staticfiles import StaticFiles

from ..analytics_api.app import creer_app as creer_api

STATIQUES = Path(__file__).resolve().parent / "static"


def creer_app(db_path: Optional[str] = None):
    """L'API de la Phase 2, plus l'interface servie à la racine.

    `db_path` est transmis tel quel : la couche de présentation ne choisit
    jamais la base, elle ne fait que relayer ce que le lanceur lui donne."""
    app = creer_api(db_path)
    # `html=True` sert `index.html` sur « / » et sur tout répertoire.
    app.mount("/", StaticFiles(directory=str(STATIQUES), html=True), name="ui")
    return app


#: L'application de production. `uvicorn src.analytics_ui.app:app`.
app = creer_app()
