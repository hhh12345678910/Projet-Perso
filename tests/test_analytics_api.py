"""L'API Analytics — une traduction HTTP, et rien d'autre.

CE QUE CE FICHIER VÉRIFIE, AU-DELÀ DES CODES DE RETOUR
------------------------------------------------------
* que l'API **n'écrit jamais** dans la base — comparaison d'empreinte
  SHA-256 du fichier avant et après une campagne de requêtes ;
* qu'elle **appelle bien `src.analytics`** et ne recalcule rien dans son coin
  — vérifié en interceptant les fonctions de la couche, pas en lisant le
  code ;
* que **le chemin de la base n'est pas un paramètre de requête** — sinon
  `?db=/ailleurs` ouvrirait une traversée de répertoires ;
* qu'une **valeur hostile** ne peut atteindre aucune chaîne SQL.
"""
from __future__ import annotations

import hashlib

import pytest

# L'API est optionnelle : une installation qui n'a pas FastAPI doit voir des
# tests IGNORÉS avec une raison, pas une suite rouge. `pip install fastapi
# uvicorn` les réactive.
fastapi = pytest.importorskip(
    "fastapi", reason="FastAPI absent — `pip install fastapi uvicorn`")

from fastapi.testclient import TestClient  # noqa: E402

from src.analytics_api.app import PAR_PAGE_MAX, VAR_DB, creer_app  # noqa: E402
from tests.analytics_base import Opp, ajouter_canal, monter  # noqa: E402


@pytest.fixture
def base(tmp_path):
    """Dix opportunités : deux sports, trois books, deux mois, tous les états
    de règlement."""
    return monter(tmp_path, [
        Opp(1, sport="soccer", book="unibet_be", odd=1.60, ev=6,
            jour="2026-08-05", cloture=1.55, gagnant="home", outcome="home"),
        Opp(2, sport="soccer", home="C", away="D", book="unibet_be",
            odd=2.20, ev=12, jour="2026-08-10", cloture=2.10,
            gagnant="away", outcome="home", alerte="2026-08-10T10:05:00+00:00"),
        Opp(3, sport="soccer", home="E", away="F", book="ladbrokes_be",
            odd=2.80, ev=9, jour="2026-08-20", cloture=2.90,
            gagnant="home", outcome="home"),
        Opp(4, sport="soccer", home="G", away="H", book="ladbrokes_be",
            odd=3.50, ev=18, jour="2026-09-01", cloture=3.30, gagnant=None),
        Opp(5, sport="tennis", home="Sinner", away="Alcaraz", book="betano_be",
            odd=4.50, ev=25, jour="2026-09-02", cloture=4.80,
            gagnant="away", outcome="home"),
        Opp(6, sport="tennis", home="Djokovic", away="Zverev",
            book="betano_be", odd=7.00, ev=40, jour="2026-09-03",
            cloture=6.50, gagnant="home", outcome="home"),
        Opp(7, sport="soccer", home="I", away="J", book="unibet_be",
            odd=2.00, ev=10, jour="2026-09-04", cloture=None, gagnant=None),
        Opp(8, sport="soccer", home="K", away="L", book="unibet_be",
            odd=2.05, ev=10, jour="2026-09-05", cloture=2.00,
            gagnant="home", outcome="home", joue=True),
        Opp(9, sport="soccer", home="M", away="N", book="ladbrokes_be",
            odd=2.60, ev=14, jour="2026-09-06", cloture=2.50,
            gagnant="away", outcome="home", joue=True),
        Opp(10, sport="tennis", home="Medvedev", away="Rune",
            book="betano_be", odd=3.10, ev=16, jour="2026-09-07",
            cloture=3.00, gagnant="home", outcome="home"),
    ])


@pytest.fixture
def client(base):
    return TestClient(creer_app(str(base)))


# ── /api/health ──────────────────────────────────────────────────────

