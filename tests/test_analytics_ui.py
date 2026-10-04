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
    # PHASE 4 — `/api/segments` rejoint la liste ; le Strategy Finder y ajoute
    # `/api/strategies`. La propriété testée est inchangée : le JS n'appelle
    # QUE des endpoints connus de l'API Analytics.
    assert _urls_appelees(JS) == {"/api/filters", "/api/analyse",
                                  "/api/detail", "/api/segments",
                                  "/api/strategies"}


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


#: ⚠️ LA CELLULE EST DÉSORMAIS TRADUITE EN TRANCHES, PLUS EN BORNES.
#: L'ancienne version recopiait « 1.0-1.8 » en `odds_min`/`odds_max` et
#: « 5-8% » en `ev_min`/`ev_max` : deux défauts en sont sortis (borne 1.0
#: refusée, `undefined` sérialisé), et surtout les bornes ne recouvraient pas
#: toujours la tranche au bord près. La cellule envoie maintenant SES
#: tranches (`odds_bands`, `ev_bands`) — les mêmes que la matrice — et les
#: règles PAR SPORT, qui priment sur la règle globale, sont restreintes à la
#: cellule ou retirent leur sport. Le miroir ci-dessous suit `requeteCellule`
#: à la lettre ; `test_le_js_correspond_bien_a_ce_miroir` relit le JS.


def _params_du_clic(bande_cote: str, bande_ev: str, base=None, sports=None):
    """Reproduit `requeteCellule` : liste de paires (clé, valeur), comme une
    `URLSearchParams`."""
    p = [(k, v) for k, v in (base or [])]
    p = [(k, v) for k, v in p if k not in ("odds_bands", "ev_bands")]
    p += [("odds_bands", bande_cote), ("ev_bands", bande_ev)]
    exclus = set()
    for k in dict.fromkeys(k for k, _ in p):
        if k.startswith("ev_bands_"):
            prefixe, cible = "ev_bands_", bande_ev
        elif k.startswith("odds_bands_"):
            prefixe, cible = "odds_bands_", bande_cote
        else:
            continue
        valeurs = [v for kk, v in p if kk == k]
        if cible in valeurs:
            p = [(kk, v) for kk, v in p if kk != k] + [(k, cible)]
        else:
            exclus.add(k[len(prefixe):])
    if exclus:
        choisis = [v for k, v in p if k == "sports"] or list(sports or [])
        restants = [s for s in choisis if s not in exclus]
        if not restants:
            return None     # cellule vide : aucun appel, jamais « tous les sports »
        p = [(k, v) for k, v in p if k != "sports"]
        p += [("sports", s) for s in restants]
    return p


def _cellules():
    from src.analytics.perimetre import bandes_cote, ordre_ev
    return [(c, e) for c, _, _ in bandes_cote() for e in ordre_ev()]


def _matrice(client, base=None):
    d = client.get("/api/analyse", params=list(base or [])).json()
    return {(c["odds"], c["ev"]): c["opportunities"] for c in d["matrix"]["cells"]}


@pytest.mark.parametrize("cote,ev", _cellules())
def test_chaque_cellule_de_la_matrice_est_cliquable(client, cote, ev):
    """Les 30 cellules, une par une, pour que l'échec nomme la cellule."""
    r = client.get("/api/detail", params=_params_du_clic(cote, ev))
    assert r.status_code == 200, (
        f"cote {cote} × EV {ev} → {r.status_code} "
        f"{str(r.json().get('detail', ''))[:90]}")


def test_le_clic_ramene_EXACTEMENT_leffectif_de_la_cellule(client):
    """Le contrat du clic, compté : le détail d'une cellule a autant de lignes
    que la cellule en annonce — ni plus (bornes trop larges), ni moins (bord
    de tranche perdu). Vérifié sur les 30 cellules, vides comprises."""
    matrice = _matrice(client)
    assert sum(matrice.values()) > 0
    for cote, ev in _cellules():
        total = client.get("/api/detail",
                           params=_params_du_clic(cote, ev) + [("per_page", 500)]).json()["total"]
        assert total == matrice.get((cote, ev), 0), (cote, ev, total)


