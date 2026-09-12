"""L'interface web — une couche de PRÉSENTATION au-dessus de l'API.

CE QUE CE FICHIER PROTÈGE, ET QUI N'EST PAS ÉVIDENT
---------------------------------------------------
1. **Le montage ne doit pas masquer l'API.** Starlette résout les routes dans
   leur ordre d'enregistrement : monter les fichiers statiques sur « / » AVANT
   `/api/*` enterrerait toute l'API derrière un 404 de fichier introuvable.
   C'est le seul vrai risque de cette composition, et il est silencieux — la
   page s'afficherait parfaitement pendant que chaque requête échouerait.

2. **Le navigateur n'appelle QUE les trois endpoints autorisés.** Vérifié en
   analysant le JavaScript, pas en le relisant.

3. **Aucune ressource externe.** Ni CDN, ni police distante : la page doit
   s'afficher derrière un tunnel SSH, sur une VM sans accès sortant.

4. **Aucune formule métier côté frontend.** Une CLV recalculée dans le
   navigateur serait une seconde définition — le §17.7, commis trois fois déjà
   dans ce projet.

5. **L'arrondi est de PRÉSENTATION.** L'API continue de rendre la pleine
   précision ; c'est le navigateur qui affiche « 12,07 % ».
"""
from __future__ import annotations

import hashlib
import pathlib
import re

import pytest

fastapi = pytest.importorskip(
    "fastapi", reason="FastAPI absent — `pip install fastapi uvicorn`")

from fastapi.testclient import TestClient  # noqa: E402

from src.analytics_ui.app import STATIQUES, creer_app  # noqa: E402
from tests.analytics_base import Opp, monter  # noqa: E402

JS = (STATIQUES / "app.js").read_text(encoding="utf-8")
HTML = (STATIQUES / "index.html").read_text(encoding="utf-8")
CSS = (STATIQUES / "style.css").read_text(encoding="utf-8")


@pytest.fixture
def base(tmp_path):
    return monter(tmp_path, [
        Opp(1, sport="soccer", book="unibet_be", odd=2.20, ev=12,
            jour="2026-08-10", cloture=2.10, gagnant="away", outcome="home"),
        Opp(2, sport="soccer", home="C", away="D", book="ladbrokes_be",
            odd=2.80, ev=9, jour="2026-08-20", cloture=2.90,
            gagnant="home", outcome="home", joue=True),
        Opp(3, sport="tennis", home="Sinner", away="Alcaraz",
            book="betano_be", odd=4.50, ev=25, jour="2026-09-02",
            cloture=4.80, gagnant="away", outcome="home"),
        Opp(4, sport="tennis", home="Rune", away="Zverev", book="betano_be",
            odd=3.10, ev=16, jour="2026-09-07", cloture=None, gagnant=None),
    ])


@pytest.fixture
def client(base):
    return TestClient(creer_app(str(base)))


# ── Les fichiers sont servis ─────────────────────────────────────────