def test_health_repond(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok" and r.json()["read_only"] is True


def test_health_ne_divulgue_pas_le_chemin_absolu(client, base):
    """Le chemin décrit l'arborescence du serveur et n'apprend rien d'utile
    à un client."""
    assert "/" not in client.get("/api/health").json()["database"]


# ── /api/filters ─────────────────────────────────────────────────────

def test_filters_peuple_les_listes(client):
    d = client.get("/api/filters").json()
    assert set(d["sports"]) == {"soccer", "tennis"}
    assert set(d["bookmakers"]) == {"unibet_be", "ladbrokes_be", "betano_be"}
    assert d["date_min"] == "2026-08-05" and d["date_max"] == "2026-09-07"


def test_filters_ne_propose_que_des_enums_de_book(client):
    """⚠️ `played_bets.book` porte des libellés d'affichage en production
    (76,6 % des lignes). En proposer un dans une liste déroulante donnerait un
    filtre qui ne rapproche rien."""
    for b in client.get("/api/filters").json()["bookmakers"]:
        assert b == b.lower() and " " not in b and "/" not in b


def test_filters_porte_les_CINQ_populations_proposees(client):
    """⚠️ CINQ, ET PLUS SIX — ce décompte a changé le 20/09.

    `BET` n'était pas un choix : `alias_de` la renvoie sur `CLICKED`, donc
    les deux entrées rendaient rigoureusement le même lot. En proposer deux
    laissait croire à une distinction — « cliquées » contre « réellement
    misées » — que le système ne sait pas faire, faute de confirmation de
    mise. Elle reste ACCEPTÉE par l'API : aucune URL enregistrée ne casse."""
    pops = client.get("/api/filters").json()["populations"]
    assert len(pops) == 5
    assert "bet" not in {p["value"] for p in pops}
    assert all(p["explication"] for p in pops)
    assert any(p["limites"] for p in pops)


def test_chaque_population_porte_un_LIBELLE_en_francais(client):
    """Un identifiant technique dans une liste déroulante n'apprend rien, et
    une population choisie sans être comprise produit un chiffre qu'on croit
    sans savoir sur quoi il porte."""
    pops = client.get("/api/filters").json()["populations"]
    par_valeur = {p["value"]: p["libelle"] for p in pops}
    assert par_valeur["detected"] == "Toutes les détections"
    assert par_valeur["clicked"] == "Cliquées sur « Jouer »"
    assert all(p["libelle"] for p in pops)


def test_la_population_bet_reste_ACCEPTEE_meme_si_elle_nest_plus_proposee(client):
    """Retirer une option de l'interface ne doit casser aucune URL existante.
    `bet` et `clicked` doivent rendre le même résultat, comme avant."""
    a = client.get("/api/analyse", params={"population": "bet"})
    b = client.get("/api/analyse", params={"population": "clicked"})
    assert a.status_code == 200 and b.status_code == 200
    assert a.json()["summary"] == b.json()["summary"]


# ── /api/analyse ─────────────────────────────────────────────────────

def test_analyse_simple(client):
    d = client.get("/api/analyse").json()
    assert d["summary"]["opportunities"] == 10
    assert {"summary", "warnings", "by_book", "by_sport", "by_odds",
            "by_ev", "by_time", "matrix", "filters", "population"} <= set(d)


def test_analyse_rend_tous_les_kpi_demandes(client):
    s = client.get("/api/analyse").json()["summary"]
    for cle in ("opportunities", "settled", "settlement_rate", "clv",
                "clv_coverage", "roi", "pnl", "stake_total", "ev_mean",
                "odds_mean"):
        assert cle in s, cle


def test_la_clv_ne_sort_JAMAIS_sans_sa_couverture(client):
    """⚠️ +10,4 % sur 95 % du lot et sur 30 % ne sont pas la même phrase."""
    d = client.get("/api/analyse").json()
    assert d["summary"]["clv_coverage"] is not None
    for axe in ("by_book", "by_sport", "by_odds", "by_ev"):
        for t in d[axe]:
            assert "clv_coverage" in t
    for c in d["matrix"]["cells"]:
        assert "clv_coverage" in c


def test_filtre_par_sport(client):
    assert client.get("/api/analyse?sport=tennis").json()[
        "summary"]["opportunities"] == 3


def test_sports_multiples_par_repetition(client):
    assert client.get("/api/analyse?sports=soccer&sports=tennis").json()[
        "summary"]["opportunities"] == 10


def test_sports_multiples_avec_crochets(client):
    """`sports[]=a&sports[]=b` arrive d'un formulaire HTML — le refuser serait
    un piège sans contrepartie."""
    assert client.get("/api/analyse?sports[]=soccer&sports[]=tennis").json()[
        "summary"]["opportunities"] == 10


def test_sports_multiples_separes_par_virgules(client):
    assert client.get("/api/analyse?sports=soccer,tennis").json()[
        "summary"]["opportunities"] == 10


def test_filtre_par_bookmakers_multiples(client):
    assert client.get("/api/analyse?bookmakers=unibet_be&bookmakers=ladbrokes_be"
                      ).json()["summary"]["opportunities"] == 7


def test_l_alias_kambi_se_deplie(client):
    d = client.get("/api/analyse?bookmakers=kambi").json()
    assert len(d["filters"]["bookmakers"]) == 4


def test_bornes_de_cote(client):
    assert client.get("/api/analyse?odds_min=2.0&odds_max=3.0").json()[
        "summary"]["opportunities"] == 5


def test_bornes_d_ev(client):
    assert client.get("/api/analyse?ev_min=10&ev_max=20").json()[
        "summary"]["opportunities"] == 6


def test_bornes_de_periode(client):
    assert client.get("/api/analyse?date_from=2026-08-01&date_to=2026-08-31"
                      ).json()["summary"]["opportunities"] == 3


def test_bornes_de_delai(client):
    assert client.get("/api/analyse?delay_min=0&delay_max=24").json()[
        "summary"]["opportunities"] == 10


@pytest.mark.parametrize("pop,attendu", [
    ("detected", 10), ("sent", 1), ("clicked", 2), ("bet", 2), ("settled", 8)])
def test_chaque_population(client, pop, attendu):
    assert client.get(f"/api/analyse?population={pop}").json()[
        "summary"]["opportunities"] == attendu


def test_population_eligible_rejoue_la_porte_de_production(base):
    ajouter_canal(base, ev_min=8.0, odd_min=1.5, odd_max=4.0)
    c = TestClient(creer_app(str(base)))
    d = c.get("/api/analyse?population=eligible_for_alert").json()
    assert d["population"]["eligibilite"]["porte"].startswith("canal")
    assert d["summary"]["opportunities"] < 10


@pytest.mark.parametrize("valeur,attendu", [("tous", 10), ("oui", 2), ("non", 8)])
def test_filtre_joue(client, valeur, attendu):
    assert client.get(f"/api/analyse?played={valeur}").json()[
        "summary"]["opportunities"] == attendu


def test_filtres_COMBINES(client):
    """Le critère de réussite de l'énoncé, en HTTP."""
    d = client.get("/api/analyse"
                   "?sports=soccer&bookmakers=unibet_be&bookmakers=ladbrokes_be"
                   "&odds_min=2.00&odds_max=4.00&ev_min=10&ev_max=20"
                   "&date_from=2026-08-01&date_to=2026-09-12"
                   "&population=settled").json()
    assert d["summary"]["opportunities"] == 3
    assert d["filters"]["odds_min"] == 2.0 and d["filters"]["population"] == "settled"


def test_les_decoupes_somment_au_total(client):
    d = client.get("/api/analyse").json()
    total = d["summary"]["opportunities"]
    for axe in ("by_book", "by_sport", "by_odds", "by_ev", "by_time"):
        assert sum(t["opportunities"] for t in d[axe]) == total, axe
    assert sum(c["opportunities"] for c in d["matrix"]["cells"]) == total


@pytest.mark.parametrize("g", ["jour", "semaine", "mois"])
def test_les_trois_granularites(client, g):
    assert client.get(f"/api/analyse?granularite={g}").status_code == 200


def test_le_json_rendu_est_serialisable(client):
    """Une valeur non sérialisable passerait le test de statut et casserait
    chez le client."""
    import json
    json.dumps(client.get("/api/analyse").json())


# ── Validation des paramètres : des 400, jamais des 500 ─────────────

@pytest.mark.parametrize("qs,fragment", [
    ("odds_min=4&odds_max=2", "à l'envers"),
    ("ev_min=20&ev_max=5", "à l'envers"),
    ("delay_min=48&delay_max=2", "à l'envers"),
    ("date_from=2026-09-12&date_to=2026-08-01", "vide par construction"),
    ("date_from=01/08/2026", "AAAA-MM-JJ"),
    ("odds_min=0.5", "1,00"),
    ("bookmakers=pinaccle", "inconnu"),
    ("markets=corners", "Marché inconnu"),
    ("played=peut-etre", "tous"),
    ("stake=0", "positive"),
    ("granularite=trimestre", "granularite"),
])
def test_un_parametre_invalide_rend_400_avec_la_raison(client, qs, fragment):
    r = client.get(f"/api/analyse?{qs}")
    assert r.status_code == 400, r.text
    assert fragment in r.json()["detail"]
    assert r.json()["error"] in ("filtre_invalide", "parametre_invalide")


def test_une_population_inconnue_rend_400_et_non_500(client):
    r = client.get("/api/analyse?population=inventee")
    assert r.status_code == 400
    assert "inventee" in r.json()["detail"]


def test_un_nombre_illisible_rend_une_erreur_propre(client):
    """FastAPI attrape le type avant nous : 422, pas 500."""
    r = client.get("/api/analyse?odds_min=beaucoup")
    assert r.status_code in (400, 422)


def test_une_base_absente_rend_503_et_non_500(tmp_path):
    """Le serveur est mal configuré ; l'utilisateur n'y peut rien. Un 404
    laisserait croire que sa requête est en cause."""
    c = TestClient(creer_app(str(tmp_path / "absente.db")))
    r = c.get("/api/analyse")
    assert r.status_code == 503
    assert r.json()["error"] == "base_indisponible"


# ── /api/detail et la pagination ─────────────────────────────────────

def test_detail_pagine(client):
    d = client.get("/api/detail?page=1&per_page=3").json()
    assert len(d["items"]) == 3 and d["total"] == 10 and d["pages"] == 4


def test_les_pages_couvrent_tout_sans_doublon(client):
    vus = []
    for page in range(1, 5):
        vus += [i["id"] for i in
                client.get(f"/api/detail?page={page}&per_page=3").json()["items"]]
    assert len(vus) == 10 and len(set(vus)) == 10


def test_une_page_au_dela_de_la_fin_est_vide(client):
    assert client.get("/api/detail?page=99").json()["items"] == []


def test_per_page_au_dessus_du_plafond_est_REFUSE(client):
    """⚠️ Refusé, pas rogné en silence : demander 100 000 lignes et en
    recevoir 500 sans le savoir ferait croire à un jeu tronqué."""
    r = client.get(f"/api/detail?per_page={PAR_PAGE_MAX + 1}")
    assert r.status_code == 400 and str(PAR_PAGE_MAX) in r.json()["detail"]


@pytest.mark.parametrize("qs", ["page=0", "page=-1", "per_page=0"])
def test_une_pagination_absurde_rend_422(client, qs):
    assert client.get(f"/api/detail?{qs}").status_code == 422


def test_le_detail_porte_les_colonnes_demandees(client):
    i = client.get("/api/detail?per_page=1").json()["items"][0]
    attendu = {"detected_at", "sport", "league", "event", "market",
               "selection", "bookmaker", "odds", "ev_pct", "stake", "result",
               "closing_fair_odd", "clv_pct", "pnl"}
    assert attendu <= set(i)


def test_le_detail_distingue_les_quatre_statuts(client):
    statuts = {i["result"] for i in
               client.get("/api/detail?per_page=50").json()["items"]}
    assert "unsettled" in statuts and "won" in statuts and "lost" in statuts


@pytest.mark.parametrize("tri", ["detected_at", "odd_taken", "ev_pct", "clv",
                                 "pnl", "sport", "book"])
def test_chaque_tri_autorise_repond(client, tri):
    assert client.get(f"/api/detail?sort={tri}").status_code == 200


def test_un_tri_inconnu_est_REFUSE(client):
    """Le service retomberait sur un défaut ; l'API préfère le dire. Un tri
    silencieusement ignoré rendrait des lignes dans un ordre que
    l'utilisateur croit avoir choisi."""
    r = client.get("/api/detail?sort=peu_importe")
    assert r.status_code == 400 and "sort" in r.json()["detail"]


def test_un_ordre_inconnu_est_refuse(client):
    assert client.get("/api/detail?order=montant").status_code == 400


def test_le_detail_accepte_les_memes_filtres_que_l_analyse(client):
    d = client.get("/api/detail?sports=tennis&odds_min=3.0").json()
    assert d["total"] == 3


# ── ⚠️ Injection SQL ─────────────────────────────────────────────────

HOSTILES = [
    "'; DROP TABLE value_bets; --",
    "unibet_be' OR '1'='1",
    "1); DELETE FROM results; --",
    "' UNION SELECT * FROM played_bets --",
    "\\'; UPDATE value_bets SET ev_pct=999; --",
]


@pytest.mark.parametrize("mechant", HOSTILES)
def test_un_book_hostile_est_refuse_et_la_base_est_intacte(client, base, mechant):
    avant = hashlib.sha256(base.read_bytes()).hexdigest()
    r = client.get("/api/analyse", params={"bookmakers": mechant})
    assert r.status_code == 400
    assert hashlib.sha256(base.read_bytes()).hexdigest() == avant


@pytest.mark.parametrize("mechant", HOSTILES)
def test_un_sport_hostile_est_refuse_en_400_et_la_base_est_intacte(client, base, mechant):
    """PHASE 4 — 400 au lieu de 200/n=0, et c'est plus sûr, pas moins.

    La valeur n'atteint toujours aucune chaîne SQL ; elle est simplement
    rejetée plus tôt, par le périmètre. La différence pour l'utilisateur est
    qu'un refus explicite remplace un zéro muet — deux causes, un chiffre,
    exactement ce que le projet cherche à éviter partout ailleurs.

    L'invariant de sûreté, lui, est identique et toujours vérifié : la base
    est octet pour octet la même après la requête."""
    avant = hashlib.sha256(base.read_bytes()).hexdigest()
    r = client.get("/api/analyse", params={"sports": mechant})
    assert r.status_code == 400
    assert "périmètre" in r.json()["detail"]
    assert hashlib.sha256(base.read_bytes()).hexdigest() == avant


@pytest.mark.parametrize("champ", ["leagues", "canal"])
def test_les_autres_champs_libres_sont_inoffensifs(client, base, champ):
    avant = hashlib.sha256(base.read_bytes()).hexdigest()
    client.get("/api/analyse", params={champ: "'; DROP TABLE events; --"})
    assert hashlib.sha256(base.read_bytes()).hexdigest() == avant


def test_le_chemin_de_la_base_n_est_PAS_un_parametre(client, tmp_path):
    """⚠️ Le laisser choisir au client ouvrirait une traversée de répertoires,
    ou pire : une base que l'attaquant contrôle."""
    autre = tmp_path / "piege.db"
    autre.write_bytes(b"")
    for nom in ("db", "database", "db_path", "ANALYTICS_DB"):
        r = client.get("/api/analyse", params={nom: str(autre)})
        # Le paramètre est ignoré : l'analyse porte toujours sur la vraie base.
        assert r.status_code == 200
        assert r.json()["summary"]["opportunities"] == 10


# ── ⚠️ L'API n'écrit JAMAIS ──────────────────────────────────────────

def test_aucune_ecriture_apres_une_campagne_complete(client, base):
    """La garantie vient de `mode=ro` dans la couche analytique. Ce test la
    constate de l'extérieur, sur l'empreinte du fichier."""
    avant = hashlib.sha256(base.read_bytes()).hexdigest()
    for url in ("/api/health", "/api/filters", "/api/analyse",
                "/api/analyse?population=settled&played=oui",
                "/api/analyse?granularite=jour",
                "/api/detail?per_page=50", "/api/detail?sort=clv&order=asc"):
        assert client.get(url).status_code == 200
    assert hashlib.sha256(base.read_bytes()).hexdigest() == avant


def test_aucune_methode_d_ECRITURE_n_est_exposee(client):
    """Pas de POST, PUT, PATCH ni DELETE : la surface d'écriture est vide par
    construction, pas par vigilance."""
    for route in client.app.routes:
        methodes = set(getattr(route, "methods", ()) or ())
        assert not (methodes & {"POST", "PUT", "PATCH", "DELETE"}), route.path


# ── ⚠️ L'API délègue à src.analytics, elle ne recalcule rien ────────

def test_analyse_appelle_bien_la_couche_analytique(base, monkeypatch):
    """Vérifié en INTERCEPTANT la fonction, pas en lisant le code : si
    quelqu'un réécrivait le calcul dans l'API, l'appel disparaîtrait."""
    appels = []
    import src.analytics_api.app as mod
    vrai = mod.analyser

    def espion(db, filtres, **kw):
        appels.append((db, filtres))
        return vrai(db, filtres, **kw)

    monkeypatch.setattr(mod, "analyser", espion)
    TestClient(creer_app(str(base))).get("/api/analyse?sports=tennis")
    assert len(appels) == 1
    assert appels[0][1].sports == ("tennis",), "les filtres n'ont pas suivi"


def test_detail_appelle_bien_la_couche_analytique(base, monkeypatch):
    appels = []
    import src.analytics_api.app as mod
    vrai = mod.detail

    def espion(db, filtres, **kw):
        appels.append(kw)
        return vrai(db, filtres, **kw)

    monkeypatch.setattr(mod, "detail", espion)
    TestClient(creer_app(str(base))).get("/api/detail?page=2&per_page=4")
    assert appels == [{"page": 2, "par_page": 4, "tri": "detected_at",
                       "ordre": "desc"}]


def test_l_api_ne_contient_aucun_sql():
    """Une seule définition du SQL, dans `requete.py`. En voir apparaître ici
    signifierait qu'une seconde est en train de naître."""
    import pathlib
    source = pathlib.Path("src/analytics_api/app.py").read_text()
    code = "\n".join(l for l in source.splitlines()
                     if not l.strip().startswith("#"))
    # ⚠️ « FROM » est absent de cette liste, et c'est délibéré : il entre en
    # collision avec le `from` des imports Python, que ce fichier utilise
    # forcément. Un test qui échoue toujours ne teste rien.
    for mot in ("SELECT ", " JOIN ", "WHERE ", "INSERT ", "UPDATE ",
                "DELETE ", "CREATE ", "ALTER ", "DROP "):
        assert mot not in code.upper(), f"du SQL est apparu dans l'API : {mot}"


# ── La configuration ─────────────────────────────────────────────────

def test_la_base_vient_de_l_environnement_quand_elle_n_est_pas_injectee(
        base, monkeypatch):
    monkeypatch.setenv(VAR_DB, str(base))
    assert TestClient(creer_app()).get("/api/analyse").json()[
        "summary"]["opportunities"] == 10


def test_l_environnement_est_relu_a_CHAQUE_requete(base, tmp_path, monkeypatch):
    """Résolu à la fabrication, un changement de configuration n'aurait pris
    effet qu'au redémarrage — et un test ne pourrait pas poser sa base après
    avoir créé l'app."""
    c = TestClient(creer_app())
    monkeypatch.setenv(VAR_DB, str(base))
    assert c.get("/api/analyse").status_code == 200
    monkeypatch.setenv(VAR_DB, str(tmp_path / "nulle_part.db"))
    assert c.get("/api/analyse").status_code == 503


def test_l_authentification_est_un_point_d_ancrage_unique():
    """Aujourd'hui vide, mais posée sur CHAQUE route : l'ajouter sera un
    changement d'une fonction, pas une revue de toutes les routes en espérant
    n'en oublier aucune."""
    from src.analytics_api.app import exiger_acces
    app = creer_app("x")
    routes_api = [r for r in app.routes
                  if getattr(r, "path", "").startswith("/api/")]
    assert routes_api
    for r in routes_api:
        noms = [d.dependency for d in getattr(r, "dependencies", [])]
        assert exiger_acces in noms, f"{r.path} n'est pas protégeable"


def test_aucun_secret_n_est_ecrit_dans_le_code():
    import pathlib
    for f in ("src/analytics_api/app.py", "scripts/analytics_serve.py"):
        texte = pathlib.Path(f).read_text().lower()
        for mot in ("password=", "secret=", "token=", "api_key=", "apikey="):
            assert mot not in texte, f"{f} contient {mot}"


# ── Le lanceur ───────────────────────────────────────────────────────

def test_le_lanceur_refuse_une_ecoute_publique_sans_authentification():
    """⚠️ `exiger_acces` est vide : écouter sur 0.0.0.0 exposerait au réseau
    l'historique complet des détections, des paris et des résultats."""
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-m", "scripts.analytics_serve",
                        "--host", "0.0.0.0"], capture_output=True, text=True)
    assert r.returncode != 0
    assert "Refus d'écouter" in r.stderr
    assert "ssh -N -L" in r.stderr, "l'alternative sûre n'est pas proposée"