def test_le_clic_respecte_les_regles_PAR_SPORT(tmp_path):
    """⚠️ UNE RÈGLE PAR SPORT PRIME SUR LA RÈGLE GLOBALE. Envoyer seulement
    `ev_bands=<cellule>` laisserait le sport réglé suivre SA règle, donc
    remonter des paris hors de la cellule. Le miroir la restreint à la
    cellule, ou retire le sport quand sa règle exclut la cellule.

    La base est construite pour que la version naïve ÉCHOUE : un tennis à
    40 % d'EV dans la même tranche de cote qu'un tennis à 25 %, et un
    football à 12 % que la règle du football garde mais que la cellule
    « 5-8 % » ne contient pas."""
    base_db = monter(tmp_path, [
        Opp(1, sport="soccer", book="unibet_be", odd=2.20, ev=12, jour="2026-08-10",
            cloture=2.10, gagnant="away", outcome="home"),
        Opp(2, sport="soccer", home="C", away="D", book="unibet_be", odd=2.10, ev=6,
            jour="2026-08-11", cloture=2.00, gagnant="home", outcome="home"),
        Opp(3, sport="tennis", home="Sinner", away="Alcaraz", book="betano_be",
            odd=4.50, ev=25, jour="2026-09-02", cloture=4.80, gagnant="away", outcome="home"),
        Opp(4, sport="tennis", home="Rune", away="Zverev", book="betano_be",
            odd=4.20, ev=40, jour="2026-09-03", cloture=4.00, gagnant="home", outcome="home"),
    ])
    c = TestClient(creer_app(str(base_db)))
    regles = [("ev_bands_soccer", "8-15%"),
              ("ev_bands_tennis", "15-35%"), ("ev_bands_tennis", "35%+")]
    matrice = _matrice(c, regles)
    assert matrice.get(("4.0-6.0", "15-35%")) == 1
    assert matrice.get(("4.0-6.0", "35%+")) == 1
    assert not matrice.get(("1.8-2.3", "5-8%"))
    sports = c.get("/api/filters").json()["sports"]
    vides = 0
    for cote, ev in _cellules():
        p = _params_du_clic(cote, ev, regles, sports)
        if p is None:
            vides += 1
            total = 0
        else:
            total = c.get("/api/detail", params=p + [("per_page", 500)]).json()["total"]
        assert total == matrice.get((cote, ev), 0), (cote, ev, total, p)
    assert vides, "aucune cellule n'exclut tous les sports : le cas limite n'est plus couvert"
    # La version naïve (règles par sport laissées telles quelles) se trompe
    # ici — c'est ce qui rend ce test capable d'échouer.
    naif = regles + [("odds_bands", "4.0-6.0"), ("ev_bands", "15-35%")]
    assert c.get("/api/detail", params=naif).json()["total"] == 2


def test_le_js_correspond_bien_a_ce_miroir():
    """Le miroir n'a de valeur que s'il suit le JS : les gestes essentiels de
    `requeteCellule` sont relus dans le fichier."""
    bloc = JS[JS.index("function requeteCellule"):]
    bloc = bloc[:bloc.index("\n}\n")]
    for geste in ("p.set('odds_bands', cellule.odds)", "p.set('ev_bands', cellule.ev)",
                  "k.startsWith('ev_bands_')", "k.startsWith('odds_bands_')",
                  "p.getAll(k).includes(regle[1])", "exclus.add(",
                  "p.delete('sports')"):
        assert geste in bloc, f"`requeteCellule` a changé ({geste}) — mettez à jour `_params_du_clic`"
    # Plus aucune traduction de la cellule en bornes : c'était la source des
    # deux défauts historiques.
    assert "odds_min" not in bloc and "ev_min" not in bloc
    detail = JS[JS.index("async function chargerDetail"):]
    detail = detail[:detail.index("$('detail-filtre')")]
    assert "if (CELLULE && !requeteCellule(p, CELLULE))" in detail
    assert "if (!restants.length) return null;" in bloc


def test_le_clic_restreint_toujours_sans_remplacer(client):
    """La restriction reste un sous-ensemble du lot complet."""
    tout = client.get("/api/detail", params={"per_page": 500}).json()["total"]
    cellule = client.get("/api/detail",
                         params=_params_du_clic("1.0-1.8", "5-8%")
                         + [("per_page", 500)]).json()["total"]
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


# ══ L'EV PAR TRANCHE DE COTE ══════════════════════════════════════

def test_le_bloc_ev_par_tranche_de_cote_existe():
    for jeton in ('id="ev-cote-split"', 'id="ev-par-cote"',
                  'EV par tranche de cote'):
        assert jeton in HTML, jeton


def test_linterface_ANNONCE_que_la_regle_prime():
    """⚠️ SANS CETTE PHRASE, LE RÉGLAGE EST UN PIÈGE. « Se cumule (ET) »
    est écrit deux blocs plus haut pour les bornes libres ; si celui-ci ne
    disait pas l'inverse, l'utilisateur assouplirait une tranche en croyant
    ajouter une condition et ne comprendrait pas le résultat."""
    bloc = HTML[HTML.index('id="champ-ev-cote"'):]
    bloc = bloc[:bloc.index('<div class="champ')]
    assert "remplace" in bloc.lower(), bloc


def test_les_bornes_par_tranche_PARTENT_AU_SERVEUR():
    assert "ev_odds_min_" in JS and "ev_odds_max_" in JS


def test_le_slug_vient_du_SERVEUR_et_nest_pas_refabrique_en_JS():
    """⚠️ DEUX RÈGLES DE FABRICATION FINIRAIENT PAR DIVERGER. Le serveur
    expose `slug` dans `/api/filters` ; le JavaScript le lit, il ne le
    recalcule pas. Une normalisation maison produirait tôt ou tard un nom que
    le serveur refuse — ou pire, qu'il accepte pour la mauvaise tranche."""
    bloc = JS[JS.index("function panneauxEvParCote"):]
    bloc = bloc[:bloc.index("\n}")]
    assert "b.slug" in bloc
    assert "replace(" not in bloc and "toLowerCase" not in bloc


def test_toutes_les_tranches_sont_proposees_pas_seulement_les_cochees():
    """Une tranche absente du panneau se lirait comme « non filtrée », alors
    qu'elle garde la règle générale."""
    bloc = JS[JS.index("function panneauxEvParCote"):]
    bloc = bloc[:bloc.index("\n}")]
    assert "REFS.odds_bands" in bloc
    assert "coches(" not in bloc, "le panneau dépend des cases cochées"


