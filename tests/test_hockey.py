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
    gardees, ecartees = hockey.filtrer([
        _q(Book.PINNACLE, MarketType.H2H),
        _q(Book.PINNACLE, MarketType.TOTALS, "over", 5.5),
        _q(Book.LADBROKES_BE, MarketType.H2H),
        _q(Book.LADBROKES_BE, MarketType.TOTALS, "over", 5.5),
        _q(Book.GOLDEN_PALACE, MarketType.TOTALS, "over", 5.5),
        _q(Book.STARCASINO_SPORT, MarketType.H2H),
        _q(Book.UNIBET_BE, MarketType.H2H),
        _q(Book.NAPOLEON_BE, MarketType.H2H),
        _q(Book.VIVATBET, MarketType.H2H),
    ])
    assert [(q.book, q.market) for q in gardees] == [
        (Book.PINNACLE, MarketType.H2H), (Book.PINNACLE, MarketType.TOTALS),
        (Book.LADBROKES_BE, MarketType.H2H), (Book.GOLDEN_PALACE, MarketType.TOTALS),
        (Book.STARCASINO_SPORT, MarketType.H2H)]
    assert ecartees == {"ladbrokes_be": 1, "unibet_be": 1, "napoleon_be": 1, "vivatbet": 1}


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


def test_altenar_hockey_ne_lit_que_406_et_412():
    q = list(goldenpalace.parse_get_events(_altenar([1, 406, 18, 412]), sport="hockey"))
    assert {(x.market, x.outcome.label) for x in q} == {
        (MarketType.H2H, "home"), (MarketType.H2H, "away"),
        (MarketType.TOTALS, "over"), (MarketType.TOTALS, "under")}
    assert len(q) == 4, "le 1X2 (typeId 1) et le total 18 sont écartés"
    assert not any(x.outcome.label == "draw" for x in q)


def test_altenar_le_nul_suspendu_ne_fait_pas_passer_le_reglementaire():
    """Le cas qui fabriquait l'EV fictive : le 1X2 sans son nul."""
    p = _altenar([1])
    p["odds"][1]["oddStatus"] = 1          # le nul suspendu
    assert list(goldenpalace.parse_get_events(p, sport="hockey")) == []


def test_altenar_le_football_ne_change_pas():
    q = list(goldenpalace.parse_get_events(_altenar([1, 18])))
    assert len(q) == 5 and {x.outcome.label for x in q} == {"home", "draw", "away", "over", "under"}


def test_starcasino_suit_la_meme_regle():
    q = list(starcasinosport.parse_get_events(_altenar([1, 406]), sport="hockey"))
    assert len(q) == 2 and all(x.book == Book.STARCASINO_SPORT for x in q)


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


def test_ladbrokes_hockey_ne_lit_que_le_vainqueur_478():
    p = _ladbrokes([
        (478, "Winner", "VAINQUEUR", ["1", "2"]),
        (999, "1X2", "1X2 TEMPS RÉGLEMENTAIRE", ["1", "X", "2"]),
        (19388, "Totals", "PLUS/MOINS 5.5", ["PLUS DE", "MOINS DE"]),
        (888, "Totals", "PLUS/MOINS 5.5 TR", ["PLUS DE", "MOINS DE"]),
    ])
    q = list(ladbrokes.parse_prematch(p, sport="hockey"))
    assert [(x.market, x.outcome.label) for x in q] == [
        (MarketType.H2H, "home"), (MarketType.H2H, "away")]


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