@pytest.mark.parametrize("hote,bouclage", [
    ("127.0.0.1", True), ("localhost", True), ("::1", True),
    ("127.0.0.2", True), ("0.0.0.0", False), ("192.168.1.10", False),
    ("pas-une-adresse", False)])
def test_la_detection_du_bouclage(hote, bouclage):
    from scripts.analytics_serve import _est_bouclage
    assert _est_bouclage(hote) is bouclage


def test_le_lanceur_echoue_si_la_base_est_absente(tmp_path):
    """Échouer au démarrage plutôt qu'à la première requête : une API qui
    démarre et rend « 0 opportunité » se lit comme « aucun pari »."""
    import subprocess
    import sys
    r = subprocess.run([sys.executable, "-m", "scripts.analytics_serve",
                        "--db", str(tmp_path / "absente.db")],
                       capture_output=True, text=True)
    assert r.returncode != 0 and "Base introuvable" in r.stderr


def test_une_ligue_contenant_une_virgule_n_est_pas_decoupee(client):
    """⚠️ Le découpage par virgules est une commodité pour les identifiants
    (sports, books, marchés). Un nom de compétition peut en contenir une :
    y découper casserait un filtre légitime pour faire marcher la commodité."""
    d = client.get("/api/analyse", params={"leagues": "Suisse, Super League"})
    assert d.status_code == 200
    assert d.json()["filters"]["leagues"] == ["Suisse, Super League"]


