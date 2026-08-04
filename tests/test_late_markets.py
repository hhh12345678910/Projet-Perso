"""Marchés prématch restés ouverts sur un match commencé.

Le cas vu en production : un match de football commencé depuis 19 minutes,
score 1-1, et Circus proposait toujours son marché « les deux équipes
marquent » en prématch. Le pari était déjà gagné au moment de le prendre.

Ce n'est pas un value bet, c'est une erreur d'exploitation du book. Tout
l'enjeu du détecteur est de ne pas confondre cette erreur avec les trois
situations qui lui ressemblent : une cote live légitime, un match reporté, et
un horaire simplement différent d'un book à l'autre.
"""
from __future__ import annotations

import importlib
from datetime import datetime, timedelta, timezone

import pytest

from src.main import find_late_markets, remember_pinnacle_events
from src.matcher import event_key
from src.models import Book, MarketType, OddQuote, Outcome


NOW = datetime(2026, 9, 1, 21, 0, tzinfo=timezone.utc)
KICKOFF = NOW - timedelta(minutes=19)      # commencé il y a 19 minutes
EK = event_key("Union Saint-Gilloise", "Anderlecht", KICKOFF)


def _pin(ek=EK):
    return OddQuote(event_key=ek, book=Book.PINNACLE, market=MarketType.H2H,
                    outcome=Outcome(label="home"), decimal_odd=2.0,
                    fetched_at=NOW, source_event_id="1")


def _soft(book=Book.CIRCUS_BE, ek=EK, live=False, market=MarketType.H2H,
          label="home", line=None):
    return OddQuote(event_key=ek, book=book, market=market,
                    outcome=Outcome(label=label, line=line), decimal_odd=2.4,
                    fetched_at=NOW, source_event_id="2", from_live_feed=live)


def _recent(ek=EK):
    return {ek: 0.0}


def test_the_production_case_is_detected():
    """Pinnacle connaissait le match, ne le price plus (il est en direct), le
    coup d'envoi est dépassé de 19 min, et Circus propose encore du prématch."""
    late = find_late_markets([], [_soft()], "soccer", NOW, recent=_recent())
    assert (EK, Book.CIRCUS_BE) in late
    assert len(late[(EK, Book.CIRCUS_BE)]) == 1


def test_a_match_pinnacle_still_prices_is_not_late():
    """Si Pinnacle price encore l'événement en prématch, c'est qu'il n'a pas
    commencé — quelle que soit l'heure affichée. C'est la disparition qui fait
    foi, pas l'horloge."""
    late = find_late_markets([_pin()], [_soft()], "soccer", NOW, recent=_recent())
    assert late == {}


def test_a_live_quote_is_never_flagged():
    """Betano expose un flux live, fusionné avec son prématch dans la même
    liste. Sans ce filtre, tout match en cours qu'il price passerait pour une
    erreur du book."""
    late = find_late_markets([], [_soft(book=Book.BETANO_BE, live=True)],
                             "soccer", NOW, recent=_recent())
    assert late == {}


def test_an_event_pinnacle_never_priced_is_ignored():
    """Un match que Pinnacle n'a jamais pricé peut simplement avoir un horaire
    faux chez le book. Sans confirmation par la référence, on se tait."""
    late = find_late_markets([], [_soft()], "soccer", NOW, recent={})
    assert late == {}


def test_a_match_too_recent_is_ignored():
    """Sous le seuil, on signalerait surtout des coups d'envoi retardés de
    quelques minutes et des arrondis de programmation."""
    ko = NOW - timedelta(minutes=3)
    ek = event_key("Union Saint-Gilloise", "Anderlecht", ko)
    late = find_late_markets([], [_soft(ek=ek)], "soccer", NOW, recent=_recent(ek))
    assert late == {}


def test_a_match_long_finished_is_ignored():
    """Deux heures après le coup d'envoi, un marché encore ouvert ne relève
    plus de l'oubli mais d'un horaire faux — et le pari ne serait pas payé."""
    ko = NOW - timedelta(minutes=140)
    ek = event_key("Union Saint-Gilloise", "Anderlecht", ko)
    late = find_late_markets([], [_soft(ek=ek)], "soccer", NOW, recent=_recent(ek))
    assert late == {}


