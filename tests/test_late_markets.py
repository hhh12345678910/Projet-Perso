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


# Pinnacle répond bien à ce cycle, simplement sur d'AUTRES matchs. C'est la
# situation normale : notre match a disparu parce qu'il est passé en direct.
# Passer une réponse vide voudrait dire « Pinnacle est muet », ce qui ne prouve
# aucune disparition et doit rester sans effet.
def _pin_elsewhere():
    return [_pin(ek=event_key("Genk", "Gand", NOW + timedelta(hours=3)))]


# La cote de _soft() vaut 2.4. « Figée » = le book affiche encore exactement son
# prix d'avant le coup d'envoi, donc il a bien oublié de suspendre. « Bougée » =
# il price en direct, ce qui est le comportement normal de la plupart des books
# belges et n'a rien d'une erreur.
def _frozen(_ek, _book, _before):
    return {("h2h", "home", None): 2.4, ("h2h", "away", None): 2.4,
            ("totals", "over", 2.5): 2.4, ("btts", "over", None): 2.4}


def _moved(_ek, _book, _before):
    return {k: v + 0.9 for k, v in _frozen(None, None, None).items()}


def _no_history(_ek, _book, _before):
    return {}


# Le consensus live : d'autres books qui, eux, ont repricé. Sans eux on ne peut
# pas savoir si le prix figé est devenu absurde, et le détecteur se tait — c'est
# la règle ajoutée après le flood de production, où « cote inchangée » suffisait
# à alerter alors qu'elle ne prouve aucune valeur.
def _live(*, home=1.30, away=3.40, market=MarketType.H2H,
          labels=("home", "away"), odds=None, line=None, ek=EK,
          books=(Book.BETANO_BE, Book.STARCASINO_SPORT)):
    odds = odds or (home, away)
    return [
        OddQuote(event_key=ek, book=b, market=market,
                 outcome=Outcome(label=lab, line=line), decimal_odd=o,
                 fetched_at=NOW, source_event_id="live", from_live_feed=True)
        for b in books for lab, o in zip(labels, odds)
    ]


def test_the_production_case_is_detected():
    """Pinnacle connaissait le match, ne le price plus (il est en direct), le
    coup d'envoi est dépassé de 19 min, et Circus propose encore du prématch."""
    late = find_late_markets(_pin_elsewhere(), [_soft()] + _live(), "soccer",
                             NOW, prior_odds=_frozen, recent=_recent())
    assert (EK, Book.CIRCUS_BE) in late
    assert len(late[(EK, Book.CIRCUS_BE)]) == 1


def test_a_match_pinnacle_still_prices_is_not_late():
    """Si Pinnacle price encore l'événement en prématch, c'est qu'il n'a pas
    commencé — quelle que soit l'heure affichée. C'est la disparition qui fait
    foi, pas l'horloge."""
    late = find_late_markets([_pin()], [_soft()], "soccer", NOW, prior_odds=_frozen, recent=_recent())
    assert late == {}


def test_a_live_quote_is_never_flagged():
    """Betano expose un flux live, fusionné avec son prématch dans la même
    liste. Sans ce filtre, tout match en cours qu'il price passerait pour une
    erreur du book."""
    late = find_late_markets(_pin_elsewhere(), [_soft(book=Book.BETANO_BE, live=True)],
                             "soccer", NOW, prior_odds=_frozen, recent=_recent())
    assert late == {}


def test_an_event_pinnacle_never_priced_is_ignored():
    """Un match que Pinnacle n'a jamais pricé peut simplement avoir un horaire
    faux chez le book. Sans confirmation par la référence, on se tait."""
    late = find_late_markets(_pin_elsewhere(), [_soft()], "soccer", NOW, prior_odds=_frozen, recent={})
    assert late == {}


def test_a_match_too_recent_is_ignored():
    """Sous le seuil, on signalerait surtout des coups d'envoi retardés de
    quelques minutes et des arrondis de programmation."""
    ko = NOW - timedelta(minutes=3)
    ek = event_key("Union Saint-Gilloise", "Anderlecht", ko)
    late = find_late_markets(_pin_elsewhere(), [_soft(ek=ek)], "soccer", NOW, prior_odds=_frozen, recent=_recent(ek))
    assert late == {}


def test_a_match_long_finished_is_ignored():
    """Deux heures après le coup d'envoi, un marché encore ouvert ne relève
    plus de l'oubli mais d'un horaire faux — et le pari ne serait pas payé."""
    ko = NOW - timedelta(minutes=140)
    ek = event_key("Union Saint-Gilloise", "Anderlecht", ko)
    late = find_late_markets(_pin_elsewhere(), [_soft(ek=ek)], "soccer", NOW, prior_odds=_frozen, recent=_recent(ek))
    assert late == {}