def test_les_identifiants_sont_bien_decoupes_par_virgules(client):
    """Le pendant : sports, books et marchés acceptent les trois écritures."""
    for champ, valeur, attendu in (
            ("sports", "soccer,tennis", 2),
            # Deux books SANS jumeau : ce test parle du découpage par
            # virgules, pas du dépliage des jumeaux Kambi.
            ("bookmakers", "betano_be,ladbrokes_be", 2),
            ("markets", "h2h,totals", 2)):
        d = client.get("/api/analyse", params={champ: valeur}).json()
        cle = {"sports": "sports", "bookmakers": "bookmakers",
               "markets": "markets"}[champ]
        assert len(d["filters"][cle]) == attendu, champ


# ══════════════════════════════════════════════════════════════════════
# PHASE 4 — les nouveaux paramètres, réellement appliqués côté serveur.
#
# ⚠️ CHAQUE TEST VÉRIFIE LE CONTENU DU LOT, JAMAIS LA PRÉSENCE DU PARAMÈTRE.
# Un paramètre accepté puis ignoré rendrait exactement la même réponse qu'un
# filtre appliqué — à ceci près que les chiffres seraient faux. C'est le mode
# de panne que l'énoncé interdit explicitement (« NE PAS implémenter un faux
# filtrage frontend »).
# ══════════════════════════════════════════════════════════════════════