def test_the_book_own_kickoff_can_veto():
    """Au tennis le rapprochement tolère trois heures d'écart. Si le book
    annonce un coup d'envoi encore à venir, c'est peut-être lui qui a raison :
    on ne l'accuse pas sur la seule foi de l'heure de Pinnacle."""
    pin_ko = NOW - timedelta(minutes=40)
    book_ko = NOW + timedelta(minutes=30)          # le book dit : pas commencé
    ref = event_key("Alcaraz", "Sinner", pin_ko)
    cand = event_key("Alcaraz", "Sinner", book_ko)
    late = find_late_markets([], [_soft(ek=cand)], "tennis", NOW,
                             recent=_recent(ref))
    assert late == {}


def test_several_markets_of_one_book_are_grouped():
    quotes = [
        _soft(market=MarketType.H2H, label="home"),
        _soft(market=MarketType.H2H, label="away"),
        _soft(market=MarketType.TOTALS, label="over", line=2.5),
    ]
    late = find_late_markets([], quotes, "soccer", NOW, recent=_recent())
    assert len(late[(EK, Book.CIRCUS_BE)]) == 3


def test_two_books_are_reported_separately():
    late = find_late_markets(
        [], [_soft(book=Book.CIRCUS_BE), _soft(book=Book.UNIBET_BE)],
        "soccer", NOW, recent=_recent())
    assert {b for _, b in late} == {Book.CIRCUS_BE, Book.UNIBET_BE}


def test_memory_forgets_old_events():
    """Le dictionnaire ne doit pas grossir indéfiniment : au-delà de six
    heures un match est terminé et n'apprend plus rien."""
    import src.main as m
    m._PINNACLE_RECENT.clear()
    remember_pinnacle_events([_pin()], 0.0)
    assert EK in m._PINNACLE_RECENT
    remember_pinnacle_events([], 7 * 3600.0)
    assert EK not in m._PINNACLE_RECENT
    m._PINNACLE_RECENT.clear()


# ── Détection de but ──────────────────────────────────────────────────────
# Le flux live Betano est la SEULE source de score du projet : Pinnacle ignore
# les matchs en cours. Un but est exactement l'instant où un marché prématch
# oublié devient exploitable — « les deux équipes marquent » sur un 1-1 est
# déjà gagné.

def _live_payload(events):
    return {"events": {str(i): e for i, e in enumerate(events)}}


def _live_event(home, away, h, a, *, sport="FOOT", live=True, secs=1200,
                start_ms=None):
    ms = start_ms if start_ms is not None else int(KICKOFF.timestamp() * 1000)
    return {
        "isLive": live, "sportId": sport, "startTime": ms,
        "participants": [{"name": home, "isHome": True}, {"name": away}],
        "liveData": {"score": {"home": str(h), "away": str(a)},
                     "clock": {"secondsSinceStart": secs}},
    }


def test_live_scores_are_extracted_for_soccer():
    from src.scrapers.betano import parse_live_scores
    got = parse_live_scores(_live_payload([
        _live_event("Union Saint-Gilloise", "Anderlecht", 1, 1)]))
    assert got == {EK: (1, 1, 20)}


def test_tennis_scores_are_ignored():
    """Au tennis le champ `score` porte les points du jeu en cours ; il change
    à chaque échange et produirait un flot d'alertes sans rapport."""
    from src.scrapers.betano import parse_live_scores
    got = parse_live_scores(_live_payload([
        _live_event("Alcaraz", "Sinner", 40, 30, sport="TENNIS")]))
    assert got == {}


def test_a_match_not_live_is_ignored():
    from src.scrapers.betano import parse_live_scores
    got = parse_live_scores(_live_payload([
        _live_event("Union Saint-Gilloise", "Anderlecht", 0, 0, live=False)]))
    assert got == {}


