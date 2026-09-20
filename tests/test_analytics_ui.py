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


# ── ⚠️ LE CACHE DU NAVIGATEUR : l'interface à moitié ancienne ───────
#
# Panne réelle du 18/09. Le déploiement Phase 4 était parfait côté serveur —
# bon commit, bon service, bons octets sur le fil — et l'interface affichée
# était fausse : `index.html` en Phase 4, `app.js` et `style.css` en Phase 3,
# servis par le cache du navigateur. L'ancien script injecte des `<option>`
# dans ce qui est devenu un `<div class="cases">`, ce qui affiche les filtres
# en texte brut, sans case à cocher.
#
# Rien de tout cela n'apparaît dans un journal, et aucun test de rendu ne peut
# l'attraper : l'incohérence n'existe pas dans le dépôt, elle existe dans le
# navigateur. La SEULE chose vérifiable ici est l'en-tête qui l'empêche.


def test_chaque_fichier_statique_impose_la_REVALIDATION(client):
    """⚠️ SANS CET EN-TÊTE, UN DÉPLOIEMENT PEUT SERVIR DEUX VERSIONS.

    Starlette ne pose aucun `Cache-Control` : le navigateur applique alors sa
    fraîcheur heuristique (~10 % de l'âge du fichier) et ne redemande RIEN
    pendant des jours. Un `app.js` de trois semaines reste donc en place après
    la mise en production de son successeur."""
    for chemin in ("/", "/index.html", "/app.js", "/style.css"):
        r = client.get(chemin)
        assert r.status_code == 200, chemin
        assert r.headers.get("cache-control") == "no-cache", (
            f"{chemin} est servi sans revalidation imposée : le navigateur "
            f"peut le garder en cache après un déploiement")


def test_la_revalidation_repond_304_et_garde_len_tete(client):
    """`no-cache` n'interdit pas le cache, il impose l'aller-retour.

    Ce test mesure le COÛT du correctif : un fichier inchangé ne repart pas
    sur le réseau, Starlette répond 304 sans corps. Si cette réponse perdait
    l'en-tête, le navigateur retomberait à l'heuristique au coup suivant."""
    premier = client.get("/app.js")
    etag = premier.headers.get("etag")
    assert etag, "pas d'ETag : la revalidation coûterait le fichier entier"
    second = client.get("/app.js", headers={"If-None-Match": etag})
    assert second.status_code == 304
    assert not second.content
    assert second.headers.get("cache-control") == "no-cache"


def test_lAPI_nest_PAS_touchee_par_la_regle_de_cache(client):
    """Le correctif vise des FICHIERS. Une décision de cache prise pour eux
    n'a aucune raison de s'appliquer aux réponses de l'API — c'est pourquoi
    il est posé sur le montage statique et non dans un middleware."""
    r = client.get("/api/filters")
    assert r.status_code == 200
    assert "cache-control" not in {k.lower() for k in r.headers}


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
    # PHASE 4 — `/api/segments` rejoint la liste. La propriété testée est
    # inchangée : le JS n'appelle QUE des endpoints connus de l'API Analytics.
    assert _urls_appelees(JS) == {"/api/filters", "/api/analyse",
                                  "/api/detail", "/api/segments"}


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


# ══════════════════════════════════════════════════════════════════════
# PHASE 4 — cases à cocher, EV par sport, segments.
#
# ⚠️ CE QUI EST TESTÉ ICI EST UNE PROPRIÉTÉ D'ERGONOMIE, ET ELLE COMPTE.
# Un `select multiple` perd sa sélection au premier clic simple et cache ce
# qui est coché dès que la liste dépasse sa hauteur. Les deux sont des pertes
# SILENCIEUSES : on croit avoir filtré sur quatre bookmakers, on en a un, et
# le tableau au-dessous a l'air parfaitement normal.
# ══════════════════════════════════════════════════════════════════════