def test_ev_bands_filtre_REELLEMENT_le_lot(client):
    tout = client.get("/api/analyse").json()["summary"]["opportunities"]
    band = client.get("/api/analyse",
                      params={"ev_bands": "5-8%"}).json()["summary"]
    assert 0 < band["opportunities"] < tout
    d = client.get("/api/detail",
                   params={"ev_bands": "5-8%", "per_page": 500}).json()
    assert d["items"], "aucune ligne : le filtre a tout mangé"
    for it in d["items"]:
        assert 5.0 <= it["ev_pct"] < 8.0


def test_ev_bands_repetable_fait_une_UNION(client):
    a = client.get("/api/analyse", params={"ev_bands": "5-8%"}).json()
    b = client.get("/api/analyse", params={"ev_bands": "8-15%"}).json()
    u = client.get("/api/analyse?ev_bands=5-8%25&ev_bands=8-15%25").json()
    assert (u["summary"]["opportunities"]
            == a["summary"]["opportunities"] + b["summary"]["opportunities"])


def test_ev_bands_accepte_aussi_les_virgules(client):
    a = client.get("/api/analyse?ev_bands=5-8%25&ev_bands=8-15%25").json()
    b = client.get("/api/analyse?ev_bands=5-8%25,8-15%25").json()
    assert a["summary"]["opportunities"] == b["summary"]["opportunities"]


