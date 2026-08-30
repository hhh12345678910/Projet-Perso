"""La bande longue du premium, fermée pour certains sports seulement.

Mesuré le 30/08 : la tranche de cotes 4,0-6,0 rend −20,34 % de ROI (n=406,
−2,3σ), et cette population est à 96 % du TENNIS (1 119 paris contre 34 au
soccer). Fermer la bande pour TOUS les sports appliquerait donc au soccer une
conclusion tirée d'un échantillon qui n'en contient presque pas.

⚠️ Le test qui compte est `test_la_bande_STANDARD_n_est_jamais_touchee` :
c'est elle qui porte le rendement (+19,92 % sur la tranche 1,8-2,3), et la
fermer par accident coûterait bien plus que ce que la bande longue fait
perdre.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.alerter import TelegramAlerter, TelegramConfig
from src.models import Book, MarketType, Outcome, ValueBet

MAINTENANT = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)


def _pari(odd: float, ev: float) -> ValueBet:
    return ValueBet(
        event_key="209912011200::a__vs__b", book=Book.UNIBET_BE,
        market=MarketType.H2H, outcome=Outcome("home"), odd_taken=odd,
        fair_prob=0.5, fair_odd=2.0, ev_pct=ev, kelly_stake_pct=1.0,
        detected_at=MAINTENANT)


def _cfg(**kw) -> TelegramConfig:
    base = dict(bot_token="jeton", chat_id="PRINCIPAL",
                premium_chat_id="PREMIUM", critical_chat_id="CRITIQUE",
                min_minutes_to_kickoff=0)
    base.update(kw)
    return TelegramConfig(**base)


class _Espion(TelegramAlerter):
    """Capture les envois au lieu de parler à Telegram."""

    def __init__(self, cfg):
        super().__init__(cfg)
        self.envois = []

    def _send(self, text, chat_id, reply_markup=None):
        self.envois.append((chat_id, text))
        return True

    def canaux(self, bet, sport):
        self.envois.clear()
        self.send_value_bet(bet, sport=sport)
        return {c for c, _ in self.envois}


@pytest.fixture
def sans_exclusion():
    return _Espion(_cfg())


@pytest.fixture
def tennis_exclu():
    return _Espion(_cfg(premium_hi_sports_exclus=("tennis",)))


# ══ le comportement demandé ════════════════════════════════════════════
def test_le_tennis_en_cote_4_a_6_ne_va_plus_au_premium(tennis_exclu):
    assert "PREMIUM" not in tennis_exclu.canaux(_pari(5.00, 25.0), "tennis")


def test_le_MEME_pari_au_soccer_y_va_toujours(tennis_exclu):
    """L'exclusion est PAR SPORT. Le soccer n'a que 34 paris scorés : on n'a
    rien mesuré sur lui, donc on ne décide rien pour lui."""
    assert "PREMIUM" in tennis_exclu.canaux(_pari(5.00, 25.0), "soccer")


def test_sans_reglage_le_tennis_y_va_comme_avant(sans_exclusion):
    """Le défaut ne change RIEN. Un réglage absent ne doit jamais modifier le
    comportement de production."""
    assert "PREMIUM" in sans_exclusion.canaux(_pari(5.00, 25.0), "tennis")


# ══ ce qui ne doit surtout pas bouger ══════════════════════════════════
def test_la_bande_STANDARD_n_est_jamais_touchee(tennis_exclu):
    """LE test qui compte. La bande 1,5-4 porte le rendement : +19,92 % sur
    la tranche 1,8-2,3, contre −31,14 % sur 4,0-6,0. La fermer par accident
    coûterait bien plus que ce que la bande longue fait perdre."""
    assert "PREMIUM" in tennis_exclu.canaux(_pari(2.00, 12.0), "tennis")
    assert "PREMIUM" in tennis_exclu.canaux(_pari(3.90, 9.0), "tennis")


def test_une_cote_exactement_a_4_reste_prise(tennis_exclu):
    """4,00 appartient AUX DEUX bandes. L'exclusion ne vise que la longue :
    la standard doit continuer de la prendre."""
    assert "PREMIUM" in tennis_exclu.canaux(_pari(4.00, 12.0), "tennis")


def test_un_sport_INCONNU_n_est_pas_exclu(tennis_exclu):
    """La production passe toujours le sport. Mais si un appel l'omettait,
    écarter par défaut supprimerait des paris d'un sport qu'on n'a jamais
    voulu couper — une panne silencieuse dans le mauvais sens."""
    assert "PREMIUM" in tennis_exclu.canaux(_pari(5.00, 25.0), None)


# ══ où le pari va-t-il à la place ? ════════════════════════════════════
def test_le_pari_ecarte_reste_DETECTE_et_visible(tennis_exclu):
    """« Je ne joue que le premium, si c'est déplacé on s'en fout. »

    Le pari ne disparaît pas : le canal critique le reprend au-delà de son
    seuil. On continue donc de le voir et de le mesurer, sans le jouer.
    """
    canaux = tennis_exclu.canaux(_pari(5.00, 40.0), "tennis")
    assert "PREMIUM" not in canaux
    assert "CRITIQUE" in canaux


def test_la_casse_du_nom_de_sport_n_a_pas_d_importance():
    e = _Espion(_cfg(premium_hi_sports_exclus=("tennis",)))
    assert "PREMIUM" not in e.canaux(_pari(5.00, 25.0), "Tennis")
    assert "PREMIUM" not in e.canaux(_pari(5.00, 25.0), "TENNIS")
