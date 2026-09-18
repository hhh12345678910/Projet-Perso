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

LE SECOND PIÈGE : LE CACHE DU NAVIGATEUR
-----------------------------------------
Voir `StatiquesRevalidees`. Un déploiement correct peut servir une interface
FAUSSE, moitié neuve moitié ancienne, sans la moindre trace côté serveur.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi.staticfiles import StaticFiles

from ..analytics_api.app import creer_app as creer_api

STATIQUES = Path(__file__).resolve().parent / "static"


class StatiquesRevalidees(StaticFiles):
    """`StaticFiles`, mais qui oblige le navigateur à REVALIDER.

    ⚠️ CE N'EST PAS UNE OPTIMISATION, C'EST UN CORRECTIF DE DÉPLOIEMENT.

    Starlette ne pose aucun `Cache-Control`. Le navigateur applique alors sa
    fraîcheur HEURISTIQUE : il garde un fichier sans jamais le redemander
    pendant environ 10 % de son âge. Un `app.js` vieux de trois semaines est
    ainsi réputé frais pendant deux jours, et un F5 ne le déloge pas — seul
    un rechargement forcé y arrive, ce qu'aucun utilisateur ne devine.

    Constaté en production le 18/09, à la mise en service de la Phase 4 :
    `index.html` arrivait neuf (une navigation est toujours revalidée) pendant
    que `app.js` et `style.css` sortaient du cache en version Phase 3. La page
    mélangeait DEUX versions. L'ancien script injecte des `<option>` dans ce
    qui est devenu un `<div class="cases">` : les filtres s'affichaient en
    texte brut, sans case à cocher, le bloc des tranches d'EV restait vide.
    Aucune erreur, aucun journal, un serveur parfaitement à jour — le mode de
    défaillance silencieux du §13.12, transposé au navigateur.

    `no-cache` n'interdit pas le cache, il impose la revalidation : le
    navigateur envoie son `If-None-Match`, Starlette répond 304 sans corps
    tant que le fichier n'a pas bougé. Le coût est un aller-retour vide par
    ressource ; le bénéfice est qu'un déploiement ne peut plus servir deux
    versions à la fois.

    Pourquoi ici plutôt qu'un `?v=4` dans `index.html` : un numéro de version
    écrit à la main doit être incrémenté à CHAQUE déploiement, et le jour où
    on l'oublie le bug revient à l'identique, sans rien pour le signaler. Le
    correctif doit tenir sans discipline humaine.

    Pourquoi ici plutôt qu'un middleware : les réponses d'`/api/*` ne doivent
    pas être touchées par une décision qui ne concerne que des fichiers.
    """

    def file_response(self, *args, **kwargs):
        reponse = super().file_response(*args, **kwargs)
        # Posé APRÈS l'appel parent, donc valable aussi bien sur la réponse
        # 200 que sur la 304 que `StaticFiles` fabrique quand le fichier n'a
        # pas changé.
        reponse.headers["Cache-Control"] = "no-cache"
        return reponse


def creer_app(db_path: Optional[str] = None):
    """L'API de la Phase 2, plus l'interface servie à la racine.

    `db_path` est transmis tel quel : la couche de présentation ne choisit
    jamais la base, elle ne fait que relayer ce que le lanceur lui donne."""
    app = creer_api(db_path)
    # `html=True` sert `index.html` sur « / » et sur tout répertoire.
    app.mount("/", StatiquesRevalidees(directory=str(STATIQUES), html=True),
              name="ui")
    return app


#: L'application de production. `uvicorn src.analytics_ui.app:app`.
app = creer_app()