def test_AUCUN_select_multiple_ne_subsiste():
    """Le Ctrl+clic est mort. S'il revenait, ce test le dirait."""
    # ⚠️ On cherche l'ATTRIBUT sur une balise, pas le mot : le commentaire qui
    # explique pourquoi les `select multiple` ont disparu contient le mot, et
    # une assertion qui tombe sur sa propre documentation ne teste rien.
    assert not re.search(r"<select[^>]*\bmultiple\b", HTML), (
        "un <select multiple> est réapparu dans le formulaire")
    assert "selectedOptions" not in JS, (
        "le JS relit encore une sélection de <select multiple>")


@pytest.mark.parametrize("groupe", ["f-sports", "f-books", "f-markets",
                                    "f-ev-bands"])
def test_chaque_filtre_multiple_est_un_groupe_de_cases(groupe):
    """Les quatre filtres à valeurs multiples exigés par l'énoncé."""
    assert re.search(rf'<div class="cases" id="{groupe}"', HTML), groupe


def test_le_groupe_de_cases_fabrique_bien_des_checkbox():
    assert "type = 'checkbox'" in JS or "c.type = 'checkbox'" in JS
    assert "input[type=\"checkbox\"]:checked" in JS, (
        "la lecture d'un groupe ne cible pas des cases cochées")


def test_tout_selectionner_et_tout_deselectionner_existent():
    assert "'Tout'" in JS and "'Aucun'" in JS


def test_une_longue_liste_offre_une_RECHERCHE():
    """Quinze bookmakers dans une liste sans recherche, c'est une liste qu'on
    parcourt à l'œil à chaque fois."""
    assert "type = 'search'" in JS
    assert "masquee" in JS and "masquee" in CSS, (
        "la recherche doit MASQUER, pas décocher")


def test_la_recherche_ne_DECOCHE_jamais():
    """Filtrer une liste ne doit pas modifier la sélection déjà faite : le
    gestionnaire de recherche ne touche qu'à une classe CSS."""
    bloc = JS[JS.index("rech.addEventListener"):]
    bloc = bloc[:bloc.index("barre.appendChild(rech)")]
    assert ".checked" not in bloc, "la recherche modifie des cases cochées"


def test_une_case_cochee_se_VOIT_sans_lire_la_case():
    assert ":has(input:checked)" in CSS


# ── EV par sport ─────────────────────────────────────────────────────

def test_lev_par_sport_a_son_interrupteur_et_son_conteneur():
    assert 'id="ev-split"' in HTML
    assert 'id="ev-par-sport"' in HTML


def test_lev_par_sport_part_vers_le_SERVEUR_et_nest_pas_filtre_ici():
    """⚠️ LA PROPRIÉTÉ CENTRALE DE LA PHASE 4 CÔTÉ FRONTEND.

    Le JS doit ENVOYER `ev_bands_<sport>` et ne jamais comparer une EV
    lui-même. Un filtrage local laisserait les KPI, les découpes et la matrice
    décrire un autre lot que le détail, sans qu'aucun n'ait l'air faux."""
    assert "'ev_bands_' + s" in JS, "les règles par sport ne partent pas en requête"
    # Aucune comparaison d'EV dans le JS : ce serait un filtrage local.
    assert not re.search(r"ev_pct\s*[<>]=?", JS), (
        "le JS compare une EV — c'est un filtrage frontend")


def test_la_regle_dEV_affichee_vient_de_la_REPONSE_du_serveur():
    """Afficher ce qu'on a coché prouve qu'on sait lire son formulaire ;
    afficher ce que le serveur dit avoir appliqué est la seule vérification
    qui vaille."""
    assert "function noteEv(regles)" in JS
    assert "regles.by_sport" in JS
    assert 'id="note-ev"' in HTML


def test_la_selection_par_sport_SURVIT_au_redessin():
    """Cocher un sport ne doit pas effacer l'EV réglée pour un autre."""
    bloc = JS[JS.index("function panneauxEvParSport"):]
    bloc = bloc[:bloc.index("/* ── Meilleurs segments")]
    assert "memoire" in bloc and "coches: memoire[sp]" in bloc


# ── Badges de volume ─────────────────────────────────────────────────

