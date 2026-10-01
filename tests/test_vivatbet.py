"""Vivatbet — marque blanche 1xBet, flux JSON `games1x2`.

Les échantillons viennent des deux HAR réels du 28/09, allégés : 16 matchs de
football (dont les 4 faux matchs « Home vs Away » du flux), 10 de tennis, et le
menu des compétitions du football.
"""
from __future__ import annotations

import copy
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from src import orchestration as orch
from src.models import Book, MarketType, OddQuote, Outcome
from src.scrapers import vivatbet as vb

FIXTURES = Path(__file__).parent / "fixtures"
FOOT = json.loads((FIXTURES / "vivatbet_games1x2_soccer_sample.json").read_text(encoding="utf-8"))
TENNIS = json.loads((FIXTURES / "vivatbet_games1x2_tennis_sample.json").read_text(encoding="utf-8"))
MENU = json.loads((FIXTURES / "vivatbet_leftmenu_soccer_sample.json").read_text(encoding="utf-8"))


def _par_match(quotes):
    out: dict[str, dict] = {}
    for q in quotes:
        out.setdefault(q.source_event_id, {})[(q.market, q.outcome.label, q.outcome.line)] = q.decimal_odd
    return out


# ── Le parseur ────────────────────────────────────────────────────────

def test_le_1X2_et_le_total_sont_lus_tels_que_le_site_les_sert():
    """Belgique–France, relevé dans le HAR : 3,98 / 3,98 / 1,891 et total 2,5
    à 1,619 / 2,419."""
    m = _par_match(vb.parse_games(FOOT))["754873915"]
    assert m[(MarketType.H2H, "home", None)] == 3.98
    assert m[(MarketType.H2H, "draw", None)] == 3.98
    assert m[(MarketType.H2H, "away", None)] == 1.891
    assert m[(MarketType.TOTALS, "over", 2.5)] == 1.619
    assert m[(MarketType.TOTALS, "under", 2.5)] == 2.419
    assert len(m) == 5, "un marché non suivi (double chance, handicap…) est sorti"


def test_les_noms_ANGLAIS_forment_la_cle_et_la_ligue_suit():
    q = next(q for q in vb.parse_games(FOOT) if q.source_event_id == "754873915")
    assert "belgium" in q.event_key and "france" in q.event_key
    assert q.league == "UEFA Nations League"
    assert q.book is Book.VIVATBET
    # startTs 1790621100 = 28/09/2026 18:45 UTC.
    assert q.event_key.startswith("202609281845::")


def test_les_faux_matchs_HOME_VS_AWAY_ne_sortent_JAMAIS():
    """Un pari sur les statistiques de toute une journée (« 10 matchs », total
    de buts 26,5), servi DANS la Ligue des nations. S'il passait, son total
    serait comparé à un vrai match."""
    speciaux = {str(g["id"]) for g in FOOT if g.get("homeAwayFlag")}
    assert len(speciaux) == 4
    quotes = list(vb.parse_games(FOOT))
    assert not speciaux & {q.source_event_id for q in quotes}
    assert all("home" not in q.event_key.split("::")[1].split("__vs__")[0] for q in quotes)
    assert max(q.outcome.line for q in quotes if q.market is MarketType.TOTALS) < 8


def test_un_nom_special_suffit_meme_sans_le_drapeau():
    jeu = copy.deepcopy(FOOT[0])
    jeu["opponent1"]["fullNameEng"] = "Home (Special bets)"
    jeu.pop("homeAwayFlag", None)
    assert vb.est_pari_special(jeu)
    assert not list(vb.parse_games([jeu]))


def test_une_competition_speciale_est_ecartee():
    jeu = copy.deepcopy(FOOT[0])
    jeu["liga"]["nameEng"] = "UEFA Nations League. League A. Matchday statistics"
    assert not list(vb.parse_games([jeu]))


def test_une_cote_BLOQUEE_nest_pas_publiee():
    jeu = copy.deepcopy(FOOT[0])
    for eg in jeu["eventGroups"]:
        if eg["groupId"] == 1:
            eg["events"][0][0]["blocked"] = True
    m = _par_match(vb.parse_games([jeu]))[str(jeu["id"])]
    assert (MarketType.H2H, "home", None) not in m
    assert (MarketType.H2H, "away", None) in m