def test_lexport_PDF_porte_la_regle_par_tranche():
    """Un export qui tait une règle prioritaire ment par omission : deux
    lecteurs du même papier en déduiraient deux filtres différents."""
    bloc = JS[JS.index("function enteteExport"):]
    assert "ev_free_by_odds" in bloc[:bloc.index("\n}")]


def test_la_reinitialisation_redessine_le_panneau_des_tranches():
    """`form.reset()` décoche la case mais ne vide pas les champs déjà
    construits : le réglage resterait visible sans plus partir au serveur."""
    bloc = JS[JS.index("$('reinit').addEventListener"):]
    assert "panneauxEvParCote()" in bloc[:bloc.index("\n  });")]


def test_les_champs_de_bornes_ne_pretendent_plus_porter_UN_SPORT():
    """`bornesPaire` sert aux sports ET aux tranches de cote. Un attribut
    `data-sport` portant un slug de tranche ferait lire le mauvais filtre au
    premier qui s'y fierait."""
    assert "bornesSport(" not in JS
    assert "dataset.sport = sport" not in JS
    assert "input[type=\"number\"][data-cle]" in JS


def test_lexport_ecrit_un_couple_de_bornes_en_FLECHE_pas_en_VIRGULE():
    """⚠️ « 1.0-1.8 : 3, » ne dit ni que la borne haute manque, ni laquelle
    des deux vaut 3. Sur un PDF qu'on relit trois mois plus tard, c'est un
    filtre qu'on ne peut plus reconstituer."""
    bloc = JS[JS.index("function enteteExport"):]
    bloc = bloc[:bloc.index("\n}")]
    assert "const bornes = " in bloc
    assert "bornes(f.ev_free_by_odds)" in bloc
    assert "bornes(f.ev_free_by_sport)" in bloc
    assert "table(f.ev_free_by_sport)" not in bloc


# ══ LE PARI (1 X 2) ═══════════════════════════════════════════════

def test_le_bloc_pari_existe_et_part_au_serveur():
    assert 'id="f-outcomes"' in HTML
    assert "append('outcomes'" in JS


def test_linterface_ANNONCE_ce_que_le_1X2_ecarte():
    """⚠️ SANS CETTE PHRASE, LE FILTRE SE LIT COMME UNE PANNE. Ne cocher que
    « 1 » et « X » fait disparaître les Over/Under — c'est correct, un Over
    n'étant ni l'un ni l'autre, mais un total qui rétrécit sans explication
    ressemble à une perte de données."""
    bloc = HTML[HTML.index('id="champ-pari"'):]
    bloc = bloc[:bloc.index("<!--")]
    assert "Over/Under" in bloc
    assert "tennis" in bloc.lower(), "le cas du tennis n'est pas expliqué"


def test_les_libelles_de_pari_viennent_du_SERVEUR():
    """Écrire « 1 — Domicile » dans le JavaScript ferait vivre deux
    vocabulaires pour le même pari ; l'un des deux finirait par ne plus
    correspondre à ce que le filtre envoie."""
    bloc = JS[JS.index("groupeCases($('f-outcomes')"):]
    bloc = bloc[:bloc.index(";")]
    assert "REFS.outcomes" in bloc
    assert "Domicile" not in JS, "un libellé de pari est codé en dur en JS"


def test_la_decoupe_par_pari_est_affichee():
    for cle in ("g-clv-outcome", "g-roi-outcome"):
        assert f'id="{cle}"' in HTML, cle
    assert "d.by_outcome" in JS


def test_lexport_PDF_nomme_le_pari_en_CLAIR():
    """« home » sur un papier relu dans trois mois ne dit pas grand-chose ;
    « 1 — Domicile » si."""
    bloc = JS[JS.index("function enteteExport"):]
    bloc = bloc[:bloc.index("\n}")]
    assert "f.outcomes" in bloc
    assert "nomPari" in bloc


def test_aucun_identifiant_HTML_nest_EN_DOUBLE():
    """⚠️ RÉGRESSION VÉCUE, 21/09. Un bloc inséré deux fois donne deux
    éléments de même `id`. `document.getElementById` rend alors TOUJOURS le
    premier : le second reste vide pour toujours, sans erreur, sans trace
    dans la console. Aucun test de présence ne voit ça — ils cherchent tous
    une occurrence et en trouvent une."""
    import re
    ids = re.findall(r'\bid="([^"]+)"', HTML)
    doubles = sorted({i for i in ids if ids.count(i) > 1})
    assert not doubles, f"identifiants en double : {doubles}"


# ══ LA REFONTE : application à pages, filtres avancés, thèmes ══════════
#
# Ce qui est protégé ici n'est pas une mise en page — elle bougera encore —
# mais les PROPRIÉTÉS qu'elle doit garder : une seule source de vérité pour
# les filtres, un état « Tous » explicite qui ne part jamais au serveur,
# aucun chiffre écrit en dur, un thème posé avant le premier rendu.


PAGES_NAV = ["vue-ensemble", "performance", "clv", "bookmakers", "marches",
             "competitions", "paris", "strategies", "avancee", "mes-analyses",
             "exporter", "parametres"]


@pytest.mark.parametrize("page", PAGES_NAV)
def test_chaque_entree_de_navigation_a_sa_page_et_sa_route(page):
    assert f'href="#/{page}" data-page="{page}"' in HTML, page
    assert f'id="page-{page}" data-page="{page}"' in HTML, page
    assert re.search(rf"""['"]?{page}['"]?\s*:\s*\{{\s*titre:""", JS), (
        f"la page {page} n'est pas déclarée dans le routeur")