def test_the_book_own_kickoff_can_veto():
    """Au tennis le rapprochement tolère trois heures d'écart. Si le book
    annonce un coup d'envoi encore à venir, c'est peut-être lui qui a raison :
    on ne l'accuse pas sur la seule foi de l'heure de Pinnacle."""
    pin_ko = NOW - timedelta(minutes=40)
    book_ko = NOW + timedelta(minutes=30)          # le book dit : pas commencé
    ref = event_key("Alcaraz", "Sinner", pin_ko)
    cand = event_key("Alcaraz", "Sinner", book_ko)
    late = find_late_markets(_pin_elsewhere(), [_soft(ek=cand)], "tennis", NOW,
                             prior_odds=_frozen, recent=_recent(ref))
    assert late == {}


def test_several_markets_of_one_book_are_grouped():
    """Les marchés figés d'un même book arrivent ensemble — mais seules les
    issues DU BON CÔTÉ sortent.

    Circus est figé à 2.40 sur les deux camps. Le match a tourné : le consensus
    live donne 72 % au domicile. Prendre le domicile à 2.40 vaut +73 % ; prendre
    l'extérieur à 2.40 quand il ne vaut plus que 28 % est un mauvais pari, pas
    une occasion. L'ancienne règle signalait les deux, faute de mesurer quoi que
    ce soit."""
    quotes = [
        _soft(market=MarketType.H2H, label="home"),
        _soft(market=MarketType.H2H, label="away"),
        _soft(market=MarketType.TOTALS, label="over", line=2.5),
    ]
    quotes += _live()
    quotes += _live(market=MarketType.TOTALS, labels=("over", "under"),
                    odds=(1.25, 3.60), line=2.5)
    late = find_late_markets(_pin_elsewhere(), quotes, "soccer", NOW, prior_odds=_frozen, recent=_recent())
    kept = late[(EK, Book.CIRCUS_BE)]
    assert {(q.market, q.outcome.label) for q in kept} == {
        (MarketType.H2H, "home"), (MarketType.TOTALS, "over"),
    }


def test_two_books_are_reported_separately():
    quotes = [_soft(book=Book.CIRCUS_BE), _soft(book=Book.UNIBET_BE)] + _live()
    late = find_late_markets(_pin_elsewhere(), quotes, "soccer", NOW,
                             prior_odds=_frozen, recent=_recent())
    assert {b for _, b in late} == {Book.CIRCUS_BE, Book.UNIBET_BE}


# ── Les deux causes du flood de production ────────────────────────────────
# Le détecteur a noyé le canal critique dès sa mise en service. Deux défauts
# indépendants, chacun capable à lui seul de produire des dizaines d'alertes
# par cycle.

def test_a_book_pricing_live_is_not_an_error():
    """LA cause du flood. « Le book expose encore ce match » ne prouve rien :
    Circus, Unibet, Napoleon et StarCasino continuent d'exposer un match
    commencé et le reprice en direct, sans qu'aucun champ ne le dise. Seule
    une cote restée IDENTIQUE à celle d'avant le coup d'envoi prouve l'oubli."""
    late = find_late_markets(_pin_elsewhere(), [_soft()], "soccer", NOW,
                             prior_odds=_moved, recent=_recent())
    assert late == {}


def test_without_history_we_stay_silent():
    """Exiger une preuve positive d'immobilité, pas une absence de preuve du
    contraire : un match jamais relevé avant son coup d'envoi (daemon
    redémarré, book ajouté la veille) ne prouve aucune erreur."""
    late = find_late_markets(_pin_elsewhere(), [_soft()], "soccer", NOW,
                             prior_odds=_no_history, recent=_recent())
    assert late == {}


def test_only_the_frozen_market_of_a_live_book_is_reported():
    """Un book peut repricer son 1X2 en direct tout en oubliant son BTTS.
    C'est précisément l'occasion recherchée, donc le filtre est par issue et
    non par match — sinon on jetterait le bébé avec l'eau du bain."""
    def half(_ek, _book, _before):
        return {("h2h", "home", None): 3.9,       # bougée : pricée en direct
                ("btts", "over", None): 2.4}      # figée : oubliée
    quotes = [_soft(market=MarketType.H2H, label="home"),
              _soft(market=MarketType.BTTS, label="over")]
    quotes += _live()
    quotes += _live(market=MarketType.BTTS, labels=("over", "under"),
                    odds=(1.20, 4.20))
    late = find_late_markets(_pin_elsewhere(), quotes, "soccer", NOW,
                             prior_odds=half, recent=_recent())
    kept = late[(EK, Book.CIRCUS_BE)]
    assert [q.market for q in kept] == [MarketType.BTTS]