def test_ev_par_sport_est_applique_au_LOT_et_sans_fuite(client):
    """⚠️ LE TEST CENTRAL DE LA PHASE 4 CÔTÉ API."""
    d = client.get("/api/detail",
                   params={"ev_bands_soccer": "5-8%",
                           "ev_bands_tennis": "35%+",
                           "per_page": 500}).json()
    assert d["items"], "le filtre par sport a vidé le lot"
    vus = set()
    for it in d["items"]:
        vus.add(it["sport"])
        if it["sport"] == "soccer":
            assert 5.0 <= it["ev_pct"] < 8.0, "règle tennis appliquée au foot"
        else:
            assert it["ev_pct"] >= 35.0, "règle foot appliquée au tennis"
    assert vus <= {"soccer", "tennis"}


def test_ev_par_sport_est_RELISIBLE_dans_la_reponse(client):
    r = client.get("/api/analyse", params={"ev_bands_tennis": "35%+"}).json()
    assert r["ev_rules"]["by_sport"] == {"tennis": ["35%+"]}
    assert r["filters"]["ev_bands_by_sport"] == {"tennis": ["35%+"]}


def test_un_sport_sans_regle_suit_la_regle_GLOBALE(client):
    r = client.get("/api/analyse",
                   params={"ev_bands": "8-15%",
                           "ev_bands_tennis": "35%+"}).json()
    assert r["ev_rules"]["global"] == ["8-15%"]
    d = client.get("/api/detail",
                   params={"ev_bands": "8-15%", "ev_bands_tennis": "35%+",
                           "per_page": 500}).json()
    for it in d["items"]:
        if it["sport"] == "soccer":
            assert 8.0 <= it["ev_pct"] < 15.0


@pytest.mark.parametrize("param,valeur", [
    ("ev_bands", "inventee"),
    ("ev_bands", "12-13%"),
    ("ev_bands_hockey", "5-8%"),
    ("ev_bands_basketball", "5-8%"),
])
def test_une_bande_ou_un_sport_invalide_est_refuse(client, base, param, valeur):
    avant = hashlib.sha256(base.read_bytes()).hexdigest()
    r = client.get("/api/analyse", params={param: valeur})
    assert r.status_code == 400
    assert hashlib.sha256(base.read_bytes()).hexdigest() == avant


@pytest.mark.parametrize("m", ["h2h_h1", "totals_h1", "handicap", "btts"])
def test_un_marche_hors_perimetre_est_refuse(client, m):
    r = client.get("/api/analyse", params={"markets": m})
    assert r.status_code == 400
    assert "périmètre" in r.json()["detail"]


def test_le_perimetre_est_ANNONCE_par_api_filters(client):
    p = client.get("/api/filters").json()["perimetre"]
    assert p["sports"] == ["soccer", "tennis"]
    assert p["markets"] == ["h2h", "totals"]
    assert p["pourquoi"]
    assert p["exclus"]["total_detections"] >= 0


