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
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from ..analytics import Filtres, analyser, detail
from ..analytics.filtres import (PREFIXE_COTE_SPORT, PREFIXE_EV_MAX_COTE,
                                PREFIXE_EV_MAX_SPORT, PREFIXE_EV_MIN_COTE,
                                PREFIXE_EV_MIN_SPORT, PREFIXE_EV_SPORT,
                                FiltreInvalide)
from ..analytics.perimetre import (BORNES_EV, MARCHES_ANALYTICS, MIN_SEGMENT,
                                   SPORTS_ANALYTICS)
from ..analytics.service import (DB_DEFAUT, GRANULARITES, meilleurs_segments,
                                 valeurs_disponibles)

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

#: Le texte d'aide des bandes d'EV, construit DEPUIS les bandes réelles. Une
#: liste recopiée dans une chaîne de documentation finirait par mentir le jour
#: où le moteur en ajoute une.
AIDE_EV = ("Bandes d'EV en UNION (OR). Répétable, ou séparé par des virgules. "
           "Valeurs : " + ", ".join(BORNES_EV) + ".")

#: ⚠️ DÉCLARÉS UN PAR UN, ET PAS EN GÉNÉRIQUE. Le périmètre Analytics ne
#: contient que deux sports ; les déclarer explicitement les fait apparaître
#: dans OpenAPI avec leur type et leur aide, au lieu d'être des paramètres
#: fantômes que seule la documentation mentionne. Le balayage générique de
#: `_ev_par_sport` reste là pour qu'un troisième sport ne soit jamais IGNORÉ
#: en silence s'il entrait dans le périmètre avant cette liste.
PARAMS_EV_SPORT = tuple(PREFIXE_EV_SPORT + s for s in SPORTS_ANALYTICS)


#: Idem pour les bandes de COTE.
PARAMS_COTE_SPORT = tuple(PREFIXE_COTE_SPORT + s for s in SPORTS_ANALYTICS)


def _openapi_par_sport(params, prefixe: str, valeurs, quoi: str) -> dict:
    """Décrit une famille de paramètres `<quoi>_<sport>` pour OpenAPI.

    Factorisé entre l'EV et la cote : deux générateurs jumeaux finiraient par
    documenter deux contrats différents pour un même comportement."""
    return {
        "parameters": [
            {
                "name": nom, "in": "query", "required": False,
                "schema": {"type": "array", "items": {"type": "string",
                                                      "enum": list(valeurs)}},
                "style": "form", "explode": True,
                "description": (
                    f"Bandes de {quoi} appliquées UNIQUEMENT au sport "
                    f"« {nom[len(prefixe):]} », en union. Prime sur la règle "
                    f"globale pour ce sport ; les autres sports la gardent. "
                    f"Appliqué DANS LE SQL, pas côté client."),
            }
            for nom in params
        ],
    }


def _openapi_cote_sport() -> dict:
    from ..analytics.perimetre import bornes_cote
    return _openapi_par_sport(PARAMS_COTE_SPORT, PREFIXE_COTE_SPORT,
                              bornes_cote(), "COTE")


def _openapi_ev_libre_cote() -> dict:
    """Décrit `ev_odds_min_<slug>` / `ev_odds_max_<slug>` pour OpenAPI.

    Les tranches sont nommées par leur SLUG et non par leur libellé : « > 6.0 »
    ne se met pas dans une URL sans encodage, et une URL illisible ne se
    partage pas. `perimetre.slugs_cote` refuse les collisions, donc ce nom
    désigne une tranche et une seule."""
    from ..analytics.perimetre import slugs_cote
    slugs = slugs_cote()
    return {
        "parameters": [
            {
                "name": prefixe + slug, "in": "query", "required": False,
                "schema": {"type": "number"},
                "description": (
                    f"Borne {'basse' if rang == 'min' else 'haute'} d'EV "
                    f"appliquée UNIQUEMENT aux paris de cote « {libelle} ». "
                    f"PRIME sur `ev_{rang}` et sur `ev_{rang}_<sport>` pour "
                    f"cette tranche — elle les REMPLACE, elle ne s'y ajoute "
                    f"pas. Les tranches non réglées gardent la règle de leur "
                    f"sport, puis la règle globale. Appliqué DANS LE SQL."),
            }
            for slug, libelle in slugs.items()
            for prefixe, rang in ((PREFIXE_EV_MIN_COTE, "min"),
                                  (PREFIXE_EV_MAX_COTE, "max"))
        ],
    }


