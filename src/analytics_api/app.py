"""L'API Analytics — une traduction HTTP, et rien d'autre.

CE QUE CETTE COUCHE NE FAIT PAS
-------------------------------
Elle ne calcule aucune métrique, n'écrit aucun SQL, ne connaît ni la CLV ni le
ROI ni la déduplication. Elle appelle `src.analytics` et sérialise le
résultat. Si un jour du calcul apparaît ici, c'est qu'une seconde définition
est en train de naître — c'est le §17.7, et ce projet l'a payé trois fois.

TROIS PROPRIÉTÉS DE SÛRETÉ, TENUES PAR DES TESTS
------------------------------------------------
1. **Le chemin de la base n'est JAMAIS un paramètre de requête.** Il vient de
   la configuration du serveur. Le laisser choisir au client ouvrirait une
   traversée de répertoires : `?db=/etc/passwd` ou, pire, une base que
   l'attaquant contrôle.
2. **La base est ouverte en lecture seule**, garanti par `mode=ro` dans
   `src.analytics.service` — la garantie vient de SQLite, pas de cette couche.
3. **Aucune valeur utilisateur n'atteint une chaîne SQL.** La validation a
   lieu dans `Filtres.valider()`, déjà couverte par 58 tests, et le SQL n'est
   construit qu'avec des marqueurs `?`.

L'AUTHENTIFICATION EST UN TROU LAISSÉ EXPRÈS
--------------------------------------------
`exiger_acces` est une dépendance FastAPI posée sur chaque route et qui, pour
l'instant, ne fait rien. C'est le point d'ancrage unique : le jour où l'API
sortira de `127.0.0.1`, c'est cette fonction-là qui portera la vérification,
et aucune route n'aura besoin d'être modifiée. Aucun secret, aucun mot de
passe, aucun jeton n'est écrit dans ce fichier — ils viendront de
l'environnement.
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..analytics import Filtres, analyser, detail
from ..analytics.filtres import FiltreInvalide
from ..analytics.service import DB_DEFAUT, GRANULARITES, valeurs_disponibles

#: Le chemin de la base, LU DANS L'ENVIRONNEMENT et jamais dans la requête.
VAR_DB = "ANALYTICS_DB"

#: Origines autorisées pour le navigateur, séparées par des virgules. Vide =
#: middleware non installé, donc aucun changement de comportement. Prévu pour
#: le jour où une page servie ailleurs appellera cette API.
VAR_CORS = "ANALYTICS_CORS_ORIGINS"

#: Plafond de pagination. Le service en impose déjà un ; celui-ci refuse la
#: demande AVANT, pour que l'utilisateur voie son erreur au lieu d'obtenir
#: silencieusement autre chose que ce qu'il a demandé.
PAR_PAGE_MAX = 500

TRIS = ("detected_at", "odd_taken", "ev_pct", "clv", "pnl", "sport", "book",
        "start_time")


async def exiger_acces(request: Request) -> None:
    """Le point d'ancrage de l'authentification. NE FAIT RIEN AUJOURD'HUI.

    Posé sur chaque route dès maintenant pour que l'ajout d'une
    authentification soit un changement d'UNE fonction, et non une revue de
    toutes les routes en espérant n'en oublier aucune — c'est exactement
    l'oubli qui produit une route ouverte en production.

    ⚠️ Tant que cette fonction est vide, l'API n'a AUCUN contrôle d'accès et
    ne doit écouter que sur l'interface de bouclage. `scripts/analytics_serve`
    le fait respecter.
    """
    return None


def _liste_multi(request: Request, *noms: str, decouper: bool = True) -> list:
    """Les valeurs d'un paramètre répétable, sous ses différentes écritures.

    `sports=a&sports=b`, `sports[]=a&sports[]=b` et `sports=a,b` arrivent tous
    les trois pour de vrai — une query string écrite à la main, un formulaire
    HTML, un client JS. En refuser une serait un piège sans contrepartie.

    ⚠️ LE DÉCOUPAGE PAR VIRGULES SE FAIT ICI, ET PAS DANS `Filtres`. Mesuré en
    écrivant les tests : `Filtres._liste` ne découpe que si on lui passe une
    CHAÎNE nue, alors que `getlist` rend toujours une LISTE. Un
    `?sports=soccer,tennis` ressortait donc en un seul sport nommé
    « soccer,tennis », qui ne rapproche rien — et rendait zéro opportunité
    sous un en-tête parfaitement normal, exactement le §11.

    ⚠️ ET IL NE S'APPLIQUE PAS AUX LIGUES. Les sports, books et marchés sont
    des identifiants sans espace ni ponctuation ; un nom de compétition, lui,
    peut contenir une virgule. Y découper à l'aveugle casserait un filtre
    légitime pour faire marcher une commodité — d'où `decouper=False` sur ce
    seul champ, où seule la répétition du paramètre est acceptée."""
    out: list = []
    for nom in noms:
        for brut in request.query_params.getlist(nom):
            if decouper:
                out.extend(m.strip() for m in brut.split(",") if m.strip())
            elif brut.strip():
                out.append(brut.strip())
    return out


def creer_app(db_path: Optional[str] = None) -> FastAPI:
    """Fabrique l'application. `db_path` explicite pour les tests.

    Fabrique plutôt qu'application globale : un test doit pouvoir viser sa
    propre base sans variable d'environnement ni état partagé entre deux
    tests. La production passe par `ANALYTICS_DB`."""
    app = FastAPI(
        title="Valuebet Analytics",
        version="1.0.0",
        description="Analyses historiques sur les détections du moteur "
                    "Valuebet. Lecture seule.",
    )

    def base() -> str:
        # Résolu à CHAQUE requête et non à la fabrication : la variable
        # d'environnement peut changer entre deux redémarrages, et un test
        # doit pouvoir la poser après avoir créé l'app.
        return db_path or os.getenv(VAR_DB) or DB_DEFAUT

    origines = [o.strip() for o in os.getenv(VAR_CORS, "").split(",") if o.strip()]
    if origines:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(
            CORSMiddleware, allow_origins=origines,
            allow_methods=["GET"], allow_headers=["*"])

    # ── Erreurs : un message qui dit quoi corriger ───────────────────

    @app.exception_handler(FiltreInvalide)
    async def _filtre_invalide(request: Request, exc: FiltreInvalide):
        """400 et non 500 : une borne à l'envers est une faute de saisie. La
        traiter comme une panne interne cacherait à l'utilisateur ce qu'il
        doit changer."""
        return JSONResponse(status_code=400,
                            content={"error": "filtre_invalide",
                                     "detail": str(exc)})

    @app.exception_handler(FileNotFoundError)
    async def _base_absente(request: Request, exc: FileNotFoundError):
        """503 : le serveur est mal configuré, l'utilisateur n'y peut rien.
        Un 404 laisserait croire que sa requête est en cause."""
        return JSONResponse(status_code=503,
                            content={"error": "base_indisponible",
                                     "detail": str(exc)})

    @app.exception_handler(ValueError)
    async def _valeur_invalide(request: Request, exc: ValueError):
        """Attrape notamment `Population("inventee")`, qui lève une ValueError
        nue depuis l'énumération."""
        return JSONResponse(status_code=400,
                            content={"error": "parametre_invalide",
                                     "detail": str(exc)})

    # ── Construction des filtres, EN UN SEUL ENDROIT ─────────────────

    def _filtres(request: Request, **scalaires) -> Filtres:
        """Traduit la requête en `Filtres`. Le nom des champs n'est mappé
        qu'ICI et dans `Filtres.depuis_dict` — nulle part ailleurs."""
        brut = dict(scalaires)
        brut["sports"] = (_liste_multi(request, "sport", "sports", "sports[]")
                          or None)
        brut["bookmakers"] = (_liste_multi(request, "bookmaker", "bookmakers",
                                           "bookmakers[]", "books") or None)
        brut["markets"] = (_liste_multi(request, "market", "markets",
                                        "markets[]") or None)
        brut["leagues"] = (_liste_multi(request, "league", "leagues",
                                        "leagues[]", decouper=False) or None)
        return Filtres.depuis_dict(brut).valider()

    # ── Les routes ───────────────────────────────────────────────────

    @app.get("/api/health", dependencies=[Depends(exiger_acces)])
    def health():
        """Vivant, et sur quelle base. Sert au reverse proxy et au diagnostic.

        Rend le NOM du fichier, jamais son chemin absolu : celui-ci décrit
        l'arborescence du serveur et n'apprend rien d'utile à un client."""
        chemin = base()
        return {"status": "ok", "database": os.path.basename(chemin),
                "read_only": True}

    @app.get("/api/filters", dependencies=[Depends(exiger_acces)])
    def filters():
        """De quoi peupler les listes déroulantes, LU dans la base."""
        return valeurs_disponibles(base())

    @app.get("/api/analyse", dependencies=[Depends(exiger_acces)])
    def analyse(
        request: Request,
        odds_min: Optional[float] = Query(None, description="Cote minimum (> 1)."),
        odds_max: Optional[float] = Query(None, description="Cote maximum."),
        ev_min: Optional[float] = Query(None, description="EV minimum en %."),
        ev_max: Optional[float] = Query(None, description="EV maximum en %."),
        date_from: Optional[str] = Query(None, description="AAAA-MM-JJ inclus."),
        date_to: Optional[str] = Query(None, description="AAAA-MM-JJ INCLUS."),
        delay_min: Optional[float] = Query(None, description="Heures avant le coup d'envoi."),
        delay_max: Optional[float] = Query(None),
        population: str = Query("detected"),
        played: str = Query("tous", description="tous | oui | non"),
        canal: Optional[str] = Query(None, description="Canal pour ELIGIBLE_FOR_ALERT."),
        dead_window_min: Optional[float] = Query(None),
        stake: float = Query(25.0, description="Mise notionnelle par pari."),
        granularite: str = Query("semaine", description="jour | semaine | mois"),
    ):
        """L'analyse complète : résumé, avertissements, découpes, matrice."""
        if granularite not in GRANULARITES:
            raise FiltreInvalide(
                f"granularite doit valoir {' | '.join(GRANULARITES)} — "
                f"reçu : {granularite!r}")
        f = _filtres(
            request, odds_min=odds_min, odds_max=odds_max, ev_min=ev_min,
            ev_max=ev_max, date_from=date_from, date_to=date_to,
            delay_min=delay_min, delay_max=delay_max, population=population,
            played=played, canal=canal, dead_window_min=dead_window_min,
            stake=stake)
        return analyser(base(), f, granularite=granularite)

    @app.get("/api/detail", dependencies=[Depends(exiger_acces)])
    def detail_route(
        request: Request,
        odds_min: Optional[float] = Query(None),
        odds_max: Optional[float] = Query(None),
        ev_min: Optional[float] = Query(None),
        ev_max: Optional[float] = Query(None),
        date_from: Optional[str] = Query(None),
        date_to: Optional[str] = Query(None),
        delay_min: Optional[float] = Query(None),
        delay_max: Optional[float] = Query(None),
        population: str = Query("detected"),
        played: str = Query("tous"),
        canal: Optional[str] = Query(None),
        dead_window_min: Optional[float] = Query(None),
        stake: float = Query(25.0),
        page: int = Query(1, ge=1),
        per_page: int = Query(50, ge=1),
        sort: str = Query("detected_at"),
        order: str = Query("desc", description="asc | desc"),
    ):
        """Les opportunités une par une, paginées côté SERVEUR.

        ⚠️ Le plafond est refusé explicitement plutôt que rogné en silence :
        demander 100 000 lignes et en recevoir 500 sans le savoir ferait
        croire à un jeu de données tronqué."""
        if per_page > PAR_PAGE_MAX:
            raise FiltreInvalide(
                f"per_page est plafonné à {PAR_PAGE_MAX} — reçu : {per_page}. "
                f"Pagine plutôt que de tout demander d'un coup.")
        if sort not in TRIS:
            raise FiltreInvalide(
                f"sort doit valoir l'un de : {', '.join(TRIS)} — "
                f"reçu : {sort!r}")
        if order.lower() not in ("asc", "desc"):
            raise FiltreInvalide(
                f"order doit valoir asc ou desc — reçu : {order!r}")
        f = _filtres(
            request, odds_min=odds_min, odds_max=odds_max, ev_min=ev_min,
            ev_max=ev_max, date_from=date_from, date_to=date_to,
            delay_min=delay_min, delay_max=delay_max, population=population,
            played=played, canal=canal, dead_window_min=dead_window_min,
            stake=stake)
        return detail(base(), f, page=page, par_page=per_page,
                      tri=sort, ordre=order)

    return app


#: L'application de production. `uvicorn src.analytics_api.app:app`.
app = creer_app()