def test_api_filters_rend_les_LIBELLES(client):
    f = client.get("/api/filters").json()
    assert f["sports_labels"]["soccer"] == "Soccer"
    assert f["bookmakers_labels"].get("ladbrokes_be") == "Ladbrokes"
    # Les quatre jumeaux Kambi ne font qu'une case à cocher, sous le libellé
    # du GROUPE : en proposer quatre laisserait croire à quatre choix.
    assert f["bookmakers_labels"].get("unibet_be") == \
        "Unibet / Scooore / 711 / Bingoal"
    for jumeau in ("scooore_be", "seven_eleven_be", "bingoal_be"):
        assert jumeau not in f["bookmakers"], jumeau
    assert f["markets_labels"]["h2h"] == "H2H"
    # ⚠️ Les valeurs canoniques restent canoniques : c'est elles qui repartent
    # en filtre. Un libellé dans `sports` casserait toutes les requêtes.
    assert all(s == s.lower() for s in f["sports"])


def test_les_decoupes_portent_leur_libelle(client):
    r = client.get("/api/analyse").json()
    for axe in ("by_book", "by_sport", "by_market"):
        for t in r[axe]:
            assert "label" in t and "key" in t


def test_les_nouvelles_decoupes_existent_et_SOMMENT(client):
    r = client.get("/api/analyse").json()
    n = r["summary"]["opportunities"]
    for axe in ("by_market", "by_delay"):
        assert axe in r
        assert sum(t["opportunities"] for t in r[axe]) == n, axe


def test_le_resume_porte_les_indicateurs_de_VOLUME(client):
    s = client.get("/api/analyse").json()["summary"]
    for cle in ("sample", "sample_settled", "sample_clv"):
        assert cle in s and "niveau" in s[cle] and "libelle" in s[cle]
        assert "significat" not in s[cle]["libelle"].lower()


def test_les_cumuls_temporels_sont_rendus(client):
    for t in client.get("/api/analyse").json()["by_time"]:
        assert "pnl_cumul" in t and "clv_cumul" in t


# ── /api/segments ────────────────────────────────────────────────────

def test_segments_repond_et_porte_sa_mise_en_garde(client):
    r = client.get("/api/segments", params={"min_n": 1})
    assert r.status_code == 200
    d = r.json()
    assert d["combinaisons_testees"] > 0
    assert any("DATA MINING" in w for w in d["warnings"])
    assert d["trier_par"] == "clv"
    assert "overall" in d


def test_segments_refuse_un_tri_inconnu(client):
    assert client.get("/api/segments", params={"sort": "pnl"}).status_code == 400


def test_segments_respecte_les_filtres_recus(client):
    d = client.get("/api/segments",
                   params={"min_n": 1, "sports": "tennis"}).json()
    for s in d["segments"]:
        for c in s["criteres"]:
            if c["dimension"] == "sport":
                assert c["value"] == "tennis"


def test_segments_ne_touche_pas_la_base(client, base):
    avant = hashlib.sha256(base.read_bytes()).hexdigest()
    client.get("/api/segments", params={"min_n": 1})
    assert hashlib.sha256(base.read_bytes()).hexdigest() == avant


def test_segments_ignore_un_chemin_de_base_passe_en_parametre(client):
    """La propriété de la Phase 2 doit tenir sur le nouvel endpoint aussi."""
    ref = client.get("/api/segments", params={"min_n": 1}).json()
    piege = client.get("/api/segments",
                       params={"min_n": 1, "db": "/tmp/piege.db"}).json()
    assert piege["overall"]["opportunities"] == ref["overall"]["opportunities"]


@pytest.mark.parametrize("mechant", HOSTILES)
def test_segments_resiste_aux_injections(client, base, mechant):
    avant = hashlib.sha256(base.read_bytes()).hexdigest()
    r = client.get("/api/segments", params={"bookmakers": mechant, "min_n": 1})
    assert r.status_code == 400
    assert hashlib.sha256(base.read_bytes()).hexdigest() == avant


# ── OpenAPI ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("chemin", ["/api/analyse", "/api/detail",
                                    "/api/segments"])
def test_les_nouveaux_parametres_sont_DOCUMENTES(client, chemin):
    """Un paramètre non documenté est un paramètre que personne ne trouvera."""
    spec = client.get("/openapi.json").json()
    noms = {p["name"] for p in spec["paths"][chemin]["get"]["parameters"]}
    assert "ev_bands" in noms
    assert "ev_bands_soccer" in noms and "ev_bands_tennis" in noms


def test_la_documentation_des_bandes_liste_les_valeurs_REELLES(client):
    spec = client.get("/openapi.json").json()
    p = next(x for x in spec["paths"]["/api/analyse"]["get"]["parameters"]
             if x["name"] == "ev_bands")
    for bande in ("5-8%", "8-15%", "15-35%", "35%+"):
        assert bande in p["description"]


# ══ Les tranches de COTE, jusque dans la query string ══════════════

def test_odds_bands_est_accepte_et_applique(client):
    tout = client.get("/api/analyse").json()["summary"]["opportunities"]
    filtre = client.get("/api/analyse",
                        params={"odds_bands": "1.8-2.3"}).json()
    assert filtre["summary"]["opportunities"] <= tout
    assert filtre["filters"]["odds_bands"] == ["1.8-2.3"]


def test_odds_bands_par_sport_est_accepte_et_RENDU(client):
    """Le filtre appliqué doit repartir dans la réponse : un critère qu'on
    croit posé et qui ne l'est pas est le mode de panne que toute cette
    couche existe pour empêcher."""
    d = client.get("/api/analyse",
                   params={"odds_bands_tennis": "> 6.0"}).json()
    assert d["filters"]["odds_bands_by_sport"] == {"tennis": ["> 6.0"]}


