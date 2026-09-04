"""Le jeton invité de MeridianBet, et les quatre façons de le rater.

L'offre exige `Authorization: Bearer` ; sans lui l'API répond
`401 {"error":"invalid_token"}`. C'est ce qui tenait ce book désactivé — et
non l'anti-bot TrafficGuard auquel on l'attribuait, un 401 étant un refus
d'authentification et non un filtrage d'ASN.

Le jeton se prend dans le `<script id="ng-state">` d'une page du site, sous la
clé `NEW_TOKEN`. Quatre pièges, tous testés ici :

  1. `NEW_TOKEN` est du JSON ENCODÉ DANS UNE CHAÎNE. Un `.get()` direct rend la
     chaîne entière et l'en-tête part invalide — l'API répond 401 et le book
     paraît cassé alors que le jeton était là.
  2. L'identifiant du script CHANGE selon la variante servie (`ng-state` hors
     mobile, `meridianbet-mobile-v4-state` sur mobile).
  3. Le cache est PARTAGÉ entre les fils : `fetch_all_parallel` lance un fil
     par sport, et sans verrou chacun chargerait sa propre page.
  4. Sur 401 il faut rejouer UNE fois, pas boucler.
"""
from __future__ import annotations

import json
import time

import httpx
import pytest

import src.scrapers.meridianbet as M


def _jwt(exp: float) -> str:
    import base64
    def b64(o):
        return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
    return f"{b64({'typ': 'JWT'})}.{b64({'exp': int(exp), 'scope': ['GENERAL']})}.sig"


def _page(jeton: str, ident: str = "ng-state", encode: bool = True) -> str:
    charge = {"access_token": jeton, "refresh_token": "r" + jeton}
    etat = {"NEW_TOKEN": json.dumps(charge) if encode else charge, "autre": 1}
    return f'<html><script id="{ident}" type="application/json">{json.dumps(etat)}</script></html>'


class _FauxClient:
    """Remplace httpx.Client : sert la page, puis compte les appels d'API."""

    def __init__(self, page: str, statuts: list[int] | None = None):
        self.page = page
        self.statuts = list(statuts or [200])
        self.pages_chargees = 0
        self.appels_api: list[str] = []

    def get(self, url, params=None, headers=None):
        if url.startswith("https://meridiansports.be"):
            self.pages_chargees += 1
            return httpx.Response(200, text=self.page,
                                  request=httpx.Request("GET", url))
        self.appels_api.append((headers or {}).get("Authorization", ""))
        code = self.statuts.pop(0) if self.statuts else 200
        return httpx.Response(code, json={"payload": {"leagues": []}},
                              request=httpx.Request("GET", url))


@pytest.fixture(autouse=True)
def _vide_le_cache():
    M._JETON.update(valeur="", expire=0.0)
    yield
    M._JETON.update(valeur="", expire=0.0)


def _scraper(client) -> M.MeridianScraper:
    s = M.MeridianScraper.__new__(M.MeridianScraper)
    s._client = client
    return s


# ── 1. le JSON encodé dans une chaîne ──────────────────────────────────────

def test_le_jeton_est_extrait_du_json_encode_en_chaine():
    t = _jwt(time.time() + 3600)
    s = _scraper(_FauxClient(_page(t)))
    assert s._prendre_jeton() == t, "la chaîne JSON doit être décodée"


def test_un_new_token_deja_decode_marche_aussi():
    """Défensif : si le site cesse un jour d'encoder, on ne doit pas tomber."""
    t = _jwt(time.time() + 3600)
    s = _scraper(_FauxClient(_page(t, encode=False)))
    assert s._prendre_jeton() == t


def test_la_chaine_entiere_n_est_jamais_renvoyee_telle_quelle():
    """C'est l'erreur qui ferait repartir un 401 en paraissant fonctionner."""
    t = _jwt(time.time() + 3600)
    s = _scraper(_FauxClient(_page(t)))
    obtenu = s._prendre_jeton()
    assert "access_token" not in obtenu, f"chaîne brute renvoyée : {obtenu[:60]}"
    assert obtenu.count(".") == 2, "un JWT a exactement deux points"