def test_une_MI_TEMPS_ne_se_lit_jamais_comme_le_match():
    """Les périodes arrivent dans `subGamesForMainGame`, avec le même
    `groupId 1` qu'un 1X2 de match."""
    jeu = copy.deepcopy(FOOT[0])
    jeu["subGamesForMainGame"] = [{"eventGroups": [{"groupId": 1, "events": [
        [{"type": 1, "cf": 99.0}], [{"type": 2, "cf": 99.0}], [{"type": 3, "cf": 99.0}]]}]}]
    assert all(q.decimal_odd != 99.0 for q in vb.parse_games([jeu]))


@pytest.mark.parametrize("ligne", [2.0, 2.25, 3.0])
def test_seules_les_lignes_en_VIRGULE_CINQ_passent(ligne):
    jeu = copy.deepcopy(FOOT[0])
    for eg in jeu["eventGroups"]:
        if eg["groupId"] == 17:
            for col in eg["events"]:
                for ev in col:
                    ev["parameter"] = ligne
    assert not [q for q in vb.parse_games([jeu]) if q.market is MarketType.TOTALS]


def test_le_tennis_a_un_vainqueur_SANS_NUL_et_des_totaux_en_jeux():
    quotes = list(vb.parse_games(TENNIS))
    assert quotes
    h2h = {q.outcome.label for q in quotes if q.market is MarketType.H2H}
    assert h2h == {"home", "away"}
    lignes = {q.outcome.line for q in quotes if q.market is MarketType.TOTALS}
    assert lignes and min(lignes) > 15, lignes   # des JEUX, pas des sets
    shub = _par_match(quotes)["756536402"]
    assert shub[(MarketType.H2H, "home", None)] == 1.019
    assert shub[(MarketType.H2H, "away", None)] == 9.4


def test_les_rejets_sont_comptes():
    c = vb.compte_rejets(FOOT)
    assert c["annonces"] == 16 and c["speciaux"] == 4 and c["retenus"] == 12


# ── Le menu des compétitions ──────────────────────────────────────────

def test_le_menu_rend_les_competitions_DIRECTES_et_celles_des_PAYS():
    ligues = {lid: (nom, n) for lid, nom, n in vb.leagues_from_menu(MENU, "soccer")}
    # Directes, hors spéciales et « Team vs Player ».
    for lid in (1706694, 118587, 118593, 2252762):
        assert lid in ligues
    for lid in (1413697, 2819162, 2144188, 2144166):
        assert lid not in ligues, ligues.get(lid)
    # Regroupées sous un pays (« Angleterre », « Allemagne ») : 3 + 3 dans
    # l'échantillon. Les oublier perdait 138 des 188 compétitions de football.
    assert len(ligues) == 4 + 6
    assert 33 not in ligues and 69 not in ligues, "un PAYS a été pris pour une compétition"


def test_un_sport_non_couvert_ne_rend_rien():
    assert vb.leagues_from_menu(MENU, "hockey") == []


# ── Les appels ────────────────────────────────────────────────────────

def _scraper_espion(reponse):
    vus = []

    def gerer(req: httpx.Request) -> httpx.Response:
        vus.append(req)
        return httpx.Response(200, json=reponse)

    sc = vb.VivatbetScraper()
    sc._client = httpx.Client(transport=httpx.MockTransport(gerer), headers=vb._headers())
    return sc, vus


def test_les_vedettes_demandent_le_plafond_de_50_et_le_bon_sport():
    sc, vus = _scraper_espion([])
    sc.fetch_top("tennis")
    p = vus[0].url.params
    assert vus[0].url.path.endswith("/main-line-feed/v3/games1x2")
    assert p["count"] == "50" and p["selectedMs"] == "2.4"
    assert p["gr"] == "704" and p["fcountry"] == "24"


@pytest.mark.parametrize("appel", ["fetch_top", "fetch_leagues", "fetch_league"])
def test_les_parametres_partent_dans_lordre_ALPHABETIQUE(appel):
    """Le flux refuse (400) les mêmes paramètres dans un autre ordre — c'est
    ce qui a fait échouer la première sonde sur la VM."""
    sc, vus = _scraper_espion([])
    getattr(sc, appel)(*(("soccer", 88637) if appel == "fetch_league" else ("soccer",)))
    cles = [k for k, _ in vus[0].url.params.multi_items()]
    assert cles == sorted(cles), cles