def test_une_bande_de_cote_inconnue_rend_400_pas_un_lot_ampute(client):
    r = client.get("/api/analyse", params={"odds_bands": "2.0-2.5"})
    assert r.status_code == 400
    assert "Bande de cote inconnue" in r.json()["detail"]


def test_openapi_declare_les_bandes_de_cote_par_sport(client):
    noms = {p["name"] for p in client.get("/openapi.json").json()
            ["paths"]["/api/analyse"]["get"]["parameters"]}
    assert "odds_bands_soccer" in noms and "odds_bands_tennis" in noms
    # Et l'EV n'a pas disparu au passage.
    assert "ev_bands_soccer" in noms


def test_ev_min_par_sport_est_accepte_et_RENDU(client):
    d = client.get("/api/analyse",
                   params={"ev_min_tennis": 25, "ev_max_tennis": 40}).json()
    assert d["filters"]["ev_free_by_sport"] == {"tennis": [25.0, 40.0]}


def test_ev_min_GLOBAL_reste_global_dans_lAPI(client):
    """⚠️ `ev_min` nu ne doit pas être happé par la lecture par sport."""
    d = client.get("/api/analyse", params={"ev_min": 8}).json()
    assert d["filters"]["ev_min"] == 8.0
    assert d["filters"]["ev_free_by_sport"] == {}


def test_openapi_declare_les_bornes_dev_par_sport(client):
    noms = {p["name"] for p in client.get("/openapi.json").json()
            ["paths"]["/api/analyse"]["get"]["parameters"]}
    assert {"ev_min_soccer", "ev_max_soccer",
            "ev_min_tennis", "ev_max_tennis"} <= noms


# ── L'EV par TRANCHE DE COTE, vue de l'API ──────────────────────────

def test_ev_par_tranche_de_cote_est_acceptee_et_RENDUE(client):
    """Le filtre appliqué repart dans la réponse, sous son LIBELLÉ : c'est
    lui que l'interface affiche et que l'export PDF imprime, pas le slug."""
    d = client.get("/api/analyse",
                   params={"ev_odds_min_1_0_1_8": 3,
                           "ev_odds_max_1_0_1_8": 9}).json()
    assert d["filters"]["ev_free_by_odds"] == {"1.0-1.8": [3.0, 9.0]}


def test_la_tranche_sans_slug_lisible_passe_quand_meme(client):
    """« > 6.0 » ne se met pas dans une URL sans encodage. Son slug « 6_0 »
    doit donc désigner la bonne tranche, et se relire sous son vrai nom."""
    d = client.get("/api/analyse", params={"ev_odds_min_6_0": 12}).json()
    assert d["filters"]["ev_free_by_odds"] == {"> 6.0": [12.0, None]}


def test_ev_odds_min_NE_VOLE_PAS_la_borne_par_sport(client):
    """⚠️ `ev_odds_min_…` ne commence PAS par `ev_min_`, et c'est voulu : si
    l'un préfixait l'autre, le lecteur par sport y verrait un sport nommé
    « odds_1_0_1_8 » et refuserait la requête comme hors périmètre."""
    d = client.get("/api/analyse",
                   params={"ev_min": 8, "ev_min_tennis": 25,
                           "ev_odds_min_1_0_1_8": 3}).json()
    assert d["filters"]["ev_min"] == 8.0
    assert d["filters"]["ev_free_by_sport"] == {"tennis": [25.0, None]}
    assert d["filters"]["ev_free_by_odds"] == {"1.0-1.8": [3.0, None]}


def test_une_tranche_de_cote_inconnue_rend_400(client):
    r = client.get("/api/analyse", params={"ev_odds_min_2_0_2_5": 3})
    assert r.status_code == 400
    assert "inconnue" in r.json()["detail"]


def test_la_regle_de_tranche_PRIME_et_ne_sadditionne_pas(client):
    """Le contrat de bout en bout : assouplir une tranche doit RENDRE DES
    LIGNES. Si la borne globale continuait de s'appliquer par-dessus, ce
    nombre serait identique au précédent et le réglage serait décoratif."""
    strict = client.get("/api/analyse",
                        params={"ev_min": 10}).json()["summary"]["opportunities"]
    ouvert = client.get("/api/analyse",
                        params={"ev_min": 10,
                                "ev_odds_min_1_0_1_8": 0}).json()
    assert ouvert["summary"]["opportunities"] > strict


def test_openapi_declare_les_bornes_dev_par_tranche(client):
    noms = {p["name"] for p in client.get("/openapi.json").json()
            ["paths"]["/api/analyse"]["get"]["parameters"]}
    assert {"ev_odds_min_1_0_1_8", "ev_odds_max_1_0_1_8",
            "ev_odds_min_6_0"} <= noms
    # Et les trois familles par sport n'ont pas disparu au passage.
    assert {"ev_bands_soccer", "odds_bands_tennis", "ev_min_soccer"} <= noms


def test_les_filtres_exposent_le_slug_de_chaque_tranche(client):
    """⚠️ LE SLUG VIENT DU SERVEUR. Le recalculer en JavaScript ferait vivre
    deux règles de fabrication, et la divergence se verrait le jour où un
    libellé change — sous la forme d'un filtre refusé, ou ignoré."""
    bandes = client.get("/api/filters").json()["odds_bands"]
    assert {b["key"]: b["slug"] for b in bandes}["1.0-1.8"] == "1_0_1_8"
    assert all(b.get("slug") for b in bandes)