def test_le_theme_est_pose_AVANT_le_premier_rendu():
    """Sans ce script en tête, un mode sombre choisi s'affiche une fraction
    de seconde en clair à chaque chargement."""
    tete = HTML[:HTML.index("</head>")]
    assert tete.index("<script>") < tete.index('rel="stylesheet"')
    assert "vb-theme" in tete and "prefers-color-scheme: dark" in tete


def test_le_mode_sombre_est_un_THEME_et_pas_le_noir_absolu():
    bloc = CSS[CSS.index(':root[data-theme="dark"] {'):]
    bloc = bloc[:bloc.index("}")]
    fond = re.search(r"--fond-page:\s*(#[0-9a-fA-F]{6})", bloc).group(1).lower()
    assert fond not in ("#000000", "#000"), "fond noir absolu"
    for jeton in ("--fond-carte", "--encre", "--bordure", "--marque", "--bon", "--grave"):
        assert jeton in bloc, jeton


def test_le_choix_du_theme_est_persiste_et_suit_le_systeme():
    assert "stock.ecrire('vb-theme'" in JS
    assert "prefers-color-scheme: dark" in JS


def test_la_ligne_TOUS_ne_part_JAMAIS_au_serveur():
    """« Tous » est un état d'affichage : « rien de coché » reste le contrat
    de l'API. Si la ligne partait, l'API recevrait une valeur « on »."""
    bloc = JS[JS.index("function coches(id)"):]
    bloc = bloc[:bloc.index("\n}")]
    assert "!c.dataset.tous" in bloc
    assert "tous.dataset.tous = '1'" in JS


def test_plus_aucun_rien_de_coche_egale_tous_dans_linterface():
    assert "Rien de coché" not in HTML


def test_les_raccourcis_de_la_barre_ecrivent_dans_le_formulaire_du_tiroir():
    """Une seule source de vérité : `parametres()` ne lit QUE le tiroir."""
    form = HTML[HTML.index('id="form"'):HTML.index("</form>")]
    for champ in ("f-sports", "f-books", "f-markets", "f-league", "f-outcomes",
                  "f-odds-bands", "f-ev-bands", "f-ev-min", "f-population",
                  "f-played", "f-stake", "f-gran"):
        assert f'id="{champ}"' in form, champ
    for raccourci in ("pf-sport", "pf-books", "pf-market", "pf-ev"):
        assert f'id="{raccourci}"' in HTML, raccourci


def test_lexport_est_dans_un_menu_et_pas_au_niveau_du_bouton_analyser():
    menu = HTML[HTML.index('id="pp-export"'):]
    menu = menu[:menu.index("</div>")]
    for ident in ('id="pdf"', 'id="csv-opps"', 'id="csv-decoupes"'):
        assert ident in menu, ident


def test_aucune_statistique_nest_ecrite_en_dur():
    """Les chiffres de la maquette (« +12,1 % », « 3 482 ») n'existent que
    dans la maquette : tout ce qui s'affiche vient de l'API."""
    # Les commentaires citent des exemples de mise en forme (« +12,1 % ») :
    # on ne cherche que dans le CODE et dans le balisage hors commentaires.
    code = "\n".join(l for l in JS.splitlines()
                     if not l.strip().startswith(("*", "//", "/*")))
    balisage = re.sub(r"<!--.*?-->", "", HTML, flags=re.S)
    for source, nom in ((balisage, "index.html"), (code, "app.js")):
        for motif in (r"[+-]\d+[,.]\d+\s*%", r"\b3\s?482\b", r"\b2\s?847\b"):
            assert not re.search(motif, source), f"{nom} : {motif}"


def test_la_barre_de_contexte_lit_les_filtres_du_SERVEUR():
    bloc = JS[JS.index("function contexte()"):]
    bloc = bloc[:bloc.index("\n}")]
    assert "ANALYSE ? ANALYSE.filters" in bloc


def test_les_exports_CSV_ne_recalculent_rien(client):
    """Chaque colonne du CSV des découpes est un champ que l'API rend déjà."""
    champs = re.findall(r"\['(\w+)', '\w+'\]", JS[JS.index("const CHAMPS_CSV"):
                                                    JS.index("function exporterCsvDecoupes")])
    resume = client.get("/api/analyse").json()["summary"]
    assert champs and set(champs) <= set(resume), set(champs) - set(resume)
    items = client.get("/api/detail?per_page=1").json()["items"][0]
    colonnes = re.findall(r"\['(\w+)', '\w+'\]", JS[JS.index("const COLONNES_CSV"):
                                                      JS.index("async function exporterCsvOpportunites")])
    assert colonnes and set(colonnes) <= set(items), set(colonnes) - set(items)


def test_la_decoupe_par_competition_somme_au_total(client):
    d = client.get("/api/analyse").json()
    assert sum(t["opportunities"] for t in d["by_league"]) == d["summary"]["opportunities"]
    assert {t["key"] for t in d["by_league"]} == {"Jupiler Pro League"}


def test_le_taux_de_paris_joues_vient_du_SERVEUR(client):
    s = client.get("/api/analyse").json()["summary"]
    assert s["played"] == 1 and s["played_rate"] == 25.0
    assert "played_rate" in JS