def test_lurl_des_vedettes_est_celle_du_curl_qui_a_marche():
    """L'URL exacte que la VM a reçue en 200 (`count=50`, football)."""
    sc, vus = _scraper_espion([])
    sc.fetch_top("soccer")
    assert str(vus[0].url).split("?", 1)[1] == (
        "cfView=3&count=50&fcountry=24&gr=704&grMode=4&lng=fr&ref=282&selectedMs=2.1")


def test_une_competition_se_demande_par_son_identifiant():
    sc, vus = _scraper_espion([])
    sc.fetch_league("soccer", 88637)
    assert vus[0].url.params["selectedMs"] == "2.1.88637"


def test_aucun_jeton_x_hd_nest_envoye():
    """La VM a reçu 200 sans lui : rejouer un jeton de session d'un
    navigateur n'a aucune raison d'être."""
    sc, vus = _scraper_espion([])
    sc.fetch_top("soccer")
    assert "x-hd" not in {k.lower() for k in vus[0].headers}
    assert vus[0].headers["x-svc-source"] == "__BETTING_APP__"


def test_le_plafond_de_50_nest_pas_depasse():
    assert vb.MAX_COUNT == 50


def test_un_sport_inconnu_leve_au_lieu_de_demander_nimporte_quoi():
    sc, _ = _scraper_espion([])
    with pytest.raises(ValueError):
        sc.fetch_top("basketball")


# ── Le cache de fond (orchestration) ──────────────────────────────────

@pytest.fixture
def cache_vierge():
    orch._VIVAT_DEEP_CACHE.clear()
    orch._VIVAT_DEEP_REFRESHING.clear()
    yield
    orch._VIVAT_DEEP_CACHE.clear()
    orch._VIVAT_DEEP_REFRESHING.clear()


def _q(cle="k", label="home", odd=2.0, line=None):
    return OddQuote(event_key=cle, book=Book.VIVATBET, market=MarketType.H2H,
                    outcome=Outcome(label=label, line=line), decimal_odd=odd,
                    fetched_at=datetime.now(timezone.utc), source_event_id="x")


def test_le_cycle_nattend_jamais_le_balayage(cache_vierge, monkeypatch):
    lances = []
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: lances.append(k)})())
    t0 = time.perf_counter()
    assert orch._vivatbet_deep_quotes("soccer") == []
    assert time.perf_counter() - t0 < 0.5
    assert lances, "le balayage de fond n'a pas été lancé"


def test_un_cache_trop_vieux_rend_RIEN(cache_vierge, monkeypatch):
    monkeypatch.setattr(threading, "Thread",
                        lambda *a, **k: type("T", (), {"start": lambda s: None})())
    vieux = time.monotonic() - orch._VIVAT_DEEP_MAX_AGE - 1
    orch._VIVAT_DEEP_CACHE["soccer"] = (vieux, [_q()])
    assert orch._vivatbet_deep_quotes("soccer") == []


def test_les_vedettes_FRAICHES_gagnent_sur_le_cache(cache_vierge, monkeypatch):
    frais = _q(odd=2.10)
    orch._VIVAT_DEEP_CACHE["soccer"] = (time.monotonic(), [_q(odd=1.90), _q(cle="autre")])

    class Faux:
        def __enter__(self): return self
        def __exit__(self, *a): return None
        def fetch_top(self, sport): return []

    monkeypatch.setattr(orch, "VivatbetScraper", Faux)
    monkeypatch.setattr(orch, "vivatbet_parse_games", lambda payload, sport=None: iter([frais]))
    quotes = orch.fetch_vivatbet_quotes("soccer")
    prix = {(q.event_key, q.outcome.label): q.decimal_odd for q in quotes}
    assert prix[("k", "home")] == 2.10, "une cote de fond a écrasé la cote fraîche"
    assert ("autre", "home") in prix, "le cache n'a rien ajouté"


def test_vivatbet_couvre_football_tennis_et_le_1x2_reglementaire_du_hockey():
    """Le hockey est entré le 01/10, en temps réglementaire seulement (voir
    `test_hockey.py`) — pas un sport de plus « par défaut »."""
    assert set(orch.VIVATBET_SPORTS) == {"soccer", "tennis", "hockey"}