def _openapi_paris() -> dict:
    """Décrit `outcomes` pour OpenAPI, avec son vocabulaire FERMÉ.

    Déclaré alors que `sports` et `markets` ne le sont pas : ceux-là ont un
    vocabulaire que `GET /api/filters` rend déjà, celui-ci a en plus une
    conséquence non évidente — ne cocher que du 1X2 écarte les totals — qui
    doit se lire là où l'on choisit le paramètre."""
    from ..analytics.perimetre import ALIAS_PARI, PARIS_ANALYTICS
    return {
        "parameters": [{
            "name": "outcomes", "in": "query", "required": False,
            "schema": {"type": "array",
                       "items": {"type": "string",
                                 "enum": list(PARIS_ANALYTICS)}},
            "style": "form", "explode": True,
            "description": (
                "Paris retenus, en union. Notation du coupon acceptée ("
                + ", ".join(sorted(ALIAS_PARI)) + "). ⚠️ Ne retenir que des "
                "paris 1X2 écarte les Over/Under, qui n'en sont pas — et "
                "réciproquement. Vide = tous."),
        }],
    }


def _openapi_familles_filtres() -> dict:
    """Les familles de filtres fins réunies : ce que les routes déclarent.

    Trois sont indexées par sport, la dernière par tranche de cote. Elles
    sont déclarées ensemble parce qu'une route qui en omettrait une aurait
    des paramètres fantômes — lus par le code, absents de la documentation."""
    return {"parameters": (_openapi_ev_sport()["parameters"]
                           + _openapi_cote_sport()["parameters"]
                           + _openapi_ev_libre_sport()["parameters"]
                           + _openapi_ev_libre_cote()["parameters"]
                           + _openapi_paris()["parameters"])}


def _openapi_ev_sport() -> dict:
    """Décrit les paramètres `ev_bands_<sport>` pour OpenAPI.

    Passe par `openapi_extra` plutôt que par des arguments de fonction
    inutilisés : un paramètre déclaré et jamais lu finit toujours par diverger
    du code qui le lit vraiment. Ici la documentation est fabriquée depuis
    `SPORTS_ANALYTICS`, la même constante que le périmètre."""
    return {
        "parameters": [
            {
                "name": nom, "in": "query", "required": False,
                "schema": {"type": "array", "items": {"type": "string",
                                                      "enum": list(BORNES_EV)}},
                "style": "form", "explode": True,
                "description": (
                    f"Bandes d'EV appliquées UNIQUEMENT au sport "
                    f"« {nom[len(PREFIXE_EV_SPORT):]} », en union. Prime sur "
                    f"`ev_bands` pour ce sport ; les autres sports gardent la "
                    f"règle globale. Appliqué DANS LE SQL, pas côté client."),
            }
            for nom in PARAMS_EV_SPORT
        ],
    }


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


def _bandes_par_sport(request: Request, prefixe: str, exclue: str) -> dict:
    """Toutes les règles par sport d'une famille, lues dans la query string.

    Balaye les clés plutôt que de lire une liste figée : un paramètre pour un
    sport qui entrerait demain dans le périmètre doit être APPLIQUÉ, ou refusé
    par la validation — jamais ignoré sans un mot. Un filtre qu'on croit posé
    et qui ne l'est pas est exactement le mode de panne que toute cette couche
    existe pour empêcher."""
    out: dict = {}
    for cle in request.query_params.keys():
        if not cle.startswith(prefixe) or cle == exclue:
            continue
        sport = cle[len(prefixe):].strip().lower()
        if sport:
            out[sport] = _liste_multi(request, cle)
    return out


