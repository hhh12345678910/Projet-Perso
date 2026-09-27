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

def test_chaque_book_garde_sa_graphie():
    """Le registre commun ne garde que le DERNIER nom vu, tous books
    confondus ; le registre par book, celui de chaque book."""
    teams.record_pair("Club Brugge", "Anderlecht", Book.PINNACLE)
    teams.record_pair("Club Brugge KV", "RSC Anderlecht", Book.LADBROKES_BE)
    teams.record_pair("Club Brugge", "Anderlecht", Book.PINNACLE)
    assert teams.display_for_book(Book.LADBROKES_BE, "brugge") == "Club Brugge KV"
    assert teams.display_for_book("ladbrokes_be", "anderlecht") == "RSC Anderlecht"
    assert teams.display_for_book(Book.PINNACLE, "brugge") == "Club Brugge"
    assert teams.display_for_book(Book.UNIBET_BE, "brugge") is None


def test_le_registre_par_book_survit_a_un_redemarrage(tmp_path):
    from src.storage import Storage
    st = Storage(str(tmp_path / "v.db"))
    teams.init(st)
    teams.record("Club Brugge KV", Book.LADBROKES_BE)
    teams.clear_cache()
    teams.init(st)
    assert teams.display_for_book(Book.LADBROKES_BE, "brugge") == "Club Brugge KV"


def test_sans_book_rien_ne_change():
    teams.record_pair("Club Brugge", "Anderlecht")
    assert teams.display("brugge") == "Club Brugge"
    assert teams.display_for_book(Book.PINNACLE, "brugge") is None


# ---------------------------------------------------------- value bet ---

def test_l_alerte_montre_les_noms_du_book():
    teams.record_pair("Club Brugge", "Anderlecht", Book.PINNACLE)
    teams.record_pair("Club Brugge KV", "RSC Anderlecht", Book.LADBROKES_BE)
    msg = format_value_bet(_vb())
    assert "Club Brugge KV vs RSC Anderlecht" in msg


def test_sans_nom_du_book_on_retombe_sur_le_registre_commun():
    teams.record_pair("Club Brugge", "Anderlecht", Book.PINNACLE)
    assert "Club Brugge vs Anderlecht" in format_value_bet(_vb())


def test_un_book_a_l_envers_garde_le_pari_sur_la_bonne_equipe():
    """Ladbrokes liste « RSC Anderlecht – Club Brugge KV », Pinnacle « Club
    Brugge – Anderlecht ». La cote Ladbrokes est sur ANDERLECHT (son home).
    Après réalignement, le label devient « away » (repère Pinnacle) ; le
    message, lui, montre l'ordre de Ladbrokes et doit donc dire « home » —
    l'équipe citée en premier, Anderlecht."""
    teams.record_pair("Club Brugge", "Anderlecht", Book.PINNACLE)
    teams.record_pair("RSC Anderlecht", "Club Brugge KV", Book.LADBROKES_BE)
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
    assert "Pari : <b>home</b> @ 3.50" in msg


def test_les_totaux_ne_se_retournent_pas():
    teams.record_pair("RSC Anderlecht", "Club Brugge KV", Book.LADBROKES_BE)
    vb = _vb(market=MarketType.TOTALS, outcome=Outcome(label="over", line=2.5),
             book_event_key=event_key("RSC Anderlecht", "Club Brugge KV", T),
             book_swapped=True)
    assert "Pari : <b>over 2.5</b>" in format_value_bet(vb)


def test_le_nul_ne_se_retourne_pas():
    vb = _vb(outcome=Outcome(label="draw"),
             book_event_key=event_key("RSC Anderlecht", "Club Brugge KV", T),
             book_swapped=True)
    assert "Pari : <b>draw</b>" in format_value_bet(vb)


def test_meme_ordre_le_label_ne_bouge_pas():
    teams.record_pair("Club Brugge KV", "RSC Anderlecht", Book.LADBROKES_BE)
    vb = _vb(outcome=Outcome(label="away"),
             book_event_key=event_key("Club Brugge KV", "RSC Anderlecht", T))
    msg = format_value_bet(vb)
    assert "Club Brugge KV vs RSC Anderlecht" in msg and "Pari : <b>away</b>" in msg


# ------------------------------------------------------------- féminin ---

def test_un_match_feminin_se_lit_dans_la_ligue_pinnacle():
    """Pinnacle écrit « Houston Dash » dans « USA - National Womens Soccer
    League » : rien dans les noms."""
    ek = event_key("Houston Dash", "Orlando Pride", T)
    teams.record_pair("Houston Dash", "Orlando Pride", Book.LADBROKES_BE)
    msg = format_value_bet(_vb(event_key=ek),
                           ligue_ref="USA - National Womens Soccer League")
    assert "Houston Dash (f) vs Orlando Pride (f)" in msg


def test_un_match_masculin_n_a_pas_de_f():
    teams.record_pair("Club Brugge KV", "RSC Anderlecht", Book.LADBROKES_BE)
    msg = format_value_bet(_vb(), ligue_ref="Belgium - Pro League")
    assert "(f)" not in msg


def test_l_abreviation_du_book_suffit():
    """Ladbrokes : noms nus, « FÉM. » dans sa ligue."""
    teams.record_pair("CD Real Santander", "Once Caldas SA", Book.LADBROKES_BE)
    ek = event_key("CD Real Santander", "Once Caldas SA", T)
    msg = format_value_bet(_vb(event_key=ek, league="COLOMBIE - 1ère DIVISION FÉM."))
    assert "CD Real Santander (f) vs Once Caldas SA (f)" in msg


def test_le_marqueur_du_book_est_remplace_par_f():
    teams.record_pair("Heips (W)", "Coritiba FC PR (W)", Book.UNIBET_BE)
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
    teams.record_pair("RSC Anderlecht", "Club Brugge KV", Book.LADBROKES_BE)
    ref = event_key("Club Brugge", "Anderlecht", T)
    q = _q(Book.LADBROKES_BE, "RSC Anderlecht", "Club Brugge KV", "home", 3.5)
    msg = format_late_market(ref, Book.LADBROKES_BE, [q], 12.0, sport="soccer")
    assert "RSC Anderlecht vs Club Brugge KV" in msg


def test_l_alerte_clv_prend_les_noms_du_book_et_le_f():
    teams.record_pair("Houston Dash W", "Orlando Pride W", Book.MERIDIAN_BE)
    ek = event_key("Houston Dash W", "Orlando Pride W", T)
    row = {"event_key": ek, "book": "meridian_be", "line": None,
           "outcome_label": "home", "odd_taken": 2.1, "ev_pct": 5.0,
           "kelly_pct": 1.0}

    class _Row(dict):
        def keys(self):
            return super().keys()
    msg = format_clv_alert(_Row(row), 4.0, 1.9, 30)
    assert "Houston Dash (f) vs Orlando Pride (f)" in msg


def test_le_surebet_porte_le_f():
    ek = event_key("Houston Dash", "Orlando Pride", T)
    teams.record_pair("Houston Dash", "Orlando Pride", Book.PINNACLE)
    sb = Surebet(event_key=ek, market=MarketType.H2H, line=None,
                 legs={"home": (2.1, Book.UNIBET_BE), "draw": (3.8, Book.LADBROKES_BE),
                       "away": (4.2, Book.BETANO_BE)},
                 margin=0.02)
    msg = format_surebet(sb, ligue_ref="USA - National Womens Soccer League")
    assert "Houston Dash (f) vs Orlando Pride (f)" in msg