def test_les_badges_disent_un_VOLUME_jamais_une_SIGNIFICATIVITE():
    """⚠️ Le mot est interdit dans l'interface : un effectif ne décide pas de
    la significativité statistique."""
    assert "function badge(ech)" in JS
    assert "pas de significativité" in JS
    for source, nom in ((JS, "app.js"), (HTML, "index.html")):
        for phrase in ("statistiquement significatif", "significativité "
                       "statistique atteinte"):
            assert phrase not in source, f"{nom} promet une significativité"


def test_le_badge_du_ROI_porte_leffectif_des_REGLES():
    """Afficher l'effectif des opportunités ferait passer pour solide un ROI
    calculé sur quarante paris."""
    assert "badge(s.sample_settled)" in JS


@pytest.mark.parametrize("niveau", ["tres_bon", "bon", "moyen", "petit"])
def test_chaque_palier_a_son_style(niveau):
    assert f".ech.{niveau}" in CSS


# ── Segments ─────────────────────────────────────────────────────────

def test_le_bloc_segments_existe_avec_ses_reglages():
    for ident in ("bloc-segments", "segments", "s-tri", "s-min", "s-prof",
                  "s-lancer", "s-garde"):
        assert f'id="{ident}"' in HTML, ident


def test_la_mise_en_garde_est_rendue_AVANT_le_tableau():
    """Un classement se lit du haut vers le bas : une mise en garde en bas de
    page est une mise en garde qu'on n'atteint pas."""
    assert HTML.index('id="s-garde"') < HTML.index('id="segments"')
    bloc = JS[JS.index("function tableauSegments"):]
    assert bloc.index("mise-en-garde") < bloc.index("$('segments')")


def test_le_nombre_de_combinaisons_testees_est_AFFICHE():
    assert "combinaisons testées" in JS
    assert "combinaisons_testees" in JS


def test_le_tri_par_CLV_est_le_defaut_propose():
    m = re.search(r'<select id="s-tri".*?</select>', HTML, re.S)
    assert m, "le sélecteur de tri des segments est absent"
    assert m.group(0).index('value="clv"') < m.group(0).index('value="roi"')
    assert "recommandé" in m.group(0)


def test_les_segments_ne_sont_pas_calcules_d_office():
    """Trois dimensions croisées coûtent nettement plus qu'une analyse."""
    bloc = JS[JS.index("async function analyser()"):]
    bloc = bloc[:bloc.index("/* ── Amorçage")]
    assert "chercherSegments()" not in bloc


# ── Nouvelles découpes ───────────────────────────────────────────────

@pytest.mark.parametrize("bloc", [
    "g-clv-market", "g-roi-market", "g-clv-delay", "g-roi-delay",
    "g-pnl-cumul", "g-clv-cumul", "g-vol-temps", "g-reg-temps"])
def test_les_nouvelles_decoupes_ont_leur_conteneur(bloc):
    assert f'id="{bloc}"' in HTML, bloc


def test_le_pnl_cumule_est_etiquete_en_EUROS():
    """Un axe en « % » qui décrit des euros est une erreur d'unité que
    personne ne rattrape en relisant."""
    assert "'pnl_cumul', 'P&L cumulé',\n      (v) => eur(v, 0)" in JS


def test_les_bandes_de_delai_POSENT_les_bornes_en_heures():
    """Un second mécanisme de délai à côté du premier finirait par le
    contredire sans que rien ne le signale."""
    bloc = JS[JS.index("function boutonsDelai"):]
    bloc = bloc[:bloc.index("function pliage")]
    assert "$('f-delay-min').value" in bloc and "$('f-delay-max').value" in bloc
    # `REFS.delay_bands` est la LISTE de référence rendue par l'API : elle est
    # légitime. Ce qu'on interdit, c'est qu'une bande parte comme PARAMÈTRE de
    # requête à côté de delay_min/delay_max — deux mécanismes de délai
    # finiraient par se contredire sans que rien ne le signale.
    assert not re.search(r"""(append|set)\(\s*['"]delay_band""", JS), (
        "une bande de délai part en filtre parallèle")


# ── Téléphone ────────────────────────────────────────────────────────