def test_an_unreadable_score_is_skipped_not_guessed():
    """Mieux vaut ne rien signaler qu'annoncer un but qui n'a pas eu lieu."""
    from src.scrapers.betano import parse_live_scores
    bad = _live_event("Union Saint-Gilloise", "Anderlecht", 0, 0)
    bad["liveData"]["score"] = {"home": "-", "away": None}
    assert parse_live_scores(_live_payload([bad])) == {}


def test_a_score_change_is_a_goal():
    from src.main import goals_since_last_cycle
    assert goals_since_last_cycle({EK: (1, 1, 20)}, {EK: (1, 0, 18)}) == {EK}
    assert goals_since_last_cycle({EK: (1, 0, 20)}, {EK: (1, 0, 18)}) == set()


def test_a_first_sighting_is_never_a_goal():
    """Au démarrage du daemon, tous les matchs en cours seraient sinon
    annoncés comme venant de marquer, et le canal partirait en rafale."""
    from src.main import goals_since_last_cycle
    assert goals_since_last_cycle({EK: (2, 1, 30)}, {}) == set()


def test_finished_matches_are_forgotten():
    """Le daemon tourne des semaines : sans purge, le dictionnaire des scores
    garde tous les matchs vus depuis le démarrage."""
    from src.main import forget_finished_scores
    old = event_key("A", "B", NOW - timedelta(hours=7))
    scores = {EK: (1, 1, 20), old: (2, 0, 90)}
    forget_finished_scores(scores, NOW)
    assert set(scores) == {EK}


def test_the_real_send_path_accepts_what_the_cycle_builds():
    """Le test du délai remplace send_late_market_alerts par un mock, ce qui
    masquerait un désaccord d'arité entre les deux. Or `_report_late_markets`
    construit désormais des sextuplets, et l'exception serait avalée par le
    `except` du cycle : le détecteur se tairait sans une ligne au journal —
    exactement le mode d'échec silencieux qui coûte le plus cher ici. Ce test
    fait passer un vrai appel de bout en bout."""
    from src.alerter import TelegramConfig, send_late_market_alerts
    import src.alerter as a

    cfg = TelegramConfig(bot_token="x", chat_id="1", critical_chat_id="9")
    texts: list[str] = []
    a.TelegramAlerter._send = lambda self, text, **kw: (texts.append(text) or True)

    item = (EK, Book.CIRCUS_BE, [_soft()], 19.0, (1, 1, 20), True)
    try:
        sent = send_late_market_alerts([item], cfg)
    finally:
        importlib.reload(a)
    assert sent == [item]
    assert "1-1" in texts[0] and "BUT" in texts[0]


def test_a_message_without_score_says_so_rather_than_implying_0_0():
    """Betano ne couvre pas tous les championnats. Un message muet sur le score
    se lirait comme un 0-0, et « les deux équipes marquent » n'a pas du tout la
    même valeur à 0-0 qu'à 1-1."""
    from src.alerter import format_late_market
    msg = format_late_market(EK, Book.CIRCUS_BE, [_soft()], 19.0, sport="soccer")
    assert "inconnu" in msg


def test_a_goal_bypasses_the_reminder_delay():
    """C'est tout l'intérêt : attendre le rappel suivant ferait manquer la
    seule minute qui compte."""
    import src.main as m
    m._LATE_ALERTED.clear(); m._LIVE_SCORES.clear()
    late = {(EK, Book.CIRCUS_BE): [_soft()]}
    sent = []
    m.send_late_market_alerts = lambda items, cfg, **kw: (sent.extend(items) or items)
    try:
        m._report_late_markets(late, "soccer", object())
        assert len(sent) == 1, "première alerte"
        m._report_late_markets(late, "soccer", object())
        assert len(sent) == 1, "silence pendant le délai"
        m._report_late_markets(late, "soccer", object(), goals={EK})
        assert len(sent) == 2, "un but doit rouvrir la parole immédiatement"
    finally:
        m._LATE_ALERTED.clear()
        import importlib
        importlib.reload(m)