# ══ LA REVUE UX DE LA REFONTE ══════════════════════════════════════

def _corps(nom):
    """Le corps d'une fonction de app.js, jusqu'à sa accolade fermante en
    colonne 0."""
    debut = JS.index(nom)
    return JS[debut:JS.index("\n}\n", debut)]


def test_tout_ce_qui_suit_lanalyse_repart_de_SES_parametres():
    """⚠️ UN FILTRE RETOUCHÉ SANS RELANCER NE DOIT RIEN FAIRE DÉRIVER. Détail,
    dernières opportunités, segments et CSV repartent des paramètres de
    l'analyse AFFICHÉE ; relire le formulaire leur ferait décrire un autre
    lot que les KPI juste au-dessus."""
    for fonction in ("async function chargerDetail", "async function chargerDernieres",
                     "async function chercherSegments", "async function exporterCsvOpportunites"):
        corps = _corps(fonction)
        assert "parametresAnalyse(" in corps, fonction
        assert "parametres(" not in corps.replace("parametresAnalyse(", ""), fonction
    analyse = _corps("async function analyser")
    assert "PARAMS_ANALYSE = new URLSearchParams(envoyes)" in analyse
    assert "SALE = cleFormulaire() !== CLE_ANALYSE" in analyse


def test_modifie_se_DEDUIT_et_seteint_si_lon_revient_aux_filtres_analyses():
    corps = _corps("function signalerChangement")
    assert "cleFormulaire() !== CLE_ANALYSE" in corps
    assert "SALE = true" not in JS


def test_la_lentille_paris_ne_touche_PAS_le_filtre_de_lanalyse():
    """Les boutons Joués / Non joués / Toutes changent la page, pas
    l'analyse : ils n'écrivent pas dans `f-played` et ne relancent rien."""
    bloc = JS[JS.index("document.querySelectorAll('#pj-filtre button').forEach((b) => b.addEventListener"):]
    bloc = bloc[:bloc.index("}));")]
    assert "f-played" not in bloc and "analyser()" not in bloc
    assert "choisirLentille(b.dataset.played)" in bloc
    # Les KPI de la lentille viennent d'une analyse du SERVEUR.
    assert "appel(API_ANALYSE, parametresAnalyse({ played: mode }))" in _corps("async function rendreParis")


def test_non_joues_nest_pas_presente_comme_alertes_non_cliques():
    """⚠️ `played=non` = `played = 0` en SQL : une opportunité jamais alertée
    en fait partie. L'appeler « Alertés, non cliqués » décrivait un autre
    lot que celui compté."""
    assert "Alertés, non cliqués" not in JS and "Alertés, non cliqués" not in HTML
    select = HTML[HTML.index('id="f-played"'):]
    select = select[:select.index("</select>")]
    assert ">Non joués<" in select


@pytest.mark.parametrize("champ", ["roi", "pnl", "clv", "clv_median", "clv_positive_rate"])
def test_trier_par_une_mesure_ne_couronne_pas_un_petit_echantillon(champ):
    corps = _corps("function tableDecoupe")
    assert f"'{champ}'" in corps
    assert "if (fa !== fb) return fa ? 1 : -1;" in corps
    assert "note-tri" in corps


def test_le_rapport_PDF_attend_ses_requetes_avant_dimprimer():
    corps = _corps("async function imprimerRapport")
    assert "await Promise.all(" in corps
    assert corps.index("await Promise.all(") < corps.index("window.print()")
    assert "impression-rapport" in corps and "impression-rapport" in CSS
    for page in ("vue-ensemble", "performance", "clv", "bookmakers", "marches",
                 "competitions", "paris", "avancee"):
        assert re.search(rf'data-page="{page}" data-titre="[^"]+"', HTML), page
    for page in ("mes-analyses", "exporter", "parametres"):
        assert f'class="page page-outil" id="page-{page}"' in HTML, page
    assert 'id="pdf-rapport"' in HTML


def test_le_menu_EV_coche_toute_EV_seulement_sans_AUCUNE_contrainte():
    corps = _corps("function menuEv")
    assert "itemMenu('Toute EV', !texte, toute)" in corps
    toute = corps[corps.index("const toute"):corps.index("const seuil")]
    for geste in ("$('f-ev-min').value = ''", "$('f-ev-max').value = ''",
                  "cocher('f-ev-bands', [])", "$('ev-split').checked = false",
                  "$('ev-cote-split').checked = false"):
        assert geste in toute, geste


def test_le_tiroir_est_un_dialogue_modal_qui_rend_le_focus():
    ouvrir, fermer = _corps("function ouvrirTiroir"), _corps("function fermerTiroir")
    assert "$('app').inert = true" in ouvrir and "OUVREUR" in ouvrir
    assert "$('app').inert = false" in fermer and "cible.focus()" in fermer


def test_le_bouton_principal_garde_un_contraste_AA_dans_les_deux_themes():
    def lum(h):
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
        f = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
    clair = CSS[CSS.index(":root {"):]
    sombre = CSS[CSS.index(':root[data-theme="dark"] {'):]
    for bloc in (clair[:clair.index("}")], sombre[:sombre.index("}")]):
        fond = re.search(r"--marque-bouton:\s*(#[0-9a-fA-F]{6})", bloc).group(1)
        assert 1.05 / (lum(fond) + 0.05) >= 4.5, fond
        muet = re.search(r"--encre-muet:\s*(#[0-9a-fA-F]{6})", bloc).group(1)
        carte = re.search(r"--fond-carte:\s*(#[0-9a-fA-F]{6})", bloc).group(1)
        hi, lo = sorted((lum(muet), lum(carte)), reverse=True)
        assert (hi + 0.05) / (lo + 0.05) >= 4.5, (muet, carte)
    assert "background: var(--marque-bouton)" in CSS