# ── 2. les deux identifiants de script ─────────────────────────────────────

@pytest.mark.parametrize("ident", ["ng-state", "meridianbet-mobile-v4-state"])
def test_les_deux_variantes_de_page_sont_lues(ident):
    t = _jwt(time.time() + 3600)
    assert _scraper(_FauxClient(_page(t, ident=ident)))._prendre_jeton() == t


# ── 3. le cache ────────────────────────────────────────────────────────────

def test_le_jeton_est_mis_en_cache_entre_deux_appels():
    c = _FauxClient(_page(_jwt(time.time() + 3600)))
    s = _scraper(c)
    s._prendre_jeton(); s._prendre_jeton(); s._prendre_jeton()
    assert c.pages_chargees == 1, "une seule page pour trois demandes"


def test_le_cache_est_partage_entre_instances():
    """Un fil par sport : chacun a SON scraper mais doit partager le jeton."""
    page = _page(_jwt(time.time() + 3600))
    c1, c2 = _FauxClient(page), _FauxClient(page)
    _scraper(c1)._prendre_jeton()
    _scraper(c2)._prendre_jeton()
    assert (c1.pages_chargees, c2.pages_chargees) == (1, 0)


def test_un_jeton_bientot_perime_est_renouvele():
    """La marge évite d'utiliser un jeton qui expire pendant la requête."""
    c = _FauxClient(_page(_jwt(time.time() + 60)))   # expire dans 60 s
    s = _scraper(c)
    s._prendre_jeton(); s._prendre_jeton()
    assert c.pages_chargees == 2, "sous la marge, il faut le reprendre"


def test_l_expiration_vient_du_jeton_et_non_d_une_constante():
    """Coder « une heure » en dur casserait en silence le jour où ils la
    raccourcissent."""
    exp = time.time() + 12345
    _scraper(_FauxClient(_page(_jwt(exp))))._prendre_jeton()
    assert M._JETON["expire"] == pytest.approx(exp, abs=1)


# ── 4. le rejeu sur 401 ────────────────────────────────────────────────────

def test_un_401_declenche_un_seul_rejeu_avec_un_jeton_neuf():
    c = _FauxClient(_page(_jwt(time.time() + 3600)), statuts=[401, 200])
    s = _scraper(c)
    s._get("/v1/offer/sport/58/leagues")
    assert len(c.appels_api) == 2, "un rejeu, pas plus"
    assert c.pages_chargees == 2, "le second appel doit porter un jeton NEUF"


def test_un_401_persistant_ne_boucle_pas():
    c = _FauxClient(_page(_jwt(time.time() + 3600)), statuts=[401, 401])
    s = _scraper(c)
    with pytest.raises(httpx.HTTPStatusError):
        s._get("/v1/offer/sport/58/leagues")
    assert len(c.appels_api) == 2


def test_l_entete_bearer_est_bien_pose():
    t = _jwt(time.time() + 3600)
    c = _FauxClient(_page(t))
    _scraper(c)._get("/v1/offer/sport/58/leagues")
    assert c.appels_api == [f"Bearer {t}"]


# ── 5. les échecs doivent CRIER, pas rendre du vide ────────────────────────

def test_une_page_sans_etat_leve_une_erreur_explicite():
    s = _scraper(_FauxClient("<html>rien du tout</html>"))
    with pytest.raises(RuntimeError, match="aucun <script id>"):
        s._prendre_jeton()


def test_un_etat_sans_access_token_leve_une_erreur_explicite():
    page = '<html><script id="ng-state" type="application/json">{"NEW_TOKEN":"{}"}</script></html>'
    with pytest.raises(RuntimeError, match="sans access_token"):
        _scraper(_FauxClient(page))._prendre_jeton()


def test_l_origine_ne_porte_pas_le_www():
    """Le navigateur envoie `meridiansports.be` ; une origine qui ne
    correspond pas est ce qu'un anti-bot vérifie en premier."""
    h = M._headers()
    assert h["Origin"] == "https://meridiansports.be"
    assert h["Referer"] == "https://meridiansports.be/"


