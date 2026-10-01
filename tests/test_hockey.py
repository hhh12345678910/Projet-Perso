"""Le hockey : collecté pour mesurer sa CLV, jamais alerté tant qu'elle ne
l'est pas, et jamais comparé à Pinnacle sur un marché « temps réglementaire »."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src import alerter, hockey
from src.models import Book, MarketType, OddQuote, Outcome
from src.scrapers import goldenpalace, ladbrokes, starcasinosport

DEBUT = datetime.now(timezone.utc) + timedelta(days=1)


# ── La sourdine par sport ─────────────────────────────────────────────

def test_le_hockey_est_EN_SOURDINE_par_defaut(monkeypatch):
    monkeypatch.delenv("SPORTS_ALERT_OFF", raising=False)
    assert alerter.sport_muet("hockey")
    assert not alerter.sport_muet("soccer") and not alerter.sport_muet("tennis")
    assert not alerter.sport_muet(None), "un appelant sans sport garde son comportement"


def test_la_sourdine_se_regle_par_l_environnement(monkeypatch):
    monkeypatch.setenv("SPORTS_ALERT_OFF", "")
    assert not alerter.sport_muet("hockey")
    monkeypatch.setenv("SPORTS_ALERT_OFF", "hockey, Tennis")
    assert alerter.sport_muet("tennis") and alerter.sport_muet("HOCKEY")


@pytest.mark.parametrize("envoi", [
    lambda cfg: alerter.send_alerts(["pari"], cfg, sport="hockey"),
    lambda cfg: alerter.send_surebet_alerts(["sb"], cfg, sport="hockey"),
    lambda cfg: alerter.send_middle_alerts(["m"], cfg, sport="hockey"),
    lambda cfg: alerter.send_clv_alerts([("b", 1, 2, 3)], cfg, sport="hockey"),
    lambda cfg: alerter.send_late_market_alerts([("ek", "b", [], 5)], cfg, sport="hockey"),
    lambda cfg: alerter.send_live_observation(["o"], cfg, sport="hockey"),
])
def test_AUCUNE_voie_d_envoi_ne_sort_un_sport_en_sourdine(monkeypatch, envoi):
    """Aucun message, et rien de marqué « envoyé » : la configuration n'est
    même pas lue (un objet vide ferait planter la moindre lecture)."""
    monkeypatch.delenv("SPORTS_ALERT_OFF", raising=False)
    assert envoi(object()) in ([], 0)


def test_scan_ecarte_un_sport_en_sourdine():
    import inspect

    import bot_listener
    corps = inspect.getsource(bot_listener.fetch_playable)
    assert "if not sport_muet(r[\"sport\"])" in corps


# ── Le verrou central ─────────────────────────────────────────────────

def _q(book, market, label="home", line=None):
    return OddQuote(event_key="ek", book=book, market=market,
                    outcome=Outcome(label=label, line=line), decimal_odd=2.0,
                    fetched_at=DEBUT, source_event_id="1")


def test_seuls_les_marches_VERIFIES_passent():
    """Chaque book n'entre qu'avec les familles relevées par la sonde du 01/10."""
    gardees, ecartees = hockey.filtrer([
        _q(Book.PINNACLE, MarketType.H2H),
        _q(Book.PINNACLE, MarketType.TOTALS_REG, "over", 5.5),
        _q(Book.LADBROKES_BE, MarketType.H2H),
        _q(Book.LADBROKES_BE, MarketType.TOTALS, "over", 5.5),
        _q(Book.LADBROKES_BE, MarketType.H2H_REG),          # n'existe pas chez lui
        _q(Book.GOLDEN_PALACE, MarketType.H2H_REG, "draw"),
        _q(Book.UNIBET_BE, MarketType.H2H_REG, "draw"),
        _q(Book.UNIBET_BE, MarketType.TOTALS_REG, "over", 5.5),
        _q(Book.UNIBET_BE, MarketType.H2H),                 # Unibet n'a pas de prolongation incluse
        _q(Book.NAPOLEON_BE, MarketType.H2H_REG),
        _q(Book.NAPOLEON_BE, MarketType.H2H),
        _q(Book.VIVATBET, MarketType.H2H_REG),
        _q(Book.VIVATBET, MarketType.TOTALS, "over", 5.5),  # total non vérifié
        _q(Book.BETFIRST, MarketType.H2H),
        _q(Book.BETFIRST, MarketType.TOTALS, "over", 5.5),  # GOU / TGOUOT non vérifiés
        _q(Book.MERIDIAN_BE, MarketType.H2H_REG),           # pas dans la liste
    ])
    assert len(gardees) == 10
    assert ecartees == {"ladbrokes_be": 1, "unibet_be": 1, "napoleon_be": 1,
                        "vivatbet": 1, "betfirst": 1, "meridian_be": 1}


def test_les_sept_books_et_pinnacle():
    assert {b.value for b in hockey.MARCHES_VERIFIES} == {
        "pinnacle", "ladbrokes_be", "golden_palace", "starcasino_sport", "betfirst",
        "unibet_be", "napoleon_be", "vivatbet"}


def test_le_verrou_est_branche_dans_la_collecte():
    import inspect

    from src import orchestration
    corps = inspect.getsource(orchestration.fetch_all_parallel)
    assert "if sport == hockey.SPORT:" in corps and "hockey.filtrer(all_quotes)" in corps


# ── Altenar : le 1X2 réglementaire n'entre jamais ─────────────────────

def _altenar(types):
    """Un match, un marché par typeId donné. Issues : 1/2/3 pour un
    vainqueur, 12/13 (« Plus de 5.5 ») pour un total."""
    markets, odds, oid = [], [], 0
    for mid, t in enumerate(types, start=1):
        ids = []
        issues = [(12, "Plus de 5.5"), (13, "Moins de 5.5")] if t in (18, 412) \
            else [(1, "1"), (2, "X"), (3, "2")] if t == 1 else [(1, "1"), (3, "2")]
        for typ, nom in issues:
            oid += 1
            odds.append({"id": oid, "typeId": typ, "price": 2.1, "oddStatus": 0, "name": nom})
            ids.append(oid)
        markets.append({"id": mid, "typeId": t, "oddIds": ids})
    return {"events": [{"id": 9, "competitorIds": [1, 2], "name": "Rangers - Bruins",
                        "startDate": DEBUT.isoformat(), "marketIds": [m["id"] for m in markets]}],
            "competitors": [{"id": 1, "name": "New York Rangers"}, {"id": 2, "name": "Boston Bruins"}],
            "markets": markets, "odds": odds}


def test_altenar_hockey_separe_les_deux_familles():
    """406/412 prolongation incluse, 1 en temps réglementaire, 18 dehors."""
    q = list(goldenpalace.parse_get_events(_altenar([1, 406, 18, 412]), sport="hockey"))
    assert sorted((x.market.value, x.outcome.label) for x in q) == sorted([
        ("h2h", "home"), ("h2h", "away"), ("totals", "over"), ("totals", "under"),
        ("h2h_reg", "home"), ("h2h_reg", "draw"), ("h2h_reg", "away")])
    assert not any(x.market == MarketType.H2H and x.outcome.label == "draw" for x in q)


def test_altenar_le_nul_suspendu_ne_fait_pas_passer_le_reglementaire():
    """Le cas qui fabriquait l'EV fictive : le 1X2 sans son nul. Il reste en
    `h2h_reg`, à deux issues face aux trois de la période 6 : la détection
    l'écarte — et il ne touche JAMAIS le vainqueur prolongation incluse."""
    p = _altenar([1])
    p["odds"][1]["oddStatus"] = 1          # le nul suspendu
    q = list(goldenpalace.parse_get_events(p, sport="hockey"))
    assert {x.market for x in q} == {MarketType.H2H_REG} and len(q) == 2


