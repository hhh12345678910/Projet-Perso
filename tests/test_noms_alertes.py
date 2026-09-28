"""Les noms d'équipes d'une alerte : ceux du BOOK de l'alerte, dans son ordre,
et « (f) » après chaque nom d'un match féminin (demande du 27/09).

Le piège n°1 : un book qui liste les équipes à l'envers de Pinnacle. Le
pipeline tient le label du pari dans le repère de Pinnacle ; afficher les noms
du book dans SON ordre sans retourner le label ferait parier sur l'autre
équipe. Ces tests suivent donc le vrai trajet : cote du book → réalignement →
value bet → message.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src import teams
from src.alerter import (_equipe, _est_feminin, format_clv_alert, format_late_market,
                         format_surebet, format_value_bet)
from src.config import ScanConfig
from src.detection import find_value_bets
from src.matcher import event_key
from src.models import Book, FairLine, MarketType, OddQuote, Outcome, ValueBet
from src.reference import remap_to_reference
from src.surebet import Surebet

T = datetime(2099, 6, 1, 18, tzinfo=timezone.utc)
NOW = datetime(2026, 9, 27, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _registre_vide():
    teams.clear_cache()
    yield
    teams.clear_cache()


def _q(book, home, away, label, odd, market=MarketType.H2H, line=None):
    return OddQuote(event_key=event_key(home, away, T), book=book, market=market,
                    outcome=Outcome(label=label, line=line), decimal_odd=odd,
                    fetched_at=NOW, source_event_id="x")


def _vb(**kw) -> ValueBet:
    base = dict(event_key=event_key("Club Brugge", "RSC Anderlecht", T),
                book=Book.LADBROKES_BE, market=MarketType.H2H,
                outcome=Outcome(label="home"), odd_taken=2.2, fair_prob=0.5,
                fair_odd=2.0, ev_pct=10.0, kelly_stake_pct=1.0, detected_at=NOW)
    base.update(kw)
    return ValueBet(**base)


# ------------------------------------------------------------ le registre ---

def test_chaque_book_garde_ses_noms_pour_chaque_match():
    """Les noms d'un book, par MATCH : sa clé d'événement (équipes et minute
    du coup d'envoi), celle de ses cotes."""
    teams.record_pair("Club Brugge", "Anderlecht", Book.PINNACLE, T)
    teams.record_pair("Club Brugge KV", "RSC Anderlecht", Book.LADBROKES_BE, T)
    teams.record_pair("Club Brugge", "Anderlecht", Book.PINNACLE, T)
    ek = event_key("Club Brugge", "Anderlecht", T)
    assert teams.noms_du_match(Book.LADBROKES_BE, ek) == ("Club Brugge KV", "RSC Anderlecht")
    assert teams.noms_du_match("pinnacle", ek) == ("Club Brugge", "Anderlecht")
    assert teams.noms_du_match(Book.UNIBET_BE, ek) is None


def test_deux_clubs_homonymes_d_un_meme_book_ne_se_confondent_pas():
    """Revue du 27/09 : « Club Olimpia » (Paraguay) et « CD Olimpia »
    (Honduras) donnent la même clé d'ÉQUIPE, `olimpia` ; un registre par
    équipe nommait l'un pour l'autre. Par match, jamais."""
    demain = T.replace(day=2)
    teams.record_pair("Club Olimpia", "Club Libertad", Book.UNIBET_BE, T)
    teams.record_pair("CD Olimpia", "FC Motagua", Book.UNIBET_BE, demain)
    vb = _vb(book=Book.UNIBET_BE, event_key=event_key("Olimpia", "Libertad", T))
    msg = format_value_bet(vb)
    assert "Club Olimpia vs Club Libertad" in msg
    assert "CD Olimpia" not in msg


def test_les_matchs_passes_sont_oublies(monkeypatch):
    monkeypatch.setattr(teams, "MAX_NOMS_MATCH", 1)
    vieux = datetime(2026, 9, 20, 15, tzinfo=timezone.utc)
    teams.record_pair("Arsenal", "Chelsea", Book.UNIBET_BE, vieux)
    teams.record_pair("Everton", "Fulham", Book.UNIBET_BE, T)
    assert teams.noms_du_match(Book.UNIBET_BE, event_key("Arsenal", "Chelsea", vieux)) is None
    assert teams.noms_du_match(Book.UNIBET_BE, event_key("Everton", "Fulham", T))


def test_sans_book_ni_heure_rien_ne_change():
    teams.record_pair("Club Brugge", "Anderlecht")
    teams.record_pair("Club Brugge KV", "RSC Anderlecht", Book.LADBROKES_BE)
    assert teams.display("brugge") == "Club Brugge KV"
    assert teams._NOMS_MATCH == {}


# ---------------------------------------------------------- value bet ---

def test_l_alerte_montre_les_noms_du_book():
    teams.record_pair("Club Brugge", "Anderlecht", Book.PINNACLE, T)
    teams.record_pair("Club Brugge KV", "RSC Anderlecht", Book.LADBROKES_BE, T)
    msg = format_value_bet(_vb())
    assert "Club Brugge KV vs RSC Anderlecht" in msg


def test_sans_nom_du_book_on_retombe_sur_le_registre_commun():
    teams.record_pair("Club Brugge", "Anderlecht")
    assert "Club Brugge vs Anderlecht" in format_value_bet(_vb())


def test_un_book_a_l_envers_garde_le_pari_sur_la_bonne_equipe():
    """Ladbrokes liste « RSC Anderlecht – Club Brugge KV », Pinnacle « Club
    Brugge – Anderlecht ». La cote Ladbrokes est sur ANDERLECHT (son home).
    Après réalignement, le label devient « away » (repère Pinnacle) ; le
    message, lui, montre l'ordre de Ladbrokes et doit donc dire « home » —
    l'équipe citée en premier, Anderlecht."""
    teams.record_pair("Club Brugge", "Anderlecht", Book.PINNACLE, T)
    teams.record_pair("RSC Anderlecht", "Club Brugge KV", Book.LADBROKES_BE, T)
    ref = event_key("Club Brugge", "Anderlecht", T)
    soft = [_q(Book.LADBROKES_BE, "RSC Anderlecht", "Club Brugge KV", lab, odd)
            for lab, odd in (("home", 3.5), ("draw", 3.6), ("away", 2.2))]
    realignees = remap_to_reference(soft, [ref], "soccer")
    anderlecht = next(q for q in realignees if q.decimal_odd == 3.5)
    assert anderlecht.outcome.label == "away" and anderlecht.book_swapped
    fair = {(ref, MarketType.H2H, None): FairLine(
        event_key=ref, market=MarketType.H2H,
        outcomes={"home": 0.40, "draw": 0.28, "away": 0.32})}
    bets = find_value_bets(realignees, fair, ScanConfig())
    vb = next(b for b in bets if b.odd_taken == 3.5)
    assert vb.book_swapped and vb.outcome.label == "away"
    msg = format_value_bet(vb)
    assert "RSC Anderlecht vs Club Brugge KV" in msg
    # « Home » = l'équipe citée en PREMIER juste au-dessus, Anderlecht. Le nom
    # n'est plus répété dans la ligne (demande du 28/09).
    assert "Pari : <b>Home</b> - 3.50 (fair" in msg
    assert msg.index("RSC Anderlecht") < msg.index("Club Brugge KV")


def test_les_totaux_ne_se_retournent_pas():
    teams.record_pair("RSC Anderlecht", "Club Brugge KV", Book.LADBROKES_BE, T)
    vb = _vb(market=MarketType.TOTALS, outcome=Outcome(label="over", line=2.5),
             book_event_key=event_key("RSC Anderlecht", "Club Brugge KV", T),
             book_swapped=True)
    assert "Pari : <b>Over 2.5</b> - " in format_value_bet(vb)


def test_le_nul_ne_se_retourne_pas():
    vb = _vb(outcome=Outcome(label="draw"),
             book_event_key=event_key("RSC Anderlecht", "Club Brugge KV", T),
             book_swapped=True)
    assert "Pari : <b>Draw</b> - " in format_value_bet(vb)


def test_meme_ordre_le_label_ne_bouge_pas():
    teams.record_pair("Club Brugge KV", "RSC Anderlecht", Book.LADBROKES_BE, T)
    vb = _vb(outcome=Outcome(label="away"),
             book_event_key=event_key("Club Brugge KV", "RSC Anderlecht", T))
    msg = format_value_bet(vb)
    assert "Club Brugge KV vs RSC Anderlecht" in msg and "Pari : <b>Away</b> - " in msg


# ------------------------------------------------------------- féminin ---

def test_un_match_feminin_se_lit_dans_la_ligue_pinnacle():
    """Pinnacle écrit « Houston Dash » dans « USA - National Womens Soccer
    League » : rien dans les noms."""
    ek = event_key("Houston Dash", "Orlando Pride", T)
    teams.record_pair("Houston Dash", "Orlando Pride", Book.LADBROKES_BE, T)
    msg = format_value_bet(_vb(event_key=ek),
                           ligue_ref="USA - National Womens Soccer League")
    assert "Houston Dash (f) vs Orlando Pride (f)" in msg


def test_un_match_masculin_n_a_pas_de_f():
    teams.record_pair("Club Brugge KV", "RSC Anderlecht", Book.LADBROKES_BE, T)
    msg = format_value_bet(_vb(), ligue_ref="Belgium - Pro League")
    assert "(f)" not in msg


def test_l_abreviation_du_book_suffit():
    """Ladbrokes : noms nus, « FÉM. » dans sa ligue."""
    teams.record_pair("CD Real Santander", "Once Caldas SA", Book.LADBROKES_BE, T)
    ek = event_key("CD Real Santander", "Once Caldas SA", T)
    msg = format_value_bet(_vb(event_key=ek, league="COLOMBIE - 1ère DIVISION FÉM."))
    assert "CD Real Santander (f) vs Once Caldas SA (f)" in msg


def test_le_marqueur_du_book_est_remplace_par_f():
    teams.record_pair("Heips (W)", "Coritiba FC PR (W)", Book.UNIBET_BE, T)
    ek = event_key("Heips (W)", "Coritiba FC PR (W)", T)
    msg = format_value_bet(_vb(event_key=ek, book=Book.UNIBET_BE))
    assert "Heips (f) vs Coritiba FC PR (f)" in msg


@pytest.mark.parametrize("nom, attendu", [
    ("Arsenal W", "Arsenal (f)"), ("Arsenal Women", "Arsenal (f)"),
    ("Lyon (F)", "Lyon (f)"), ("Standard Fém.", "Standard (f)"),
    ("Bayern Frauen", "Bayern (f)"), ("Barcelona Femenino", "Barcelona (f)"),
    ("Juventus Femminile", "Juventus (f)"), ("Arsenal Women (W)", "Arsenal (f)"),
    ("Houstondashxwomen", "Houstondash (f)"), ("W Connection", "W Connection (f)"),
    ("Houston Dash", "Houston Dash (f)"),
])
def test_le_nom_feminin_affiche(nom, attendu):
    assert _equipe(nom, True) == attendu
    assert _equipe(nom, False) == nom


@pytest.mark.parametrize("ligue", [
    # Revue du 27/09 : ces compétitions ne disent pas « Women » dans leur nom.
    "Sweden - Damallsvenskan", "Norway - Toppserien", "Spain - Liga F",
    "France - D1 Arkema", "USA - NWSL", "England - Barclays WSL",
    "Mexico - Liga MX Femenil", "Belgium - Super League Vrouwen",
    "Denmark - Kvindeliga", "Japan - WE League", "USA - WNBA",
    "ITF W50 Incheon", "ITF W15 Monastir", "WTA125 Florianopolis",
    "USA - National Womens Soccer League", "COLOMBIE - 1ère DIVISION FÉM.",
])
def test_les_ligues_feminines_reelles(ligue):
    assert _est_feminin((), (ligue,), "soccer")


@pytest.mark.parametrize("ligue", [
    "Sweden - Allsvenskan", "Belgium - Pro League", "England - Premier League",
    "ATP Paris - Doubles", "Trinidad and Tobago - TT Premier League",
    "Spain - LaLiga", "Norway - Eliteserien", "Germany - Bundesliga",
])
def test_les_ligues_masculines(ligue):
    assert not _est_feminin((), (ligue,), "soccer")


def test_w_connection_n_est_pas_un_match_feminin():
    """« W Connection » (Trinidad, un club d'hommes) devient `xwomen` à lui
    seul : un seul camp marqué ne fait pas un match féminin."""
    teams.record_pair("W Connection", "Defence Force", Book.LADBROKES_BE, T)
    ek = event_key("W Connection", "Defence Force", T)
    assert "xwomen" in ek
    msg = format_value_bet(_vb(event_key=ek),
                           ligue_ref="Trinidad and Tobago - TT Premier League")
    assert "(f)" not in msg and "W Connection vs Defence Force" in msg


def test_au_tennis_les_initiales_restent():
    """« Hsieh S-W », « Falkowska W » : un W final y est une initiale."""
    ek = event_key("Ostapenko J / Hsieh S-W", "Kawa K / Falkowska W", T)
    teams.record_pair("Ostapenko J / Hsieh S-W", "Kawa K / Falkowska W",
                      Book.LADBROKES_BE, T)
    msg = format_value_bet(_vb(event_key=ek), sport="tennis",
                           ligue_ref="WTA Beijing - Doubles")
    assert "Ostapenko J / Hsieh S-W (f) vs Kawa K / Falkowska W (f)" in msg


def test_au_tennis_seule_la_ligue_compte():
    """« Koolhof W / Skupski N » : le « W » est une initiale, `normalize_team`
    en fait pourtant un `xwomen`."""
    frag = event_key("Koolhof W / Skupski N", "Arevalo M / Pavic M", T).split("::")[1]
    assert "xwomen" in frag
    assert not _est_feminin(frag.split("__vs__"), ("ATP Paris - Doubles",), "tennis")
    assert _est_feminin((), ("WTA Wuhan",), "tennis")
    assert _est_feminin((), ("ITF Women Monastir",), "tennis")


# ------------------------------------------------- les autres alertes ---

def test_le_marche_en_retard_montre_le_book_dans_son_ordre():
    """Ses cotes gardent la clé du book et ses labels sont dans son repère :
    les noms doivent suivre, sinon « home » désigne l'autre équipe."""
    teams.record_pair("RSC Anderlecht", "Club Brugge KV", Book.LADBROKES_BE, T)
    ref = event_key("Club Brugge", "Anderlecht", T)
    q = _q(Book.LADBROKES_BE, "RSC Anderlecht", "Club Brugge KV", "home", 3.5)
    msg = format_late_market(ref, Book.LADBROKES_BE, [q], 12.0, sport="soccer")
    assert "RSC Anderlecht vs Club Brugge KV" in msg


def test_l_alerte_clv_nomme_l_equipe_et_porte_le_f():
    teams.record_pair("Houston Dash W", "Orlando Pride W", Book.MERIDIAN_BE, T)
    ek = event_key("Houston Dash W", "Orlando Pride W", T)
    row = {"event_key": ek, "book": "meridian_be", "line": None, "market": "h2h",
           "outcome_label": "home", "odd_taken": 2.1, "ev_pct": 5.0,
           "kelly_pct": 1.0}

    class _Row(dict):
        def keys(self):
            return super().keys()
    msg = format_clv_alert(_Row(row), 4.0, 1.9, 30)
    assert "Houston Dash (f) vs Orlando Pride (f)" in msg
    # L'alerte CLV suit l'ordre de Pinnacle : « Home » est l'équipe citée en
    # premier juste au-dessus, Houston Dash. Plus de nom dans la ligne
    # (demande du 28/09).
    assert "Pari : <b>Home</b> - 2.10" in msg
    assert "Houston Dash W @" not in msg


def test_le_surebet_porte_le_f():
    ek = event_key("Houston Dash", "Orlando Pride", T)
    teams.record_pair("Houston Dash", "Orlando Pride", Book.PINNACLE, T)
    sb = Surebet(event_key=ek, market=MarketType.H2H, line=None,
                 legs={"home": (2.1, Book.UNIBET_BE), "draw": (3.8, Book.LADBROKES_BE),
                       "away": (4.2, Book.BETANO_BE)},
                 margin=0.02)
    msg = format_surebet(sb, ligue_ref="USA - National Womens Soccer League")
    assert "Houston Dash (f) vs Orlando Pride (f)" in msg


# ------------------------------------------ marchés en retard à l'envers ---
#
# Revue du 27/09 : Pinnacle et Betano listent « Club Brugge – Anderlecht »,
# Brugge mène 2-0 ; Ladbrokes liste « RSC Anderlecht – Club Brugge KV » et n'a
# pas bougé son prématch (Anderlecht 2.80, Brugge 2.40). Le consensus mêlait
# les repères : le « home » de Ladbrokes (Anderlecht) était comparé au « home »
# des autres (Brugge) — alerte à +109 % sur l'équipe menée, et le score
# s'affichait à l'envers des noms.

KO = datetime(2026, 9, 1, 20, 41, tzinfo=timezone.utc)
MAINTENANT = datetime(2026, 9, 1, 21, 11, tzinfo=timezone.utc)
REF = event_key("Club Brugge", "Anderlecht", KO)
LAD = event_key("RSC Anderlecht", "Club Brugge KV", KO)


def _cote(book, ek, label, odd, live=False):
    return OddQuote(event_key=ek, book=book, market=MarketType.H2H,
                    outcome=Outcome(label=label), decimal_odd=odd, fetched_at=MAINTENANT,
                    source_event_id="s", from_live_feed=live)


def _figees(ek, book, _avant):
    # Sous la clé de la référence : cotes stockées APRÈS réalignement, donc
    # dans le repère de Pinnacle (home = Brugge).
    if ek == REF and book == Book.LADBROKES_BE:
        return {("h2h", "home", None): 2.40, ("h2h", "draw", None): 3.40,
                ("h2h", "away", None): 2.80}
    return {}


def _retard():
    from src.late_markets import find_late_markets
    pin_ailleurs = [OddQuote(event_key=event_key("Genk", "Gand", MAINTENANT),
                             book=Book.PINNACLE, market=MarketType.H2H,
                             outcome=Outcome(label="home"), decimal_odd=2.0,
                             fetched_at=MAINTENANT, source_event_id="p")]
    soft = [_cote(Book.LADBROKES_BE, LAD, "home", 2.80),
            _cote(Book.LADBROKES_BE, LAD, "draw", 3.40),
            _cote(Book.LADBROKES_BE, LAD, "away", 2.40)]
    for b in (Book.BETANO_BE, Book.STARCASINO_SPORT):
        soft += [_cote(b, REF, "home", 1.30, True), _cote(b, REF, "draw", 5.0, True),
                 _cote(b, REF, "away", 12.0, True)]
    return find_late_markets(pin_ailleurs, soft, "soccer", MAINTENANT,
                             prior_odds=_figees, recent={REF: 0.0})


def test_le_consensus_du_marche_en_retard_compare_la_meme_equipe():
    from src.late_markets import late_market_swapped
    late = _retard()
    retenues = late[(REF, Book.LADBROKES_BE)]
    # La vraie occasion : Brugge (« away » chez Ladbrokes) figé à 2.40 alors
    # qu'il mène 2-0 ; jamais Anderlecht, mené.
    assert [(q.outcome.label, q.decimal_odd) for q in retenues] == [("away", 2.40)]
    assert late_market_swapped(REF, Book.LADBROKES_BE)


def test_le_marche_en_retard_a_l_envers_montre_le_score_et_l_equipe_justes():
    teams.record_pair("RSC Anderlecht", "Club Brugge KV", Book.LADBROKES_BE, KO)
    retenues = _retard()[(REF, Book.LADBROKES_BE)]
    msg = format_late_market(REF, Book.LADBROKES_BE, retenues, 30.0, sport="soccer",
                             score=(2, 0, 30), swapped=True)
    assert "RSC Anderlecht vs Club Brugge KV" in msg
    assert "Score : 0-2" in msg                      # Anderlecht 0 – 2 Brugge
    assert "<b>away</b> — Club Brugge KV @ 2.40" in msg


def test_un_book_a_l_envers_qui_cote_en_direct_ne_fausse_pas_les_autres():
    """Vérification de la revue : Ladbrokes, à l'envers, reprice en direct
    (Anderlecht 12.0 / Brugge 1.30) ; Circus, dans l'ordre de Pinnacle, est
    figé (Brugge 2.45 / Anderlecht 2.90). Mêlés au consensus sans repère,
    les prix de Ladbrokes faisaient alerter Circus sur Anderlecht, mené."""
    from src.late_markets import find_late_markets

    def figees(ek, book, _avant):
        if ek == REF and book == Book.CIRCUS_BE:
            return {("h2h", "home", None): 2.45, ("h2h", "draw", None): 3.40,
                    ("h2h", "away", None): 2.90}
        return {}
    pin_ailleurs = [OddQuote(event_key=event_key("Genk", "Gand", MAINTENANT),
                             book=Book.PINNACLE, market=MarketType.H2H,
                             outcome=Outcome(label="home"), decimal_odd=2.0,
                             fetched_at=MAINTENANT, source_event_id="p")]
    soft = [_cote(Book.CIRCUS_BE, REF, "home", 2.45), _cote(Book.CIRCUS_BE, REF, "draw", 3.40),
            _cote(Book.CIRCUS_BE, REF, "away", 2.90),
            _cote(Book.LADBROKES_BE, LAD, "home", 12.0, True),
            _cote(Book.LADBROKES_BE, LAD, "draw", 5.0, True),
            _cote(Book.LADBROKES_BE, LAD, "away", 1.30, True),
            # Un second book en direct, dans l'ordre de Pinnacle : le
            # consensus en exige deux.
            _cote(Book.BETANO_BE, REF, "home", 1.30, True),
            _cote(Book.BETANO_BE, REF, "draw", 5.0, True),
            _cote(Book.BETANO_BE, REF, "away", 12.0, True)]
    late = find_late_markets(pin_ailleurs, soft, "soccer", MAINTENANT,
                             prior_odds=figees, recent={REF: 0.0})
    retenues = late.get((REF, Book.CIRCUS_BE), [])
    assert [(q.outcome.label, q.decimal_odd) for q in retenues] == [("home", 2.45)]


def test_l_observation_live_recoit_le_sport():
    """Au tennis, la ligue seule compte et les initiales restent — même en
    direct, où `Opportunite` ne porte pas le sport."""
    from types import SimpleNamespace
    from src.alerter import format_live_observation
    ek = event_key("Koolhof W / Skupski N", "Arevalo M / Pavic M W", T)
    _t, h, a = ek.split("::")[1].partition("__vs__")
    o = SimpleNamespace(home=_t, away=a, book=Book.UNIBET_BE, feed_score="0:0",
                        line=None, minute_ecoulee=None, age_fair_sec=1.0,
                        market=MarketType.H2H, outcome="home", cote_preneur=2.0,
                        fair_cote=1.9, ev_pct=5.0, age_preneur_sec=1.0,
                        delai_calcul_sec=0.1, partiel=False, issues_manquantes=[],
                        statut=SimpleNamespace(value="OK"), motif="")
    assert "(f)" not in format_live_observation(o, sport="tennis")