# ══ STRATEGY FINDER ═══════════════════════════════════════════════════
#
# Une page outil qui MONTRE un classement calculé par le serveur
# (`/api/strategies`). Ce qui est protégé ici : elle ne recalcule rien, elle
# ne promet rien, elle dit quand elle a moins de cinq résultats ou aucun, et
# ses modales se comportent comme le tiroir (inertes derrière, Échap, focus).


def _bloc_strategy_finder():
    """Le code du Strategy Finder, de son bandeau à la section suivante."""
    debut = JS.index("/* ── Strategy Finder ──")
    return JS[debut:JS.index("/* ── Paramètres ──", debut)]


def test_strategy_finder_est_une_page_outil_entre_paris_et_analyse_avancee():
    assert 'class="page page-outil" id="page-strategies" data-page="strategies"' in HTML
    assert re.search(r"strategies:\s*\{\s*titre:\s*'Strategy Finder',[^}]*analyse:\s*false", JS)
    nav = HTML[HTML.index('id="sb-nav"'):HTML.index("</nav>")]
    assert (nav.index('href="#/paris"') < nav.index('href="#/strategies"')
            < nav.index('href="#/avancee"')), "le lien n'est pas entre Paris joués et Analyse avancée"
    lien = nav[nav.index('href="#/strategies"'):]
    lien = lien[:lien.index("</a>")]
    assert "<svg" in lien, "le lien de navigation n'a pas son icône SVG en ligne"


@pytest.mark.parametrize("ident", [
    "sf-sport", "sf-date-from", "sf-date-to", "sf-population", "sf-min-presets",
    "sf-min", "sf-objectif", "sf-rapides", "sf-lancer", "sf-methode-btn",
    "sf-resultats", "sf-cartes", "sf-table", "sf-par-book", "sf-par-marche",
    "sf-csv", "sf-pdf", "sf-entete-pdf", "sf-vide", "sf-nombre"])
def test_strategy_finder_a_ses_elements(ident):
    assert f'id="{ident}"' in HTML, ident


def test_strategy_finder_propose_les_objectifs_et_les_minimums_demandes():
    obj = HTML[HTML.index('id="sf-objectif"'):]
    obj = obj[:obj.index("</div>")]
    for valeur, libelle in (("balanced", "Équilibre"), ("clv", "CLV"), ("roi", "ROI")):
        assert f'data-objectif="{valeur}"' in obj and f">{libelle}<" in obj, valeur
    assert 'data-objectif="balanced" class="actif"' in obj, "Équilibre n'est pas le défaut"
    mins = HTML[HTML.index('id="sf-min-presets"'):]
    mins = mins[:mins.index("</div>")]
    assert re.findall(r'data-min="(\d+)"', mins) == ["50", "100", "250", "500", "1000"]
    assert 'data-min="100" class="actif"' in mins
    assert 'id="sf-min" min="1" step="1" value="100"' in HTML
    assert "Le ROI ne porte jamais que sur les paris réglés." in HTML


def test_strategy_finder_appelle_SON_endpoint_par_appel():
    assert "const API_STRATEGIES = '/api/strategies';" in JS
    corps = _corps("async function sfLancer")
    assert "appel(API_STRATEGIES, sfParametres())" in corps
    # Une réponse périmée ne remplace jamais la plus récente, et le bouton
    # est inactif pendant la recherche.
    assert "JETON_SF = jeton" in corps and "if (JETON_SF !== jeton) return;" in corps
    assert "b.disabled = true" in corps
    params = _corps("function sfParametres")
    for nom in ("'sport'", "'date_from'", "'date_to'", "'population'", "'min_n'", "'objective'"):
        assert nom in params, nom
    assert "if (sport) p.set('sport', sport)" in params, "« Tous les sports » doit omettre `sport`"


def test_strategy_finder_ne_recalcule_AUCUNE_metrique():
    """⚠️ Le classement, les parts et les écarts viennent du serveur. Compter
    les sous-périodes à CLV positive, ou soustraire l'entraînement de la
    validation, ferait une seconde définition sous la même étiquette."""
    bloc = _bloc_strategy_finder()
    code = "\n".join(l for l in bloc.splitlines()
                     if not l.strip().startswith(("*", "//", "/*")))
    assert not re.search(r"blocks\s*\)?\.filter\(", code), "des sous-périodes sont recomptées"
    assert not re.search(r"\.clv\s*-\s*\w+\.clv|\.roi\s*-\s*\w+\.roi", code), "un écart est recalculé"
    assert not re.search(r"\breduce\(|\bsum\s*\(", code)
    assert "st.clv_positive_share" in code and "st.measured_blocks" in code
    assert "pts(delta.clv)" in code and "pts(delta.roi)" in code