def test_altenar_le_football_ne_change_pas():
    q = list(goldenpalace.parse_get_events(_altenar([1, 18])))
    assert len(q) == 5 and {x.outcome.label for x in q} == {"home", "draw", "away", "over", "under"}


def test_starcasino_suit_la_meme_regle():
    q = list(starcasinosport.parse_get_events(_altenar([1, 406]), sport="hockey"))
    assert len(q) == 5 and all(x.book == Book.STARCASINO_SPORT for x in q)
    assert {x.market for x in q} == {MarketType.H2H, MarketType.H2H_REG}


# ── Ladbrokes : le vainqueur 478, rien d'autre ────────────────────────

def _ladbrokes(groupes):
    return {"result": {"events": [{
        "eventInfo": {"eventCode": 1, "eventData": DEBUT.timestamp() * 1000,
                      "teamHome": {"description": "Rangers"},
                      "teamAway": {"description": "Bruins"}},
        "betGroupList": [{"oddGroupList": [
            {"betId": bid, "alternativeDescription": alt, "oddGroupDescription": desc,
             "oddList": [{"boxTitle": b, "oddValue": 210} for b in boites]}
            for bid, alt, desc, boites in groupes]}],
    }]}}


def test_ladbrokes_hockey_ne_lit_que_478_et_19388():
    p = _ladbrokes([
        (478, "Winner", "VAINQUEUR", ["1", "2"]),
        (999, "1X2", "1X2 TEMPS RÉGLEMENTAIRE", ["1", "X", "2"]),
        (19388, "Totals", "PLUS/MOINS 5.5", ["PLUS DE", "MOINS DE"]),
        (888, "Totals", "PLUS/MOINS 5.5 TR", ["PLUS DE", "MOINS DE"]),
    ])
    q = list(ladbrokes.parse_prematch(p, sport="hockey"))
    assert [(x.market, x.outcome.label, x.outcome.line) for x in q] == [
        (MarketType.H2H, "home", None), (MarketType.H2H, "away", None),
        (MarketType.TOTALS, "over", 5.5), (MarketType.TOTALS, "under", 5.5)]