def _ev_libre_par_sport(request: Request) -> dict:
    """Les BORNES LIBRES d'EV par sport présentes dans la query string.

    ⚠️ `ev_min` et `ev_max` NUS ne sont pas lus ici : ce sont les bornes
    GLOBALES. Les prendre pour un sport dont le nom serait vide les ferait
    disparaître de la règle globale — un filtre qui se déplace tout seul est
    pire qu'un filtre absent."""
    out: dict = {}
    for cle in request.query_params.keys():
        for prefixe, rang in ((PREFIXE_EV_MIN_SPORT, 0),
                              (PREFIXE_EV_MAX_SPORT, 1)):
            if not cle.startswith(prefixe) or len(cle) == len(prefixe):
                continue
            sport = cle[len(prefixe):].strip().lower()
            if not sport:
                continue
            courant = list(out.get(sport, (None, None)))
            courant[rang] = request.query_params.get(cle)
            out[sport] = tuple(courant)
    return out


def _ev_libre_par_cote(request: Request) -> dict:
    """Les BORNES LIBRES d'EV par tranche de cote présentes dans la requête.

    Jumeau de `_ev_libre_par_sport`, sur des clés qui ne peuvent PAS se
    confondre avec les siennes : `ev_odds_min_…` ne commence pas par
    `ev_min_`. Si un jour l'un devenait préfixe de l'autre, ce balayage
    volerait ses paramètres au voisin sans rien signaler."""
    out: dict = {}
    for cle in request.query_params.keys():
        for prefixe, rang in ((PREFIXE_EV_MIN_COTE, 0),
                              (PREFIXE_EV_MAX_COTE, 1)):
            if not cle.startswith(prefixe) or len(cle) == len(prefixe):
                continue
            bande = cle[len(prefixe):].strip()
            if not bande:
                continue
            courant = list(out.get(bande, (None, None)))
            courant[rang] = request.query_params.get(cle)
            out[bande] = tuple(courant)
    return out


def _openapi_ev_libre_sport() -> dict:
    """Décrit `ev_min_<sport>` / `ev_max_<sport>` pour OpenAPI."""
    return {
        "parameters": [
            {
                "name": prefixe + sport, "in": "query", "required": False,
                "schema": {"type": "number"},
                "description": (
                    f"Borne {'basse' if rang == 'min' else 'haute'} d'EV "
                    f"appliquée UNIQUEMENT au sport « {sport} ». Prime sur "
                    f"`ev_{rang}` pour ce sport ; les autres sports gardent la "
                    f"borne globale. Se cumule en ET avec les bandes du même "
                    f"sport. Appliqué DANS LE SQL."),
            }
            for sport in SPORTS_ANALYTICS
            for prefixe, rang in ((PREFIXE_EV_MIN_SPORT, "min"),
                                  (PREFIXE_EV_MAX_SPORT, "max"))
        ],
    }


def _cote_par_sport(request: Request) -> dict:
    """Les bandes de COTE par sport présentes dans la query string."""
    return _bandes_par_sport(request, PREFIXE_COTE_SPORT, "odds_bands_by_sport")