# ── Le faux rapprochement du 28/09 et les noms de l'alerte ────────────

def test_une_equipe_parfaite_ne_rachete_plus_une_equipe_fausse():
    """Vivatbet « Faroe Islands vs Slovakia » avait été apparié à Pinnacle
    « England vs Slovakia », même horaire : Slovakia à 100, « faroeislands »
    / « england » à 71 (le « lands » des clés sans espaces), moyenne 85,7.
    L'alerte est partie à +424 % d'EV sur la cote de Féroé comparée à la
    cote juste de l'Angleterre."""
    from src.matcher import reconcile_event_keys
    assert reconcile_event_keys(["202610021845::england__vs__slovakia"],
                                ["202610021845::faroeislands__vs__slovakia"]) == {}


def test_le_plancher_par_equipe_garde_les_vraies_variantes():
    from src.matcher import reconcile_event_keys
    for ref, cand in (("atleticomadrid__vs__slovakia", "atlmadrid__vs__slovakia"),
                      ("nottinghamforest__vs__chelsea", "nottmforest__vs__chelsea"),
                      ("saintetienne__vs__lyon", "asstetienne__vs__lyon")):
        assert reconcile_event_keys([f"202610021845::{ref}"], [f"202610021845::{cand}"]), cand


def test_lalerte_montre_les_noms_FRANCAIS_du_site():
    """Le rapprochement se fait en anglais (la langue de Pinnacle) ; l'alerte
    montre ce que le site affiche, pour qu'on retrouve le match sur
    vivatbet.be — « Belgique », pas « Belgium »."""
    from src import teams
    from src.alerter import format_value_bet
    from src.models import ValueBet

    # La ValueBet est construite à la main : passer par la détection rendrait
    # le test dépendant de l'heure (un match commencé n'est plus détecté).
    q = next(q for q in vb.parse_games(FOOT)
             if q.source_event_id == "754873915" and q.outcome.label == "home")
    assert "belgium" in q.event_key
    assert teams.noms_du_match(Book.VIVATBET, q.event_key) == ("Belgique", "France")
    bet = ValueBet(event_key=q.event_key, book=Book.VIVATBET, market=q.market,
                   outcome=q.outcome, odd_taken=q.decimal_odd, fair_prob=0.30,
                   fair_odd=3.33, ev_pct=19.4, kelly_stake_pct=1.2,
                   detected_at=datetime.now(timezone.utc), league=q.league)
    msg = format_value_bet(bet)
    assert "Belgique vs France" in msg and "Belgium" not in msg
    assert "Pari : <b>Home</b> - 3.98 (fair 3.33)" in msg


def test_la_sonde_d_appariement_signale_un_faux_appariement(tmp_path, capsys):
    """Le cas Féroé : rapprochement flou, EV +424 %. Une fausse EV donne une
    fausse CLV du même montant — seule la ligne, pas la moyenne, le montre."""
    import sqlite3

    from scripts.appariement_book import main
    from src.storage import Storage
    db = tmp_path / "t.db"
    Storage(str(db))
    c = sqlite3.connect(db)
    c.execute("INSERT INTO events VALUES ('ek1','soccer','L','Faroe Islands','England',"
              "'2026-09-30T18:00:00')")
    c.execute("INSERT INTO value_bets(event_key,book,market,outcome_label,line,odd_taken,"
              "fair_prob,fair_odd,ev_pct,kelly_pct,detected_at) VALUES "
              "('ek1','vivatbet','h2h','home',NULL,6.44,0.81,1.23,424,3,'2026-09-29T10:00:00')")
    c.execute("INSERT INTO bet_features(value_bet_id,detected_at,event_key,book,market,"
              "outcome_label,odd_taken,fair_odd,ev_pct,match_score,time_shift_min) VALUES "
              "(1,'2026-09-29T10:00:00','ek1','vivatbet','h2h','home',6.44,1.23,424,85.7,0)")
    c.commit()
    main(["--db", str(db)])
    out = capsys.readouterr().out
    assert "appariement flou  (score < 100):     1" in out
    assert "Faroe Islands - England" in out and "appariement flou 86" in out and "EV +424.0 %" in out