def test_ladbrokes_le_football_garde_son_repli():
    p = _ladbrokes([(777, "1X2", "RÉSULTAT", ["1", "X", "2"])])
    assert len(list(ladbrokes.parse_prematch(p))) == 3


# ── L'alerte « CLV confirmé » ne croise plus deux sports ──────────────

def test_sports_of(tmp_path):
    from src.storage import Storage
    st = Storage(str(tmp_path / "t.db"))
    st.upsert_event("202610101900::cska|spartak", "hockey", "KHL", "CSKA", "Spartak", DEBUT)
    assert st.sports_of(["202610101900::cska|spartak", "absent"]) == {
        "202610101900::cska|spartak": "hockey"}


def test_la_clv_confirmee_ne_lit_que_les_paris_de_son_sport():
    import inspect

    from src import main
    src = inspect.getsource(main)
    assert "_sport_de = storage.sports_of(b[\"event_key\"] for b in _ouverts)" in src
    assert "if _s is not None and _s != current_sport:" in src


# ── Pinnacle : période 0 (prolongation incluse) et période 6 (réglementaire) ──

def _pinnacle(sport, marches):
    from unittest.mock import patch

    from src.scrapers import pinnacle as pin
    plus_tard = DEBUT.isoformat().replace("+00:00", "Z")
    matchups = [{"id": 7, "startTime": plus_tard, "participants": [
        {"name": "Rangers", "alignment": "home"}, {"name": "Bruins", "alignment": "away"}]}]

    def faux_get(self, path, params=None):
        return matchups if path.endswith("/matchups") else marches

    pin._MATCHUPS_CACHE.clear()
    with patch.object(pin.PinnacleScraper, "_get", faux_get):
        sc = pin.PinnacleScraper(request_delay=0)
        q = list(sc.fetch_market_quotes(sport))
        sc.close()
    pin._MATCHUPS_CACHE.clear()
    return q


_MARCHES_PIN = [
    {"status": "open", "matchupId": 7, "type": "moneyline", "period": 0, "prices": [
        {"designation": "home", "price": -150}, {"designation": "away", "price": 130}]},
    {"status": "open", "matchupId": 7, "type": "total", "period": 0, "prices": [
        {"designation": "over", "price": -110, "points": 5.5},
        {"designation": "under", "price": -110, "points": 5.5}]},
    {"status": "open", "matchupId": 7, "type": "moneyline", "period": 6, "prices": [
        {"designation": "home", "price": 110}, {"designation": "draw", "price": 330},
        {"designation": "away", "price": 210}]},
    {"status": "open", "matchupId": 7, "type": "total", "period": 6, "prices": [
        {"designation": "over", "price": 120, "points": 5.5},
        {"designation": "under", "price": -140, "points": 5.5}]},
    {"status": "open", "matchupId": 7, "type": "spread", "period": 6, "prices": [
        {"designation": "home", "price": 110, "points": -1.5}]},
]


def test_pinnacle_hockey_lit_la_periode_6_sous_ses_propres_types():
    q = _pinnacle("hockey", _MARCHES_PIN)
    par = {}
    for x in q:
        par.setdefault(x.market, set()).add(x.outcome.label)
    assert par == {MarketType.H2H: {"home", "away"}, MarketType.TOTALS: {"over", "under"},
                   MarketType.H2H_REG: {"home", "draw", "away"},
                   MarketType.TOTALS_REG: {"over", "under"}}
    # Chaque période garde SES prix : le vainqueur ne reçoit pas le 1X2.
    h2h_dom = next(x for x in q if x.market == MarketType.H2H and x.outcome.label == "home")
    reg_dom = next(x for x in q if x.market == MarketType.H2H_REG and x.outcome.label == "home")
    assert h2h_dom.decimal_odd < 1.7 < reg_dom.decimal_odd