def test_strategy_finder_lit_la_reponse_du_serveur():
    bloc = _bloc_strategy_finder()
    for lecture in ("s.summary", "sm.settled", "s.validation", "v.sufficient", "s.robustness",
                    "r.level", "r.label", "s.sample", "d.counts", "d.split",
                    "d.warnings", "d.by_bookmaker", "d.by_market", "s.why",
                    "s.analytics_filters", "p.sport_label", "p.population_label",
                    "p.objective_label", "p.min_n", "p.stake"):
        assert lecture in bloc, lecture
    # Les cinq premières pistes DISTINCTES font les cartes ; toutes font le
    # tableau, variantes comprises (marquées « variante de #n »).
    assert "sfCartes(cartes)" in bloc and "s.variant_of" in bloc


def test_strategy_finder_cartes_criteres_dans_un_ordre_fixe():
    """Les critères autres que bookmaker et marché, dans l'ordre des
    dimensions (Pari, EV, Cote, Délai), et seulement ceux que la
    configuration restreint ; les autres sont nommés « Sans filtre »."""
    carte = _corps("function sfCarte(s)")
    assert "sfCriteresOrdonnes(s).filter((x) => ['outcome', 'ev', 'odds', 'delay']" in carte
    assert "Sans filtre : ${libre}" in carte
    code = "\n".join(l for l in carte.splitlines() if not l.strip().startswith("//"))
    assert "Toutes" not in code, "un critère absent ne s'écrit pas « Toutes »"


def test_les_infobulles_de_courbe_n_ecrivent_pas_un_champ_ABSENT():
    """Une semaine du Strategy Finder ne porte ni taux de règlement ni
    couverture : l'infobulle commune ne doit pas y écrire « (—) »."""
    corps = _corps("function lignesTranche")
    assert "t[k] === undefined ? '' : txt" in corps
    for champ in ("'settlement_rate'", "'clv_coverage'", "'sample'", "'sample_settled'"):
        assert f"si({champ}" in corps, champ


def test_strategy_finder_etat_vide_et_moins_de_cinq_resultats():
    assert "Aucune configuration suffisamment documentée." in HTML
    assert "Essayez d'augmenter la période ou de réduire le nombre minimum de paris." in HTML
    corps = _corps("function sfRendre")
    assert "$('sf-vide').hidden = !vide" in corps
    # Des pistes DISTINCTES : une variante (`variant_of`) n'occupe pas de carte.
    assert "liste.filter((s) => !s.variant_of).slice(0, 5)" in corps
    assert "cartes.length >= 5" in corps
    assert "configurations distinctes répondent aux critères." in corps
    assert "configuration distincte répond aux critères." in corps


def test_strategy_finder_chargement_et_erreur():
    corps = _corps("function sfAfficherEtat")
    assert "Analyse des configurations…" in corps
    assert "blocAvert(" in corps and ", true)" in corps, "l'erreur n'est pas un avertissement grave"
    # Pas de fausse progression : aucun pourcentage d'avancement inventé.
    assert "progress" not in corps.lower()


def test_strategy_finder_les_modales_sont_des_dialogues():
    for ident in ("sf-methode", "sf-detail"):
        assert re.search(rf'id="{ident}" role="dialog" aria-modal="true" aria-labelledby="[^"]+"', HTML), ident
    assert 'id="sf-voile"' in HTML
    ouvrir, fermer = _corps("function sfOuvrirModale"), _corps("function sfFermerModale")
    assert "$('app').inert = true" in ouvrir and "SF_OUVREUR" in ouvrir
    assert "$('app').inert = false" in fermer and "cible.focus()" in fermer
    # Échap ferme la modale, une couche à la fois, avant le tiroir.
    echap = JS[JS.index("if (e.key !== 'Escape') return;"):]
    echap = echap[:echap.index("});")]
    assert echap.index("sfFermerModale()") < echap.index("fermerTiroir()")


def test_strategy_finder_la_methode_vient_du_serveur_sinon_du_texte_fixe():
    assert "(SF && SF.method) || SF_METHODE" in _corps("function sfOuvrirMethode")
    for phrase in ("Valuebet analyse différentes combinaisons de bookmaker, marché, type de pari, EV, cote et délai.",
                   "Une partie de la période est conservée pour valider les configurations hors-échantillon."):
        assert phrase in JS, phrase


def test_strategy_finder_detail_graphes_et_intervalles():
    corps = _corps("function sfGraphes")
    assert "courbe(a, s.series || [], 'clv', 'CLV dans le temps')" in corps
    assert "'pnl_cumul', 'P&L cumulé', (v) => eur(v, 0)" in corps, "le P&L cumulé doit être en euros"
    detail = _corps("function sfRemplirDetail")
    for jeton in ("sfIntervalle(ci, ci.clv)", "sfIntervalle(ci, ci.roi)", "st.blocks",
                  "Pourquoi cette configuration ?", "Entraînement / Validation",
                  "Variation CLV", "Variation ROI", "Stabilité", "clv_coverage"):
        assert jeton in detail, jeton
    assert "IC ${num(ci.level)} % : [" in _corps("function sfIntervalle")


def test_strategy_finder_ouvre_la_configuration_dans_le_TIROIR():
    corps = _corps("function sfOuvrirAnalytics")
    for geste in ("$('reinit').click()", "poser('f-sports', f.sports)",
                  "poser('f-books', f.bookmakers)", "poser('f-markets', f.markets)",
                  "poser('f-outcomes', f.outcomes)", "appliquerModeEv('tranches', false)",
                  "poser('f-ev-bands', f.ev_bands)", "appliquerModeCote('tranches', false)",
                  "poser('f-odds-bands', f.odds_bands)", "$('f-delay-unite').value = 'h'",
                  "$('f-population').value = f.population", "signalerChangement()",
                  "allerA('vue-ensemble')", "analyser()"):
        assert geste in corps, geste
    assert "cocher(groupe, v)" in corps