# ── 6. le parseur, sur la forme RÉELLE relevée dans la capture ─────────────

def _charge_reelle() -> dict:
    """Reproduit exactement la forme observée le 04/09 dans `ng-state` et dans
    `GET /betshop/api/v2/events/{id}` : `positions[].groups[].selections[]`,
    `overUnder`/`handicap` portés par le GROUPE et non par la sélection."""
    return {"payload": {"leagues": [{
        "leagueName": "First Division A",
        "events": [{
            "header": {
                "eventId": 19586084,
                "startTime": 1788616800000,
                "rivals": ["Royal Charleroi SC", "Union Saint-Gilloise"],
                "sport": {"sportId": 58, "name": "Football"},
                "league": {"leagueId": 132, "name": "First Division A"},
                "betradar": {"id": "72221244"},
            },
            "positions": [{"index": 0, "groups": [
                {"overUnder": None, "handicap": None, "selections": [
                    {"selectionId": "19586084_719386224_0", "gameTemplateId": 3999,
                     "price": 3.25, "state": "ACTIVE", "name": "1"},
                    {"selectionId": "19586084_719386224_1", "gameTemplateId": 3999,
                     "price": 3.4, "state": "ACTIVE", "name": "X"},
                    {"selectionId": "19586084_719386224_2", "gameTemplateId": 3999,
                     "price": 2.15, "state": "ACTIVE", "name": "2"},
                ]},
                {"overUnder": 2.5, "handicap": None, "selections": [
                    {"selectionId": "19586084_719849960_0", "price": 1.83,
                     "state": "ACTIVE", "name": "Under"},
                    {"selectionId": "19586084_719849960_1", "price": 1.87,
                     "state": "ACTIVE", "name": "Over"},
                ]},
                # Handicap : volontairement IGNORÉ (conventions de signe non
                # normalisées côté softs, cf. §21.13).
                {"overUnder": None, "handicap": -1.5, "selections": [
                    {"selectionId": "x_0", "price": 2.9, "name": "1"},
                ]},
            ]}],
        }],
    }]}}


def test_le_parseur_rend_le_1x2_et_les_totaux():
    from src.models import Book, MarketType
    q = list(M.parse_offer(_charge_reelle()))
    h2h = {x.outcome.label: x.decimal_odd for x in q if x.market == MarketType.H2H}
    assert h2h == {"home": 3.25, "draw": 3.4, "away": 2.15}
    tot = {(x.outcome.label, x.outcome.line): x.decimal_odd
           for x in q if x.market == MarketType.TOTALS}
    assert tot == {("under", 2.5): 1.83, ("over", 2.5): 1.87}
    assert all(x.book == Book.MERIDIAN_BE for x in q)


def test_la_ligne_des_totaux_vient_du_GROUPE_pas_de_la_selection():
    """`overUnder` est porté par le groupe. Le chercher sur la sélection
    rendrait des totaux sans ligne — inutilisables pour l'appariement."""
    from src.models import MarketType
    q = [x for x in M.parse_offer(_charge_reelle()) if x.market == MarketType.TOTALS]
    assert q and all(x.outcome.line == 2.5 for x in q)


def test_les_handicaps_sont_ignores():
    from src.models import MarketType
    q = list(M.parse_offer(_charge_reelle()))
    assert not [x for x in q if x.market == MarketType.HANDICAP]
    assert len(q) == 5, "3 issues de 1X2 + 2 de total, le handicap écarté"


def test_l_identifiant_d_evenement_de_la_source_est_conserve():
    """`source_event_id` est ce qui permettra un jour d'apparier autrement que
    par similarité de noms."""
    q = list(M.parse_offer(_charge_reelle()))
    assert {x.source_event_id for x in q} == {"19586084"}


def test_un_evenement_sans_horaire_est_ecarte_sans_planter():
    charge = _charge_reelle()
    del charge["payload"]["leagues"][0]["events"][0]["header"]["startTime"]
    assert list(M.parse_offer(charge)) == []