def test_les_groupes_se_replient_sur_telephone():
    assert "@media (max-width: 720px)" in CSS
    assert ".champ.pliable" in CSS
    assert "function pliage()" in JS


def test_la_liste_defile_sans_allonger_la_page():
    """Une longue liste de bookmakers ne doit pas repousser le bouton
    ANALYSER hors de l'écran."""
    assert ".cases-liste" in CSS and "overflow-y: auto" in CSS


# ══════════════════════════════════════════════════════════════════════
# LE CLIC SUR UNE CELLULE DE MATRICE — les 30, pas seulement 25.
#
# ⚠️ CE BLOC EXISTE PARCE QUE LA REVUE A TROUVÉ LE DÉFAUT, PAS L'INVERSE.
# La première bande de cote s'appelle « 1.0-1.8 » ; le clic envoyait donc
# `odds_min=1.0`, que `Filtres.valider` refuse — une cote décimale vaut
# toujours plus que 1. Résultat : les CINQ cellules de la première ligne
# répondaient 400 et vidaient le tableau de détail, sous un message d'erreur
# que rien d'autre ne signalait. Vingt-cinq cellules sur trente marchaient, et
# c'est exactement le genre de panne partielle qui survit à une relecture.
#
# Le défaut datait de la Phase 3 : ni la logique du clic ni la règle de
# validation n'avaient bougé. Il a survécu à la validation Phase 3 parce que
# la VM n'avait aucun navigateur pilotable.
# ══════════════════════════════════════════════════════════════════════


#: Sentinelle pour le `undefined` de JavaScript. Une valeur absente n'est PAS
#: `None` côté JS, et la différence est exactement ce qui a produit le défaut
#: n° 2 : `Number.isNaN(undefined)` vaut **false**, donc une borne absente est
#: affectée quand même, puis sérialisée en la chaîne « undefined ».
INDEFINI = object()


def _number_isNaN(v) -> bool:
    """`Number.isNaN` de JavaScript, à la lettre.

    Il ne rend `true` QUE pour la valeur NaN elle-même — pas pour `undefined`,
    contrairement au `isNaN` global. C'est cette subtilité que le code de
    `chargerDetail` n'anticipe pas."""
    return isinstance(v, float) and v != v


def _params_du_clic(bande_cote: str, bande_ev: str) -> dict:
    """Reproduit `chargerDetail` À LA LETTRE, `undefined` compris.

    ⚠️ LA PREMIÈRE VERSION DE CE MIROIR ÉTAIT INFIDÈLE, ET ELLE A MENTI.
    Elle traitait une borne absente comme une absence de clé, là où le JS
    affecte `undefined`. Résultat : le test annonçait « les 30 cellules sont
    cliquables » pendant que cinq d'entre elles répondaient 422 dans un vrai
    navigateur. Un miroir approximatif est pire qu'aucun miroir — il donne la
    tranquillité sans la vérification.

    D'où la fidélité littérale ci-dessous, et le test
    `test_le_js_correspond_bien_a_ce_miroir` qui relit le JS pour que la
    dérive se voie."""
    extra = {}
    morceaux = bande_cote.replace("> ", "").split("-")

    def flottant(i):
        # `map(parseFloat)` sur un tableau plus court rend `undefined`.
        if i >= len(morceaux):
            return INDEFINI
        try:
            return float(morceaux[i])
        except ValueError:
            return float("nan")

    a, b = flottant(0), flottant(1)
    if not _number_isNaN(a) and a is not INDEFINI and a > 1:
        extra["odds_min"] = a
    if b is not INDEFINI and not _number_isNaN(b):
        # Le JS teste maintenant `b !== undefined && !Number.isNaN(b)` :
        # une bande ouverte vers le haut n'envoie plus de borne haute du tout.
        extra["odds_max"] = b

    ev = bande_ev.replace("%", "")
    m = re.match(r"^(\d+)-(\d+)$", ev)
    if m:
        extra["ev_min"], extra["ev_max"] = m.group(1), m.group(2)
    elif ev.startswith("<"):
        extra["ev_max"] = float(ev[1:])
    elif ev.endswith("+"):
        extra["ev_min"] = float(ev[:-1])
    return extra