def test_a_silent_pinnacle_proves_no_disappearance():
    """Second défaut. Sans réponse de Pinnacle, aucun événement n'est dans
    `live_now` : tous les matchs mémorisés passent pour disparus et le veto
    « Pinnacle le price encore » saute sans bruit. Un recul après 403 ou un
    sondage espacé suffisent — donc au moment où l'on est le moins sûr, le
    détecteur devenait le plus bavard."""
    late = find_late_markets([], [_soft()], "soccer", NOW,
                             prior_odds=_frozen, recent=_recent())
    assert late == {}


def test_the_counters_explain_a_silence():
    """Un filtre trop strict et un book sans erreur donnent le même résultat :
    rien. Sans compteurs, impossible de savoir laquelle des deux situations on
    observe — et c'est le mode d'échec le plus coûteux du projet."""
    from collections import Counter
    stats: Counter = Counter()
    find_late_markets(_pin_elsewhere(), [_soft()], "soccer", NOW,
                      prior_odds=_moved, recent=_recent(), stats=stats)
    assert stats["cote_bougée"] == 1
    stats.clear()
    find_late_markets([], [_soft()], "soccer", NOW,
                      prior_odds=_frozen, recent=_recent(), stats=stats)
    assert stats["pinnacle_muet"] == 1


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


def test_a_goal_bypasses_the_reminder_delay(monkeypatch):
    """C'est tout l'intérêt : attendre le rappel suivant ferait manquer la
    seule minute qui compte.

    ⚠️ Le remplacement vise `src.late_markets`, pas `src.main`. Depuis que
    `_report_late_markets` habite `late_markets.py`, c'est LÀ que le nom
    `send_late_market_alerts` est résolu ; poser un faux sur `main` ne
    changerait que la copie réexportée et laisserait tourner le vrai envoi.
    Ce test l'a prouvé en tombant bruyamment au moment du découpage — ce qui
    est le bon comportement : le mode de panne à craindre était l'inverse, un
    test qui continue de passer sans plus rien remplacer."""
    import src.late_markets as lm
    lm._LATE_ALERTED.clear(); lm._LIVE_SCORES.clear()
    late = {(EK, Book.CIRCUS_BE): [_soft()]}
    sent = []
    monkeypatch.setattr(lm, "send_late_market_alerts",
                        lambda items, cfg, **kw: (sent.extend(items) or items))
    try:
        lm._report_late_markets(late, "soccer", object())
        assert len(sent) == 1, "première alerte"
        lm._report_late_markets(late, "soccer", object())
        assert len(sent) == 1, "silence pendant le délai"
        lm._report_late_markets(late, "soccer", object(), goals={EK})
        assert len(sent) == 2, "un but doit rouvrir la parole immédiatement"
    finally:
        lm._LATE_ALERTED.clear()


# ── La règle du consensus live ────────────────────────────────────────────
# « Cote inchangée » prouve que le book n'a pas repricé. Ça ne prouve PAS que le
# pari vaut quelque chose. Deux situations produisent une cote inchangée : le
# book a oublié de suspendre un 1-1 (exploitable), et le book est simplement
# lent sur un 0-0 sans histoire (rien à gagner). Le second noyait le canal.

def test_a_slow_book_on_an_uneventful_match_is_silent():
    """LE faux positif signalé : tout a commencé, les autres books pricent en
    direct à peu près au même prix, et le book figé n'offre donc aucun écart.
    Rien ne s'est passé dans le match — il n'y a rien à jouer."""
    quotes = [_soft()] + _live(home=2.35, away=1.60)   # consensus ≈ le prix figé
    late = find_late_markets(_pin_elsewhere(), quotes, "soccer", NOW,
                             prior_odds=_frozen, recent=_recent())
    assert late == {}


def test_a_market_the_game_has_settled_is_reported_with_its_edge():
    """Le cas utile : le match a tourné, le consensus live est à 1.30, et le
    book figé paie encore 2.40. C'est cet écart qui fait l'alerte."""
    from src.main import late_market_edge
    quotes = [_soft()] + _live(home=1.30, away=3.40)
    late = find_late_markets(_pin_elsewhere(), quotes, "soccer", NOW,
                             prior_odds=_frozen, recent=_recent())
    kept = late[(EK, Book.CIRCUS_BE)]
    assert len(kept) == 1
    assert late_market_edge(EK, Book.CIRCUS_BE, kept[0]) > 50.0