def test_la_page_est_servie_a_la_racine(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Valuebet Analytics" in r.text
    assert "text/html" in r.headers["content-type"]


def test_index_html_est_servi_par_son_nom(client):
    assert client.get("/index.html").status_code == 200


def test_le_css_est_servi(client):
    r = client.get("/style.css")
    assert r.status_code == 200 and "css" in r.headers["content-type"]


def test_le_js_est_servi(client):
    r = client.get("/app.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]


def test_un_fichier_inexistant_rend_404(client):
    assert client.get("/inexistant.js").status_code == 404


# ── ⚠️ LE RISQUE PRINCIPAL : l'API masquée ──────────────────────────

@pytest.mark.parametrize("route", ["/api/health", "/api/filters",
                                   "/api/analyse", "/api/detail"])
def test_les_routes_api_survivent_au_montage(client, route):
    """⚠️ Monter « / » avant `/api/*` enterrerait l'API derrière un 404 de
    fichier introuvable — et la page s'afficherait parfaitement pendant que
    chaque requête échouerait. Panne silencieuse, §11."""
    assert client.get(route).status_code == 200, route


def test_docs_et_openapi_survivent_au_montage(client):
    assert client.get("/docs").status_code == 200
    d = client.get("/openapi.json")
    assert d.status_code == 200 and "/api/analyse" in d.json()["paths"]


def test_l_api_rend_toujours_les_memes_donnees_qu_avant_le_montage(base):
    """La composition ne doit rien changer au contenu : on compare la même
    requête sur l'app NUE et sur l'app composée."""
    from src.analytics_api.app import creer_app as creer_api
    nue = TestClient(creer_api(str(base))).get("/api/analyse").json()
    composee = TestClient(creer_app(str(base))).get("/api/analyse").json()
    assert nue == composee


def test_les_trois_endpoints_repondent_avec_des_filtres_combines(client):
    r = client.get("/api/analyse?sports=soccer&bookmakers=unibet_be"
                   "&odds_min=2&odds_max=3&ev_min=10&ev_max=20"
                   "&date_from=2026-08-01&date_to=2026-09-12"
                   "&population=settled&played=tous")
    assert r.status_code == 200
    assert r.json()["summary"]["opportunities"] == 1


# ── ⚠️ Le frontend n'appelle que les trois endpoints ────────────────

def _urls_appelees(js: str) -> set:
    """Toutes les cibles de `fetch` littérales du fichier."""
    return set(re.findall(r"const\s+API_\w+\s*=\s*'([^']+)'", js))


def test_le_js_ne_connait_que_les_trois_endpoints():
    assert _urls_appelees(JS) == {"/api/filters", "/api/analyse", "/api/detail"}


def test_le_js_n_appelle_fetch_que_par_ces_constantes():
    """Un `fetch('/autre/chose')` littéral doit être impossible : tous les
    appels passent par la fonction `appel`, qui reçoit une des constantes."""
    for cible in re.findall(r"fetch\(([^,)]+)", JS):
        assert cible.strip() in ("url",), f"fetch direct sur {cible!r}"


#: ⚠️ LA SEULE EXCEPTION, ET ELLE N'EST PAS UNE RESSOURCE.
#: `http://www.w3.org/2000/svg` est l'IDENTIFIANT XML du SVG : il apparaît
#: dans `createElementNS` et dans l'attribut `xmlns` d'une icône intégrée en
#: `data:`. Rien n'est chargé — aucune requête réseau n'en sort. L'exception
#: est nommée ici, et un test vérifie plus bas qu'elle ne laisse pas passer
#: une vraie URL externe.
NS_SVG = "http://www.w3.org/2000/svg"


def sans_namespace_svg(texte: str) -> str:
    return texte.replace(NS_SVG, "")


@pytest.mark.parametrize("source", ["JS", "HTML", "CSS"])
def test_aucune_ressource_externe(source):
    """⚠️ La page doit s'afficher derrière un tunnel SSH, sur une VM sans
    accès sortant. Un CDN ou une police distante la casserait — et le
    symptôme serait une page nue, pas un message d'erreur."""
    texte = sans_namespace_svg({"JS": JS, "HTML": HTML, "CSS": CSS}[source])
    for motif in ("http://", "https://", "//cdn", "cdnjs", "unpkg",
                  "jsdelivr", "fonts.googleapis", "fonts.gstatic", "@import"):
        assert motif not in texte, f"{source} contient {motif}"


def test_l_exception_du_namespace_ne_laisse_pas_passer_une_vraie_url():
    """⚠️ Une exception qui ne se teste pas devient une porte. Celle-ci ne
    retire QUE la chaîne exacte du namespace : tout le reste est encore
    attrapé."""
    piege = f"<script src='https://cdn.exemple/x.js'></script> {NS_SVG}"
    assert "https://" in sans_namespace_svg(piege)
    assert sans_namespace_svg(NS_SVG) == ""


def test_aucun_script_ni_style_en_ligne_vers_l_exterieur():
    """Une balise ne doit pointer que vers un chemin relatif ou une donnée
    intégrée. Le `//` d'un protocole trahit une ressource distante."""
    for balise in re.findall(r"<(?:script|link)[^>]*>", HTML):
        nu = sans_namespace_svg(balise).replace("<", "")
        assert "//" not in nu, balise


def test_le_favicon_est_integre_et_non_telecharge():
    """Le navigateur réclame /favicon.ico de lui-même ; sans déclaration il
    récoltait un 404 en console. L'icône est une donnée intégrée, pas un
    fichier distant."""
    assert 'rel="icon"' in HTML and 'href="data:image/svg+xml,' in HTML


# ── ⚠️ Aucune formule métier côté frontend ──────────────────────────

def test_le_js_ne_recalcule_ni_clv_ni_roi_ni_pnl():
    """Une formule ici serait une SECONDE définition. On cherche les motifs
    de calcul, pas les mots : `clv` apparaît partout comme nom de champ."""
    code = "\n".join(l for l in JS.splitlines()
                     if not l.strip().startswith(("*", "//", "/*")))
    interdits = [
        r"odd[s_]?\w*\s*/\s*clos",        # odd / closing — la formule de CLV
        r"clos\w*\s*\*",                  # produit sur la clôture
        r"pnl\s*=\s*[^=]",                # un P&L assigné par calcul
        r"stake\s*\*\s*\(",               # stake × (cote − 1)
        r"\bsum\s*\(|\breduce\(",         # une agrégation maison
    ]
    for motif in interdits:
        trouve = re.findall(motif, code)
        assert not trouve, f"calcul métier détecté : {motif} → {trouve}"


def test_les_seuils_d_affichage_sont_nommes():
    """80 % de couverture et 30 paris réglés sont des seuils de PRÉSENTATION
    (l'API émet déjà ses propres avertissements). Nommés, ils se relisent."""
    assert "SEUIL_COUVERTURE = 80" in JS
    assert "SEUIL_REGLES = 30" in JS


# ── ⚠️ L'arrondi est de présentation seulement ──────────────────────

def test_l_api_rend_toujours_la_pleine_precision(client):
    """L'UI affiche « 12,07 % » ; l'API ne doit pas avoir été arrondie pour
    autant, sinon toute analyse ultérieure hériterait de la perte."""
    it = client.get("/api/detail?per_page=4").json()["items"]
    brut = [i["ev_pct"] for i in it] + [i["clv_pct"] for i in it
                                        if i["clv_pct"] is not None]
    assert any(len(str(abs(v)).split(".")[-1]) > 4 for v in brut), \
        "aucune valeur en pleine précision — l'API a-t-elle été arrondie ?"


def test_le_formatage_vit_dans_le_js_et_pas_dans_l_api():
    for f in ("const pct =", "const eur =", "const ent ="):
        assert f in JS, f
    assert "toLocaleString('fr-FR'" in JS


def test_l_espace_des_pourcentages_est_INSECABLE():
    """« 7,67 % » ne doit pas se couper entre le nombre et son signe."""
    assert " " in JS, "l'espace fine insécable manque"


# ── Les filtres demandés sont présents ──────────────────────────────

@pytest.mark.parametrize("champ", [
    "f-sports", "f-books", "f-markets", "f-league",
    "f-odds-min", "f-odds-max", "f-ev-min", "f-ev-max",
    "f-date-from", "f-date-to", "f-delay-min", "f-delay-max",
    "f-population", "f-played", "f-stake", "f-gran"])
def test_chaque_filtre_demande_est_dans_le_formulaire(champ):
    assert f'id="{champ}"' in HTML, champ


@pytest.mark.parametrize("bloc", [
    "kpis", "g-clv-temps", "g-roi-temps", "g-clv-cote", "g-roi-cote",
    "g-clv-ev", "g-roi-ev", "g-clv-book", "g-roi-book", "g-clv-sport",
    "g-roi-sport", "matrice", "detail", "pagination"])
def test_chaque_bloc_visuel_demande_existe(bloc):
    assert f'id="{bloc}"' in HTML, bloc


def test_clv_et_roi_ne_partagent_jamais_un_graphique():
    """⚠️ La faute de visualisation la plus commune. La CLV vaut quelques
    points, le ROI quelques dizaines : les superposer sur deux échelles ferait
    lire un croisement qui n'existe pas. Deux conteneurs, deux appels."""
    assert 'id="g-clv-temps"' in HTML and 'id="g-roi-temps"' in HTML
    assert JS.count("courbe($('g-clv-temps'), d.by_time, 'clv'") == 1
    assert JS.count("courbe($('g-roi-temps'), d.by_time, 'roi'") == 1


def test_la_couverture_accompagne_toujours_la_clv():
    """La règle du projet : +10,4 % sur 95 % du lot et sur 30 % ne sont pas
    la même phrase."""
    assert "clv_coverage" in JS
    assert "couverture" in JS


# ── Sécurité : rien n'est introduit ─────────────────────────────────

def test_aucune_methode_d_ecriture_n_est_introduite(client):
    for route in client.app.routes:
        methodes = set(getattr(route, "methods", ()) or ())
        assert not (methodes & {"POST", "PUT", "PATCH", "DELETE"}), route


def test_la_base_n_est_pas_modifiee_par_une_visite(client, base):
    avant = hashlib.sha256(base.read_bytes()).hexdigest()
    for url in ("/", "/style.css", "/app.js", "/api/filters", "/api/analyse",
                "/api/detail?per_page=10", "/docs"):
        client.get(url)
    assert hashlib.sha256(base.read_bytes()).hexdigest() == avant


def test_le_chemin_de_la_base_reste_hors_de_portee_du_client(client):
    """Le montage de l'UI ne doit pas avoir rouvert cette porte."""
    n = client.get("/api/analyse").json()["summary"]["opportunities"]
    for p in ("db", "database", "db_path", "ANALYTICS_DB"):
        r = client.get(f"/api/analyse?{p}=/tmp/piege.db")
        assert r.status_code == 200
        assert r.json()["summary"]["opportunities"] == n


def test_le_lanceur_sait_servir_l_api_seule():
    """`--api-seule` doit rendre exactement le service validé en Phase 2."""
    source = pathlib.Path("scripts/analytics_serve.py").read_text()
    assert "--api-seule" in source
    assert "src.analytics_api.app:app" in source
    assert "src.analytics_ui.app:app" in source