def test_pinnacle_la_periode_6_reste_fermee_aux_autres_sports():
    q = _pinnacle("soccer", _MARCHES_PIN)
    assert {x.market for x in q} == {MarketType.H2H, MarketType.TOTALS}


# ── Les scrapers temps réglementaire ──────────────────────────────────

def test_unibet_hockey_lit_le_critere_pas_le_type():
    from src.scrapers import unibet
    debut = DEBUT.isoformat().replace("+00:00", "Z")

    def offre(crit, typ, issues, ligne=None):
        return {"criterion": {"id": crit}, "betOfferType": {"id": typ},
                "outcomes": [{"type": t, "odds": 2100, **({"line": ligne} if ligne else {})}
                             for t in issues]}
    data = {"events": [{"event": {"id": 1, "homeName": "Rangers", "awayName": "Bruins",
                                  "start": debut, "group": "NHL"},
                        "betOffers": [
        offre(1001105802, 2, ["OT_ONE", "OT_CROSS", "OT_TWO"]),
        offre(1001105863, 6, ["OT_OVER", "OT_UNDER"], 5500),
        offre(1001105889, 1, ["OT_ONE", "OT_TWO"]),            # puck line : dehors
        offre(999, 2, ["OT_ONE", "OT_TWO"]),                    # type 2 inconnu : dehors
    ]}]}
    q = list(unibet.parse_listview(data, sport="hockey"))
    assert sorted((x.market.value, x.outcome.label) for x in q) == sorted([
        ("h2h_reg", "home"), ("h2h_reg", "draw"), ("h2h_reg", "away"),
        ("totals_reg", "over"), ("totals_reg", "under")])
    assert {x.outcome.line for x in q if x.market == MarketType.TOTALS_REG} == {5.5}
    # Le football ne change pas : le type 2 reste `h2h`.
    assert {x.market for x in unibet.parse_listview(data)} == {MarketType.H2H, MarketType.TOTALS}


def test_betfirst_hockey_mw2w_vainqueur_mw3w_reglementaire():
    from src.scrapers import betfirst
    assert betfirst._market_type({"marketTemplateId": "MW2W"}, "hockey") == MarketType.H2H
    assert betfirst._market_type({"marketTemplateId": "MW3W"}, "hockey") == MarketType.H2H_REG
    for modele in ("GOU", "TGOUOT", "MTG2W", "MHCPNOT"):
        assert betfirst._market_type({"marketTemplateId": modele}, "hockey") is None
    assert betfirst._market_type({"marketTemplateId": "MW3W"}) == MarketType.H2H


def test_napoleon_hockey_marche_640_en_reglementaire():
    from src.scrapers import napoleon
    jour = DEBUT.strftime("%Y-%m-%d %H:%M:%S")
    p = {"data": [{"matchName": "Rangers·Bruins", "matchDate": jour, "eventId": 1,
                   "odds": [{"marketId": 640, "code": c, "price": 2.5, "status": "active"}
                            for c in ("1", "0", "2")]
                   + [{"marketId": 547, "code": "1", "price": 9.9, "status": "active"}]}]}
    q = list(napoleon.parse_by_date(p, "hockey"))
    assert {(x.market, x.outcome.label) for x in q} == {
        (MarketType.H2H_REG, "home"), (MarketType.H2H_REG, "draw"), (MarketType.H2H_REG, "away")}
    assert all(x.decimal_odd == 2.5 for x in q)


def test_vivatbet_hockey_groupe_1_en_reglementaire_total_dehors():
    """Rejoué sur une vraie réponse `games1x2` (football) : sous le drapeau
    hockey, le groupe 1 sort en `h2h_reg` et le total (groupe 17) disparaît."""
    import json
    from pathlib import Path

    from src.scrapers import vivatbet
    assert vivatbet.SPORT_IDS["hockey"] == 2
    brut = json.loads((Path(__file__).parent / "fixtures"
                       / "vivatbet_games1x2_soccer_sample.json").read_text(encoding="utf-8"))
    foot = list(vivatbet.parse_games(brut))
    hockey_ = list(vivatbet.parse_games(brut, sport="hockey"))
    assert {x.market for x in foot} == {MarketType.H2H, MarketType.TOTALS}
    assert {x.market for x in hockey_} == {MarketType.H2H_REG}
    assert sorted((x.event_key, x.outcome.label, x.decimal_odd) for x in hockey_) == sorted(
        (x.event_key, x.outcome.label, x.decimal_odd) for x in foot if x.market == MarketType.H2H)


# ── De bout en bout : chaque famille face à SA référence ──────────────