def test_without_a_live_consensus_we_stay_silent():
    """Personne d'autre ne price ce marché en direct : impossible de savoir si
    le prix figé est devenu absurde. Même exigence de preuve positive que pour
    l'historique."""
    late = find_late_markets(_pin_elsewhere(), [_soft()], "soccer", NOW,
                             prior_odds=_frozen, recent=_recent())
    assert late == {}


def test_one_live_book_is_not_a_consensus():
    """Un seul book qui a bougé peut se tromper tout seul. Deux qui bougent
    ensemble, c'est le marché."""
    quotes = [_soft()] + _live(books=(Book.BETANO_BE,))
    late = find_late_markets(_pin_elsewhere(), quotes, "soccer", NOW,
                             prior_odds=_frozen, recent=_recent())
    assert late == {}


def test_the_frozen_book_never_feeds_its_own_consensus():
    """Un book ne peut pas servir de référence à lui-même : sa propre cote
    figée tirerait le consensus vers le prix périmé et effacerait l'écart."""
    quotes = [_soft(book=Book.BETANO_BE)] + _live(
        books=(Book.BETANO_BE, Book.STARCASINO_SPORT, Book.NAPOLEON_BE))
    late = find_late_markets(_pin_elsewhere(), quotes, "soccer", NOW,
                             prior_odds=_frozen, recent=_recent())
    assert (EK, Book.BETANO_BE) in late


def test_an_incomplete_market_is_not_devigged():
    """Un 1X2 dont il manque une issue donne une somme d'implicites sous 1 :
    le déviger fabriquerait des probabilités inventées."""
    from src.live_consensus import book_probs
    assert book_probs({"home": 2.0, "draw": 3.5}) is None       # somme 0.79
    assert book_probs({"home": 2.0}) is None                     # une seule issue
    assert book_probs({"home": 2.0, "draw": 3.5, "away": 3.4}) is not None


# ── La fuite de _LATE_EDGES ───────────────────────────────────────────────
# Ce dictionnaire était le SEUL état du module à ne jamais être purgé.
# `_PINNACLE_RECENT` a son TTL, `_LIVE_SCORES` a `forget_finished_scores`,
# `_LATE_ALERTED` est nettoyé à chaque rapport — celui-ci grossissait tant que
# le daemon tournait. Relevé pendant la préparation du LIVE (§21.22).

def test_les_ecarts_des_matchs_termines_sont_oublies():
    import src.late_markets as lm
    lm._LATE_EDGES.clear()

    vieux_ko = NOW - timedelta(hours=9)
    vieux_ek = event_key("Vieux", "Match", vieux_ko)
    lm._LATE_EDGES[(vieux_ek, Book.CIRCUS_BE, "h2h", "home", None)] = 42.0
    lm._LATE_EDGES[(EK, Book.CIRCUS_BE, "h2h", "home", None)] = 21.0

    otes = lm.forget_old_edges(NOW)

    assert otes == 1, "seul le match de neuf heures doit partir"
    assert (EK, Book.CIRCUS_BE, "h2h", "home", None) in lm._LATE_EDGES, \
        "un match dans la fenêtre de détection doit survivre"
    assert lm.late_market_edge(EK, Book.CIRCUS_BE, _soft()) == 21.0
    lm._LATE_EDGES.clear()


def test_une_cle_illisible_ne_reste_pas_coincee():
    """Une clé qu'on ne sait plus dater ne pourra plus jamais être consultée :
    la garder serait une fuite que rien ne viderait."""
    import src.late_markets as lm
    lm._LATE_EDGES.clear()
    lm._LATE_EDGES[("pas-une-cle", Book.CIRCUS_BE, "h2h", "home", None)] = 1.0
    assert lm.forget_old_edges(NOW) == 1
    assert not lm._LATE_EDGES


def test_la_purge_tourne_meme_sans_detection():
    """⚠️ Le cas qui compte. Un cycle sans marché en retard ne retouche pas le
    dictionnaire — c'est précisément là qu'il resterait tel quel si la purge
    était posée au moment de l'écriture plutôt qu'au début du cycle."""
    import src.late_markets as lm
    lm._LATE_EDGES.clear()
    vieux_ek = event_key("Vieux", "Match", NOW - timedelta(hours=9))
    lm._LATE_EDGES[(vieux_ek, Book.CIRCUS_BE, "h2h", "home", None)] = 42.0

    # Aucun événement disparu → aucune détection, et le retour est vide.
    assert lm.find_late_markets([_pin()], [], "soccer", NOW,
                                prior_odds=lambda *a, **k: {},
                                recent=_recent()) == {}
    assert not lm._LATE_EDGES, "la purge doit avoir tourné malgré zéro détection"