def _ev_par_sport(request: Request) -> dict:
    """Toutes les règles d'EV par sport présentes dans la query string.

    Balaye les clés plutôt que de lire une liste figée : un paramètre
    `ev_bands_<sport>` pour un sport qui entrerait demain dans le périmètre
    doit être APPLIQUÉ, ou refusé par la validation — jamais ignoré sans un
    mot. Un filtre qu'on croit posé et qui ne l'est pas est exactement le mode
    de panne que toute cette couche existe pour empêcher."""
    return _bandes_par_sport(request, PREFIXE_EV_SPORT, "ev_bands_by_sport")


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
        brut["outcomes"] = (_liste_multi(request, "outcome", "outcomes",
                                         "outcomes[]") or None)
        brut["leagues"] = (_liste_multi(request, "league", "leagues",
                                        "leagues[]", decouper=False) or None)
        brut["ev_bands"] = _liste_multi(request, "ev_band", "ev_bands",
                                        "ev_bands[]") or None
        regles = _ev_par_sport(request)
        if regles:
            brut["ev_bands_by_sport"] = regles
        brut["odds_bands"] = _liste_multi(request, "odds_band", "odds_bands",
                                          "odds_bands[]") or None
        regles_cote = _cote_par_sport(request)
        if regles_cote:
            brut["odds_bands_by_sport"] = regles_cote
        libres = _ev_libre_par_sport(request)
        if libres:
            brut["ev_free_by_sport"] = libres
        libres_cote = _ev_libre_par_cote(request)
        if libres_cote:
            brut["ev_free_by_odds"] = libres_cote
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

    @app.get("/api/analyse", dependencies=[Depends(exiger_acces)],
             openapi_extra=_openapi_familles_filtres())
    def analyse(
        request: Request,
        ev_bands: Optional[List[str]] = Query(None, description=AIDE_EV),
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
        """L'analyse complète : résumé, avertissements, découpes, matrice.

        PÉRIMÈTRE — restreint aux sports et marchés que `GET /api/filters`
        rend sous la clé `perimetre`, avec la raison. Les autres restent en
        base mais ne sont jamais analysés : aucune source de résultats pour
        les sports écartés, aucun règlement possible pour les marchés écartés.

        EV — trois mécanismes qui se CUMULENT en ET :
        `ev_min`/`ev_max` (bornes libres), `ev_bands` (union de bandes), et
        `ev_bands_<sport>` (union de bandes propre à un sport, qui remplace
        `ev_bands` pour CE sport seulement). Les trois sont appliqués dans le
        SQL — rien n'est filtré après coup.
        """
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

    @app.get("/api/detail", dependencies=[Depends(exiger_acces)],
             openapi_extra=_openapi_familles_filtres())
    def detail_route(
        request: Request,
        ev_bands: Optional[List[str]] = Query(None, description=AIDE_EV),
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

    @app.get("/api/segments", dependencies=[Depends(exiger_acces)],
             openapi_extra=_openapi_familles_filtres())
    def segments_route(
        request: Request,
        ev_bands: Optional[List[str]] = Query(None, description=AIDE_EV),
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
        min_n: int = Query(MIN_SEGMENT, ge=1,
                           description="Plancher d'opportunités par segment."),
        sort: str = Query("clv", description="clv (recommandé) | roi"),
        depth: int = Query(3, ge=1, le=3,
                           description="Nombre de dimensions croisées."),
        limit: int = Query(25, ge=1, le=100),
    ):
        """Les combinaisons qui ressortent — et de quoi s'en méfier.

        ⚠️ ENDPOINT SÉPARÉ, ET C'EST DÉLIBÉRÉ. Le croisement de trois
        dimensions coûte nettement plus cher qu'une analyse ; l'agréger à
        `/api/analyse` ralentirait chaque affichage de la page pour une
        section que l'utilisateur ne consulte pas à chaque fois.

        La réponse porte TOUJOURS `combinaisons_testees` et ses
        avertissements. Un classement de segments lu sans le nombre de tests
        qui l'a produit n'est pas une information, c'est une illusion
        d'optique — et le premier du classement est la combinaison la plus
        chanceuse autant que la meilleure.
        """
        if sort not in ("clv", "roi"):
            raise FiltreInvalide(
                f"sort doit valoir clv ou roi pour les segments — "
                f"reçu : {sort!r}")
        f = _filtres(
            request, odds_min=odds_min, odds_max=odds_max, ev_min=ev_min,
            ev_max=ev_max, date_from=date_from, date_to=date_to,
            delay_min=delay_min, delay_max=delay_max, population=population,
            played=played, canal=canal, dead_window_min=dead_window_min,
            stake=stake)
        return meilleurs_segments(base(), f, min_n=min_n, trier_par=sort,
                                  limite=limit, profondeur=depth)

    return app


#: L'application de production. `uvicorn src.analytics_api.app:app`.
app = creer_app()