def _cellules():
    from src.analytics.perimetre import bandes_cote, ordre_ev
    return [(c, e) for c, _, _ in bandes_cote() for e in ordre_ev()]


@pytest.mark.parametrize("cote,ev", _cellules())
def test_chaque_cellule_de_la_matrice_est_cliquable(client, cote, ev):
    """Les 30 cellules, une par une, pour que l'échec nomme la cellule.

    Deux défauts sont passés par ici, tous deux antérieurs à la Phase 4 et
    tous deux sur la même ligne de code :

    * la bande « 1.0-1.8 » envoyait `odds_min=1.0`, refusé en 400 ;
    * la bande « > 6.0 » envoyait `odds_max=undefined`, refusé en 422.

    Les deux sont corrigés. Ce test les couvre désormais sans exception."""
    r = client.get("/api/detail", params=_params_du_clic(cote, ev))
    assert r.status_code == 200, (
        f"cote {cote} × EV {ev} → {r.status_code} "
        f"{str(r.json().get('detail', ''))[:90]}")


def test_la_premiere_bande_n_envoie_PAS_de_borne_basse(client):
    """Le défaut n° 1, corrigé, isolé pour que l'échec soit lisible."""
    p = _params_du_clic("1.0-1.8", "5-8%")
    assert "odds_min" not in p, "la borne basse 1.0 repart vers l'API"
    assert p["odds_max"] == 1.8, "la borne haute doit être conservée"
    assert client.get("/api/detail", params=p).status_code == 200


@pytest.mark.parametrize("bande,attendu", [
    ("1.8-2.3", 1.8), ("2.3-3.0", 2.3), ("3.0-4.0", 3.0), ("4.0-6.0", 4.0)])
def test_les_AUTRES_bandes_gardent_leur_borne_basse(bande, attendu):
    """La correction ne doit toucher QUE la première bande : élargir le lot
    des autres ferait remonter des paris hors de la cellule cliquée."""
    assert _params_du_clic(bande, "8-15%")["odds_min"] == attendu


def test_la_bande_ouverte_garde_sa_borne_basse():
    """« > 6.0 » garde bien `odds_min` — son défaut est sur l'autre borne."""
    assert _params_du_clic("> 6.0", "8-15%")["odds_min"] == 6.0


def test_le_js_garde_la_borne_basse_de_la_premiere_bande():
    """La garde est vérifiée sur le FICHIER, pas sur son miroir Python."""
    bloc = JS[JS.index("async function chargerDetail"):]
    bloc = bloc[:bloc.index("$('detail-filtre')")]
    assert re.search(r"if\s*\(!Number\.isNaN\(a\)\s*&&\s*a\s*>\s*1\)", bloc), (
        "app.js n'écarte plus la borne basse ≤ 1 : la première ligne de la "
        "matrice va de nouveau répondre 400")
    assert "extra.odds_max = b" in bloc, "la borne haute a disparu"


def test_le_js_correspond_bien_a_ce_miroir():
    """⚠️ CE GARDE EST EXACT, ET LA VERSION PRÉCÉDENTE NE L'ÉTAIT PAS.

    Elle se contentait de chercher la sous-chaîne « !Number.isNaN(b) » —
    toujours présente APRÈS la correction du défaut n° 2. Elle n'aurait donc
    jamais signalé la dérive : un garde qui ne peut pas échouer ne garde rien.
    Et de fait, la correction du JS a été faite sans que cette suite bouge
    d'un test.

    On exige donc les DEUX conditions dans leur forme littérale. Toute
    modification de cette ligne casse ce test, ce qui est précisément le but :
    le miroir Python ci-dessus doit être remis à jour en même temps."""
    bloc = JS[JS.index("async function chargerDetail"):]
    bloc = bloc[:bloc.index("$('detail-filtre')")]
    assert re.search(r"if\s*\(!Number\.isNaN\(a\)\s*&&\s*a\s*>\s*1\)\s*"
                     r"extra\.odds_min\s*=\s*a;", bloc), (
        "la garde de la borne BASSE a changé — mets à jour `_params_du_clic`")
    assert re.search(r"if\s*\(b\s*!==\s*undefined\s*&&\s*!Number\.isNaN\(b\)\)\s*"
                     r"extra\.odds_max\s*=\s*b;", bloc), (
        "la garde de la borne HAUTE a changé — mets à jour `_params_du_clic`. "
        "Si `b !== undefined` disparaît, la bande « > 6.0 » renverra "
        "`odds_max=undefined` et l'API répondra 422 sur cinq cellules.")


