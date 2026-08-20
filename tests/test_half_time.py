"""Les marchés de mi-temps — §21.14.

Le risque de ce chantier tient en une phrase : un `total 1.5` de mi-temps
apparié à un `total 1.5` de match plein ne lèverait AUCUNE erreur. Le groupe
aurait ses deux côtés, la marge serait plausible, et seule la ligne juste
serait fausse. C'est la panne silencieuse du §21.3, celle qui fabrique des
paris de valeur inventés au lieu de zéro pari.

Ces tests portent donc d'abord sur la séparation, ensuite sur le reste.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.main import build_fair_lines, find_value_bets, _flip_outcome_for_swap
from src.config import ScanConfig
from src.models import (Book, HALF_TIME_MARKETS, MarketType, OddQuote, Outcome,
                        TOTALS_LIKE, ValueBet, base_market, is_half_time)


def _q(book, market, label, line, odd, ek="2030-01-01T20:00|a|b"):
    return OddQuote(event_key=ek, book=book, market=market,
                    outcome=Outcome(label=label, line=line), decimal_odd=odd,
                    fetched_at=datetime.now(timezone.utc), source_event_id="1")


# --------------------------------------------------------------------------
# La séparation — le point non négociable
# --------------------------------------------------------------------------

def test_mi_temps_et_match_plein_ne_partagent_jamais_un_groupe_de_devig():
    """Même événement, même ligne 1.5, deux périodes : deux lignes justes.

    Si ces deux marchés fusionnaient, la ligne juste de la mi-temps serait
    calculée sur les probabilités du match plein — un but avant la pause n'a
    rien à voir avec un but sur 90 minutes.
    """
    quotes = [
        _q(Book.PINNACLE, MarketType.TOTALS, "over", 1.5, 1.30),
        _q(Book.PINNACLE, MarketType.TOTALS, "under", 1.5, 3.60),
        _q(Book.PINNACLE, MarketType.TOTALS_H1, "over", 1.5, 3.70),
        _q(Book.PINNACLE, MarketType.TOTALS_H1, "under", 1.5, 1.28),
    ]
    fair = build_fair_lines(quotes, "shin")
    ek = "2030-01-01T20:00|a|b"
    plein = fair[(ek, MarketType.TOTALS, 1.5)]
    mi_temps = fair[(ek, MarketType.TOTALS_H1, 1.5)]

    assert plein.outcomes["over"] > 0.65, "le match plein doit rester le match plein"
    assert mi_temps.outcomes["over"] < 0.35, "la mi-temps doit rester la mi-temps"
    # Et surtout : les deux ne se ressemblent pas.
    assert abs(plein.outcomes["over"] - mi_temps.outcomes["over"]) > 0.3


def test_un_soft_de_mi_temps_ne_se_valorise_pas_sur_la_ligne_du_match_plein():
    """Le scénario exact du §21.3, transposé.

    Un soft propose `over 1.5` de MI-TEMPS à 2.10. Pinnacle ne price que le
    match plein sur cette ligne. Sans séparation, le devig du match plein
    (over à ~0,77) donnerait une EV délirante de +61 % — un pari fantôme.
    Avec des types distincts, il n'y a pas de référence, donc pas de pari.
    """
    pinnacle = [
        _q(Book.PINNACLE, MarketType.TOTALS, "over", 1.5, 1.30),
        _q(Book.PINNACLE, MarketType.TOTALS, "under", 1.5, 3.60),
    ]
    soft = [_q(Book.UNIBET_BE, MarketType.TOTALS_H1, "over", 1.5, 2.10),
            _q(Book.UNIBET_BE, MarketType.TOTALS_H1, "under", 1.5, 1.65)]
    bets = find_value_bets(soft, build_fair_lines(pinnacle, "shin"),
                           ScanConfig(min_ev_pct=2.0))
    assert bets == [], "une mi-temps sans référence de mi-temps ne doit rien produire"


def test_la_mi_temps_produit_un_pari_quand_pinnacle_la_price_vraiment():
    """La contrepartie : la séparation ne doit pas tout stériliser."""
    pinnacle = [
        _q(Book.PINNACLE, MarketType.TOTALS_H1, "over", 1.5, 3.70),
        _q(Book.PINNACLE, MarketType.TOTALS_H1, "under", 1.5, 1.28),
    ]
    # Les deux côtés : find_value_bets exige du soft autant d'issues que la
    # ligne juste en compte, pour écarter les marchés de structure différente.
    soft = [_q(Book.UNIBET_BE, MarketType.TOTALS_H1, "over", 1.5, 4.60),
            _q(Book.UNIBET_BE, MarketType.TOTALS_H1, "under", 1.5, 1.20)]
    bets = find_value_bets(soft, build_fair_lines(pinnacle, "shin"),
                           ScanConfig(min_ev_pct=2.0))
    assert len(bets) == 1
    assert bets[0].market == MarketType.TOTALS_H1
    assert bets[0].ev_pct > 2.0


# --------------------------------------------------------------------------
# Le silence exigé
# --------------------------------------------------------------------------

def test_aucune_alerte_ne_sort_d_un_marche_de_mi_temps(monkeypatch):
    """Ni canal principal, ni premium, ni critique.

    La détection doit avoir lieu et rester en base — seul l'envoi est coupé.
    C'est le motif du §21.8 : faire taire sans cesser de mesurer.
    """
    from src.alerter import TelegramAlerter, TelegramConfig

    envois: list[str] = []
    cfg = TelegramConfig(bot_token="t", chat_id="1", premium_chat_id="2",
                         critical_chat_id="3", min_ev_pct=1.0,
                         min_premium_ev_pct=1.0, min_critical_ev_pct=1.0)
    al = TelegramAlerter(cfg)
    monkeypatch.setattr(al, "_send", lambda *a, **k: envois.append(a[0]) or True)

    demain = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    for marche in (MarketType.TOTALS_H1, MarketType.H2H_H1):
        bet = ValueBet(event_key=f"{demain}|a|b", book=Book.UNIBET_BE, market=marche,
                       outcome=Outcome(label="over", line=1.5), odd_taken=2.5,
                       fair_prob=0.5, fair_odd=2.0, ev_pct=25.0, kelly_stake_pct=5.0,
                       detected_at=datetime.now(timezone.utc))
        assert al.send_value_bet(bet) is False
    assert envois == [], "aucun canal ne doit recevoir de mi-temps"


def test_le_match_plein_alerte_toujours(monkeypatch):
    """Contrepartie : le blocage ne doit pas déborder sur le match plein."""
    from src.alerter import TelegramAlerter, TelegramConfig

    envois: list[str] = []
    cfg = TelegramConfig(bot_token="t", chat_id="1", min_ev_pct=1.0,
                         main_max_ev_pct=100.0, main_min_odd=1.0, main_max_odd=10.0)
    al = TelegramAlerter(cfg)
    monkeypatch.setattr(al, "_send", lambda *a, **k: envois.append(a[0]) or True)

    demain = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M")
    bet = ValueBet(event_key=f"{demain}|a|b", book=Book.UNIBET_BE, market=MarketType.TOTALS,
                   outcome=Outcome(label="over", line=2.5), odd_taken=2.5,
                   fair_prob=0.5, fair_odd=2.0, ev_pct=25.0, kelly_stake_pct=5.0,
                       detected_at=datetime.now(timezone.utc))
    assert al.send_value_bet(bet) is True
    assert len(envois) == 1


# --------------------------------------------------------------------------
# Les pièges de dispatch : tout code qui commutait sur TOTALS
# --------------------------------------------------------------------------

def test_flip_outcome_laisse_passer_les_totaux_de_mi_temps():
    """`over`/`under` sont symétriques en équipes, à la mi-temps comme ailleurs.

    Avant TOTALS_LIKE, un `totals_h1` tombait dans la branche home↔away.
    """
    o = Outcome(label="over", line=1.5)
    assert _flip_outcome_for_swap(o, MarketType.TOTALS_H1) == o
    assert _flip_outcome_for_swap(o, MarketType.TOTALS) == o


def test_flip_outcome_retourne_bien_un_h2h_de_mi_temps():
    """En revanche `home`/`away` doit toujours être retourné, mi-temps comprise."""
    assert _flip_outcome_for_swap(Outcome(label="home"), MarketType.H2H_H1).label == "away"


@pytest.mark.parametrize("marche", sorted(HALF_TIME_MARKETS, key=lambda m: m.value))
def test_chaque_marche_de_mi_temps_a_une_base_et_se_declare(marche):
    assert is_half_time(marche)
    assert not is_half_time(base_market(marche))
    assert base_market(marche) in (MarketType.H2H, MarketType.TOTALS)


def test_les_marches_de_match_plein_ne_sont_pas_des_mi_temps():
    for m in (MarketType.H2H, MarketType.TOTALS, MarketType.HANDICAP, MarketType.BTTS):
        assert not is_half_time(m)
        assert base_market(m) is m
    assert MarketType.TOTALS_H1 in TOTALS_LIKE
    assert MarketType.HANDICAP not in TOTALS_LIKE


# --------------------------------------------------------------------------
# La CLV — l'exigence explicite : muet, mais mesuré
# --------------------------------------------------------------------------

def test_la_cloture_d_une_mi_temps_ne_lit_pas_les_cotes_du_match_plein(tmp_path):
    """Le pendant du test de devig, côté clôture — et donc côté CLV.

    Sur le même événement et la même ligne 1.5, deux marchés coexistent en
    base. Si `closing_group` mélangeait les deux, la CLV d'une mi-temps serait
    calculée contre la clôture du match plein : un chiffre plausible, jamais
    signalé, et faux. C'est ce qui rendrait la mesure demandée inutilisable.
    """
    from src.storage import Storage

    st = Storage(str(tmp_path / "t.db"))
    ek = "2030-01-01T20:00|a|b"
    avant = datetime(2030, 1, 1, 19, 0, tzinfo=timezone.utc)

    for marche, over, under in ((MarketType.TOTALS, 1.30, 3.60),
                                (MarketType.TOTALS_H1, 3.70, 1.28)):
        for label, cote in (("over", over), ("under", under)):
            q = _q(Book.PINNACLE, marche, label, 1.5, cote, ek=ek)
            st.insert_quote(replace(q, fetched_at=avant))

    kickoff = datetime(2030, 1, 1, 20, 0, tzinfo=timezone.utc)
    groupe = st.closing_group(event_key=ek, market=MarketType.TOTALS_H1.value,
                              line=1.5, before=kickoff)

    assert len(groupe) == 2, "les deux côtés de la mi-temps, et rien de plus"
    cotes = sorted(float(r["decimal_odd"]) for r in groupe)
    assert cotes == pytest.approx([1.28, 3.70]), \
        "ce sont les cotes de la MI-TEMPS, pas celles du match plein"
    assert all(r["market"] == MarketType.TOTALS_H1.value for r in groupe)


def test_une_mi_temps_reste_ecrite_en_base_malgre_le_silence(tmp_path):
    """Muet ne veut pas dire absent : sans écriture, aucune CLV à mesurer."""
    from src.storage import Storage

    st = Storage(str(tmp_path / "t.db"))
    bet = ValueBet(event_key="2030-01-01T20:00|a|b", book=Book.UNIBET_BE,
                   market=MarketType.TOTALS_H1, outcome=Outcome(label="over", line=1.5),
                   odd_taken=4.6, fair_prob=0.25, fair_odd=4.0, ev_pct=15.0,
                   kelly_stake_pct=2.0, detected_at=datetime.now(timezone.utc))
    vb_id = st.insert_value_bet(bet)
    assert vb_id > 0

    ouverts = st.open_value_bets()
    assert any(int(r["id"]) == vb_id for r in ouverts), \
        "le pari doit entrer dans la file de clôture, sinon pas de CLV"
    assert next(r for r in ouverts if int(r["id"]) == vb_id)["market"] == "totals_h1"


# --------------------------------------------------------------------------
# Circus — les quatre codes relevés sur le dump réel le 20/08
# --------------------------------------------------------------------------

def _circus_dump(marches):
    return {"Leagues": [{"LeagueName": "Jupiler", "SportId": 1, "Events": [
        {"HomeName": "Anderlecht", "AwayName": "Genk", "EventId": "1",
         "StartDate": "2030-01-01T20:00:00", "Markets": marches}]}]}


def test_circus_lit_les_deux_orthographes_du_total_de_mi_temps():
    """Circus écrit ce marché de DEUX façons, coexistant dans le même dump.

    N'en reconnaître qu'une avait déjà coûté 64 % des totaux de tennis
    (voir circus.py). Ici, la moitié négligée pèse 448 marchés contre 99.
    """
    from src.scrapers.circus import parse_prematch

    quotes = list(parse_prematch(_circus_dump([
        {"BetType": "half-time-totals-over-under-OverUnder", "Base": 1.5,
         "Outcomes": [{"Name": "Plus de", "Odd": 2.10, "Base": 1.5},
                      {"Name": "Moins de", "Odd": 1.70, "Base": 1.5}]},
        {"BetType": "1st-half-total-OverUnder", "Base": 0.5,
         "Outcomes": [{"Name": "Plus de", "Odd": 1.55, "Base": 0.5},
                      {"Name": "Moins de", "Odd": 2.35, "Base": 0.5}]},
    ])))
    assert len(quotes) == 4, "les DEUX orthographes doivent passer"
    assert {q.market for q in quotes} == {MarketType.TOTALS_H1}
    assert {q.outcome.line for q in quotes} == {1.5, 0.5}
    assert {q.outcome.label for q in quotes} == {"over", "under"}


def test_circus_lit_les_deux_orthographes_du_vainqueur_de_mi_temps():
    """Le piège du dispatch : avant `base_market`, un `h2h_h1` tombait dans la
    branche des totaux et `_totals_label` renvoyait None sur « 1 »/« X »/« 2 ».
    Les 506 marchés de vainqueur de mi-temps disparaissaient en silence.
    """
    from src.scrapers.circus import parse_prematch

    for code in ("1HalfP1XP2", "1st-half-1x2"):
        quotes = list(parse_prematch(_circus_dump([
            {"BetType": code, "Outcomes": [
                {"Name": "Anderlecht", "Odd": 2.40},
                {"Name": "Nul", "Odd": 2.05},
                {"Name": "Genk", "Odd": 3.60}]}])))
        assert len(quotes) == 3, f"{code} doit rendre ses trois issues"
        assert {q.market for q in quotes} == {MarketType.H2H_H1}
        assert all(q.outcome.line is None for q in quotes), \
            "un h2h ne porte pas de ligne"


def test_circus_ne_confond_pas_mi_temps_et_match_plein():
    """Les deux dans le même dump, sur la même ligne 1.5 : ils doivent sortir
    sous des marchés différents, sans quoi le devig les réunirait."""
    from src.scrapers.circus import parse_prematch

    quotes = list(parse_prematch(_circus_dump([
        {"BetType": "total-goals-OverUnder", "Base": 1.5,
         "Outcomes": [{"Name": "Plus de", "Odd": 1.30, "Base": 1.5},
                      {"Name": "Moins de", "Odd": 3.40, "Base": 1.5}]},
        {"BetType": "half-time-totals-over-under-OverUnder", "Base": 1.5,
         "Outcomes": [{"Name": "Plus de", "Odd": 3.60, "Base": 1.5},
                      {"Name": "Moins de", "Odd": 1.28, "Base": 1.5}]},
    ])))
    par_marche = {q.market: q for q in quotes if q.outcome.label == "over"}
    assert set(par_marche) == {MarketType.TOTALS, MarketType.TOTALS_H1}
    # Même ligne, mais des cotes qui n'ont rien à voir : c'est bien deux
    # marchés, et les mélanger produirait une ligne juste absurde.
    assert par_marche[MarketType.TOTALS].outcome.line == 1.5
    assert par_marche[MarketType.TOTALS_H1].outcome.line == 1.5
    assert par_marche[MarketType.TOTALS].decimal_odd == 1.30
    assert par_marche[MarketType.TOTALS_H1].decimal_odd == 3.60