def test_un_1x2_reglementaire_n_est_compare_qu_a_la_periode_6():
    """Un 1X2 réglementaire à 2,60 sur l'extérieur : face à la période 6
    (extérieur juste ≈ 3,1), aucune valeur. Face au vainqueur prolongation
    incluse (extérieur ≈ 2,3), il afficherait +13 % — ce qui n'arrive plus."""
    from src.config import ScanConfig
    from src.detection import build_fair_lines, find_value_bets
    pin = [q for q in _pinnacle("hockey", _MARCHES_PIN)]
    ek = pin[0].event_key
    fair = build_fair_lines(pin, ScanConfig().devig_method)
    assert (ek, MarketType.H2H_REG, None) in fair and (ek, MarketType.H2H, None) in fair

    def soft(label, cote, marche=MarketType.H2H_REG):
        return OddQuote(event_key=ek, book=Book.UNIBET_BE, market=marche,
                        outcome=Outcome(label=label, line=None), decimal_odd=cote,
                        fetched_at=DEBUT, source_event_id="1")
    reg = [soft("home", 2.0), soft("draw", 4.0), soft("away", 2.6)]
    cfg = ScanConfig(sport="hockey", min_ev_pct=1.0)
    assert find_value_bets(reg, fair, cfg) == []
    # Le même 1X2 réglementaire porté par erreur en `h2h` serait rejeté par le
    # nombre d'issues (3 contre 2) — et sans son nul, il passerait : c'est
    # pourquoi le type est fixé dans le scraper, pas déduit ici.
    sans_nul = [soft("home", 2.0, MarketType.H2H), soft("away", 2.6, MarketType.H2H)]
    assert find_value_bets(sans_nul, fair, cfg), "le piège existe bien sans types séparés"


def test_un_marche_reglementaire_n_est_JAMAIS_regle():
    """Un score final peut contenir la prolongation : régler dessus écrirait
    un résultat faux. Le pari reste sans résultat, sa CLV reste mesurée."""
    from src.clv import settle
    assert settle("h2h_reg", "home", None, "home", 3, 2) is None
    assert settle("totals_reg", "over", 5.5, "home", 4, 3) is None


def test_le_swap_garde_les_totaux_reglementaires_symetriques():
    from src.reference import _flip_outcome_for_swap
    o = Outcome(label="over", line=5.5)
    assert _flip_outcome_for_swap(o, MarketType.TOTALS_REG) == o
    assert _flip_outcome_for_swap(Outcome("home", None), MarketType.H2H_REG).label == "away"


# ── Le rythme : jamais plus d'un passage par intervalle ───────────────

def test_le_hockey_ne_passe_qu_une_fois_par_intervalle(monkeypatch):
    monkeypatch.delenv("HOCKEY_INTERVAL_SEC", raising=False)
    hockey._DERNIER_PASSAGE.clear()
    assert hockey.doit_passer(1000.0)
    assert not hockey.doit_passer(1060.0)
    assert hockey.doit_passer(1121.0)
    monkeypatch.setenv("HOCKEY_INTERVAL_SEC", "0")
    assert hockey.doit_passer(1121.5)
    hockey._DERNIER_PASSAGE.clear()


def test_le_frein_est_branche_et_le_secours_smarkets_coupe():
    import inspect

    from src import main
    corps = inspect.getsource(main._daemon_scan_sport)
    assert "if current_sport == hockey.SPORT and not hockey.doit_passer():" in corps
    assert "secondary = ([] if current_sport == hockey.SPORT" in corps


def test_le_hockey_tourne_en_fond_sans_bloquer_ni_se_dedoubler():
    import threading
    import time as _t
    hockey._FOND.clear()
    debut, fin, erreurs = threading.Event(), threading.Event(), []

    def long():
        debut.set()
        fin.wait(2)

    t0 = _t.monotonic()
    assert hockey.lancer_en_fond(long)
    assert _t.monotonic() - t0 < 0.5, "le cycle n'attend pas le hockey"
    debut.wait(1)
    assert not hockey.lancer_en_fond(long), "un seul passage à la fois"
    fin.set()
    hockey._FOND[0].join(1)

    def casse():
        raise RuntimeError("boum")
    assert hockey.lancer_en_fond(casse, on_error=erreurs.append)
    hockey._FOND[0].join(1)
    assert [str(e) for e in erreurs] == ["boum"]
    hockey._FOND.clear()


def test_le_cycle_ne_met_pas_le_hockey_au_premier_plan():
    import inspect

    from src import main
    src = inspect.getsource(main)
    assert "premier_plan = [sp for sp in sports_list if sp != hockey.SPORT]" in src
    assert "for sp in premier_plan" in src and "hockey.lancer_en_fond(" in src