def test_le_clic_restreint_toujours_sans_remplacer(client):
    """La correction ne doit pas transformer la restriction en remplacement :
    le lot d'une cellule reste un sous-ensemble du lot complet."""
    tout = client.get("/api/detail", params={"per_page": 500}).json()["total"]
    cellule = client.get("/api/detail",
                         params={**_params_du_clic("1.0-1.8", "5-8%"),
                                 "per_page": 500}).json()["total"]
    assert cellule <= tout


# ══ LES CINQ AJOUTS DE L'INTERFACE ═════════════════════════════════

def test_les_tranches_de_cote_ont_leur_bloc_et_leur_panneau_par_sport():
    for marqueur in ('id="f-odds-bands"', 'id="cote-split"',
                     'id="cote-par-sport"'):
        assert marqueur in HTML, marqueur
    assert "panneauxCoteParSport" in JS
    assert "odds_bands_" in JS, "les bandes par sport ne partent pas au serveur"


def test_le_delai_porte_une_UNITE_et_convertit_avant_denvoyer():
    """⚠️ LE SERVEUR ATTEND DES HEURES, TOUJOURS.

    Envoyer des minutes sous le nom `delay_min` ferait lire « 15 heures » à
    une requête qui voulait dire quinze minutes — sans aucune erreur."""
    assert 'id="f-delay-unite"' in HTML
    assert "1 / 60" in JS, "aucune conversion minutes → heures"
    assert "majAideDelai" in JS, "rien ne dit à l'utilisateur ce qui part"


def test_les_bornes_dEV_par_sport_existent_dans_le_panneau():
    assert "ev-min-" in JS and "ev-max-" in JS
    assert "ev_min_" in JS and "ev_max_" in JS


def test_les_populations_saffichent_par_leur_LIBELLE_pas_leur_cle():
    """Le libellé s'affiche, la valeur canonique repart en filtre. Les
    confondre enverrait « Toutes les détections » à une API qui ne connaît
    que « detected »."""
    assert "p.libelle" in JS
    assert "o.value = p.value" in JS


def test_lexport_PDF_nembarque_AUCUNE_bibliotheque():
    """Un générateur PDF embarqué, ce serait des centaines de kilo-octets, une
    seconde mise en page à maintenir, et un téléchargement de dépendance que
    cette machine s'interdit. L'impression du navigateur suffit."""
    assert 'id="pdf"' in HTML
    assert "window.print()" in JS
    assert "@media print" in CSS
    # ⚠️ CHERCHER L'USAGE, PAS LE MOT. Une première version de ce test
    # refusait la chaîne « cdn » et tombait sur le commentaire de
    # `index.html` qui dit précisément qu'il n'y en a aucun — un garde qui
    # se déclenche sur sa propre mise en garde ne protège rien.
    import re
    for nom in ("jspdf", "html2canvas", "pdfmake", "jsPDF"):
        assert not re.search(rf"\b{nom}\b", JS, re.I), nom
    assert not re.search(r'<script[^>]+src=["\']https?:', HTML, re.I), (
        "un script distant est chargé")
    assert not re.search(r'<link[^>]+href=["\']https?:', HTML, re.I), (
        "une feuille de style distante est chargée")