def test_strategy_finder_export_csv_et_pdf():
    csv = _corps("function sfExporterCsv")
    assert "csvTexte(entetes, lignes)" in csv and "telecharger(" in csv
    assert "valuebet-strategies-${p.sport || 'tous'}-${de}_${a}.csv" in csv
    for colonne in ("'rank'", "'settled'", "'clv'", "'clv_n'", "'roi'", "'pnl'", "'stake_total'",
                    "'validation_clv'", "'validation_roi'", "'validation_settled'",
                    "'robustesse'", "'echantillon'"):
        assert colonne in csv, colonne
    assert "window.print()" in _corps("function sfImprimer")
    entete = _corps("function sfEntetePdf")
    for ligne in ("'Sport'", "'Période'", "'Population'", "'Minimum'", "'Mode'", "'Exporté le'"):
        assert ligne in entete, ligne
    assert 'class="carte impression-seule sf-entete-pdf" id="sf-entete-pdf"' in HTML


def test_strategy_finder_imprime_sans_formulaire_ni_entete_de_lanalyse():
    bloc = CSS[CSS.rindex("@media print"):]
    assert "#sf-formulaire" in bloc and ".sf-modale" in bloc
    assert 'body[data-page="strategies"]:not(.impression-rapport) #entete-pdf' in bloc
    assert "document.body.dataset.page = p" in _corps("function afficherPage")


def test_strategy_finder_la_robustesse_suit_les_JETONS_du_theme():
    for niveau, jeton in (("strong", "--bon"), ("medium", "--alerte"), ("weak", "--grave")):
        assert re.search(rf"\.sf-robustesse\.{niveau} \.sf-point \{{ background: var\({jeton}\); \}}", CSS), niveau
    # Le niveau devient une classe : il est borné aux trois valeurs du contrat.
    assert "['strong', 'medium', 'weak'].includes(r.level)" in JS


def test_strategy_finder_les_cartes_sont_accessibles_et_neutres():
    carte = _corps("function sfCarte(s)")
    assert "c.tabIndex = 0" in carte and "setAttribute('role', 'button')" in carte
    assert "e.key === 'Enter'" in carte
    assert ".sf-carte:focus-visible" in CSS
    regle = CSS[CSS.index(".sf-carte {"):]
    regle = regle[:regle.index("}")]
    assert "--bon" not in regle and "--grave" not in regle, "la carte elle-même ne doit pas être colorée"


def test_strategy_finder_le_tableau_se_trie_au_clavier_et_garde_les_absents_en_bas():
    corps = _corps("function sfTableau")
    assert "aria-sort" in corps and "th.tabIndex = 0" in corps
    assert "if (absA || absB) return absA === absB ? a.rank - b.rank : absA ? 1 : -1;" in corps
    assert '<div class="enrob"><table class="tableau sf-table" id="sf-table">' in HTML


def test_strategy_finder_vocabulaire_prudent_et_vouvoiement():
    """Une configuration « a présenté » une CLV passée : l'interface ne
    promet pas de gains et ne tutoie personne."""
    code = sans_namespace_svg(_bloc_strategy_finder())
    page = HTML[HTML.index('id="page-strategies"'):HTML.index("<!-- ═══ MES ANALYSES")]
    for source in (code, page):
        bas = source.lower()
        for interdit in ("va gagner", "meilleure stratégie", "garanti", "clique ", "essaie "):
            assert interdit not in bas, interdit


def test_strategy_finder_la_configuration_affiche_SES_criteres_et_ses_regles():
    """Le bloc Configuration lit `criteria` (valeur + règle exacte) et
    `unconstrained` ; l'EV moyenne reste dans l'Échantillon."""
    from src.analytics_ui.app import STATIQUES
    js = (STATIQUES / "app.js").read_text(encoding="utf-8")
    assert "function sfCriteresOrdonnes(s)" in js and "function sfSansFiltre(s)" in js
    assert "c.rule ? [c.rule] : null" in js
    assert "s.unconstrained" in js and "'Sans filtre'" in js
    detail = js[js.index("function sfOuvrirDetail"):]
    config = detail[detail.index("sfSection('Configuration'"):detail.index("sfSection('Échantillon'")]
    assert "ev_mean" not in config
    assert "['EV moyenne', pct(sm.ev_mean, 1)" in detail
    # Aucune tranche déduite d'une moyenne, aucune tranche écrite en dur.
    assert "8-15" not in js and "8–15" not in js


def test_strategy_finder_le_seuil_de_detection_s_affiche():
    """Le seuil d'EV imposé par le système apparaît avec les critères (en
    retrait), au lieu d'un « EV : sans filtre » trompeur."""
    from src.analytics_ui.app import STATIQUES
    js = (STATIQUES / "app.js").read_text(encoding="utf-8")
    css = (STATIQUES / "style.css").read_text(encoding="utf-8")
    assert "function sfTousCriteres(s)" in js and "s.implicit_criteria" in js
    assert js.count("sfTousCriteres(s)") >= 4, "détail, cartes, tableau et export"
    assert "x.implicit ? 'sf-implicite'" in js and ".sf-implicite" in css