def test_le_PDF_emporte_les_FILTRES_qui_ont_produit_les_chiffres():
    """⚠️ SANS ÇA L'EXPORT EST UN PIÈGE. Relu trois semaines plus tard,
    « ROI +16 % » se lit comme le ROI du système entier alors qu'il portait
    sur une tranche d'EV et un seul sport. L'écran montre les filtres
    au-dessus ; le papier ne les emporte pas tout seul."""
    assert 'id="entete-pdf"' in HTML
    assert "enteteExport" in JS
    assert "#bloc-filtres" in CSS, "les filtres ne sont pas masqués à l'impression"
    # L'en-tête doit citer les règles PAR SPORT, pas seulement les globales.
    for cle in ("ev_bands_by_sport", "odds_bands_by_sport", "ev_free_by_sport"):
        assert cle in JS, cle


def test_limpression_force_lencre_sombre_sur_fond_blanc():
    """Un export en mode sombre est illisible et ruineux en encre."""
    bloc = CSS[CSS.index("@media print"):]
    assert 'data-theme="dark"' in bloc
    assert "#fff" in bloc


# ══ L'EXPOSITION PUBLIQUE : le gabarit Caddy ═══════════════════════

def test_le_gabarit_caddy_ne_code_AUCUNE_valeur_en_dur():
    """⚠️ MÊME RÈGLE QUE LES UNITÉS SYSTEMD DU PROJET. Un fichier écrit à la
    main sur la VM dérive en silence et personne ne le sait — c'est
    exactement ce qui est arrivé à `valuebet-analytics.service`."""
    g = (pathlib.Path(__file__).parent.parent
         / "scripts" / "Caddyfile.in").read_text(encoding="utf-8")
    for jeton in ("__DOMAINE__", "__UTILISATEUR__", "__HASH__"):
        assert jeton in g, jeton
    assert "equodds" not in g.lower(), "un domaine réel est codé en dur"


def test_le_gabarit_caddy_ne_relaie_que_la_BOUCLE_LOCALE():
    """L'Analytics n'a AUCUNE authentification à elle : Caddy doit être la
    seule porte, et le port 8899 rester inaccessible autrement."""
    g = (pathlib.Path(__file__).parent.parent
         / "scripts" / "Caddyfile.in").read_text(encoding="utf-8")
    assert "reverse_proxy 127.0.0.1:8899" in g
    assert "0.0.0.0" not in g
    assert "basic_auth" in g, "aucun mot de passe devant l'API"


def test_installateur_caddy_reprend_le_journal_APRES_la_validation():
    """⚠️ RÉGRESSION VÉCUE, 20/09. `caddy validate` ne lit pas la
    configuration : il PROVISIONNE ses modules, donc il crée
    /var/log/caddy/analytics.log sous root quand le script tourne en sudo.
    Le service tourne sous `caddy` et meurt sur « permission denied » avec
    une configuration validée à la ligne précédente. Le chown doit venir
    APRÈS la validation, sinon il ne sert à rien."""
    s = (pathlib.Path(__file__).parent.parent
         / "scripts" / "setup-caddy.sh").read_text(encoding="utf-8")
    validation = s.index("caddy validate --config")
    reprise = s.index("chown -R caddy:caddy /var/log/caddy")
    assert reprise > validation, "le chown du journal précède la validation"


def test_installateur_caddy_ne_masque_AUCUN_echec_de_chown():
    """Un `|| true` sur un chown transforme une panne nette en service mort
    sans cause lisible — c'est ce qui a rendu le premier diagnostic faux."""
    s = (pathlib.Path(__file__).parent.parent
         / "scripts" / "setup-caddy.sh").read_text(encoding="utf-8")
    for ligne in s.splitlines():
        if ligne.strip().startswith("chown"):
            assert "|| true" not in ligne, ligne
            assert "2>/dev/null" not in ligne, ligne


def test_installateur_caddy_explique_un_demarrage_refuse():
    """Sans le journal sous les yeux, « Job for caddy.service failed » envoie
    chercher la cause ailleurs que là où elle est."""
    s = (pathlib.Path(__file__).parent.parent
         / "scripts" / "setup-caddy.sh").read_text(encoding="utf-8")
    assert "journalctl -u caddy" in s
    assert "$CIBLE.avant-" in s, "aucun retour en arrière indiqué"
