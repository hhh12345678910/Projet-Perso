"""La seconde voie du critique : les grosses cotes que le premium refuse.

Fermer la bande longue du premium au tennis (§22) a créé un trou que
personne n'avait vu : un pari tennis en cote 4-6 avec 25 % d'EV ne partait
plus NULLE PART. Il fallait 35 % pour reparaître sur le canal critique.
Déplacer ces paris était voulu, les effacer non — l'utilisateur avait
demandé de « continuer à les détecter et les analyser ».

Le garde-fou central est `test_un_pari_ne_part_jamais_dans_les_deux` :
cette voie ne doit pas transformer le critique en copie du premium. Un
doublon par pari, c'est deux fois le même message à trier au moment où on
décide de jouer.

Le second est `test_la_bande_standard_ne_gagne_aucune_voie_critique` : la
tranche 1,5-4,0 porte le rendement (+19,92 % sur 1,8-2,3), et lui ajouter
une sortie critique la dédoublerait pour rien.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.alerter import TelegramAlerter, TelegramConfig
from src.models import Book, MarketType, Outcome, ValueBet


def _cfg(**kw) -> TelegramConfig:
    base = dict(bot_token="jeton", chat_id="PRINCIPAL", premium_chat_id="PREMIUM",
                critical_chat_id="CRITIQUE", min_minutes_to_kickoff=0,
                premium_hi_sports_exclus=("tennis",))
    base.update(kw)
    return TelegramConfig(**base)


class _Espion(TelegramAlerter):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.envois: list[str] = []

    def _send(self, text, chat_id, reply_markup=None):
        self.envois.append(chat_id)
        return True

    def canaux(self, odd, ev, sport, *, live=False):
        self.envois.clear()
        quand = datetime.now(timezone.utc) + timedelta(days=-1 if live else 2)
        bet = ValueBet(
            event_key=f"{quand:%Y%m%d%H%M}::a__vs__b", book=Book.UNIBET_BE,
            market=MarketType.H2H, outcome=Outcome("home"), odd_taken=odd,
            fair_prob=0.3, fair_odd=round(odd / (1 + ev / 100), 3), ev_pct=ev,
            kelly_stake_pct=1.0, detected_at=datetime.now(timezone.utc))
        self.send_value_bet(bet, sport=sport)
        return self.envois


@pytest.fixture(autouse=True)
def _sans_disque(monkeypatch):
    import src.alerter as al
    monkeypatch.setattr(al, "_load_played_keys", lambda: (set(), set()))
    monkeypatch.setattr(al, "_load_books_alert_off", lambda: set())


@pytest.fixture
def a():
    return _Espion(_cfg())


# ══ le trou qu'on ferme ════════════════════════════════════════════════
def test_le_tennis_exclu_du_premium_arrive_au_critique(a):
    """25 % d'EV en cote 5 : hier nulle part, aujourd'hui sur le critique."""
    assert a.canaux(5.00, 25.0, "tennis") == ["CRITIQUE"]


def test_une_cote_au_dessus_de_toute_bande_premium_arrive_au_critique(a):
    """Cote 8 : aucune bande premium ne la couvre, et 25 % < 35 %."""
    assert a.canaux(8.00, 25.0, "soccer") == ["CRITIQUE"]


def test_sous_le_seuil_de_la_voie_grosses_cotes_rien_ne_part(a):
    assert a.canaux(5.00, 15.0, "tennis") == []


# ══ ce qui ne doit pas bouger ══════════════════════════════════════════
def test_un_pari_ne_part_jamais_dans_les_deux(a):
    """Le garde-fou central : le critique reste une voie de débordement."""
    for odd, ev, sport in ((5.00, 40.0, "soccer"), (2.10, 50.0, "soccer"),
                           (5.00, 25.0, "soccer")):
        canaux = a.canaux(odd, ev, sport)
        assert canaux == ["PREMIUM"], f"cote {odd} EV {ev} {sport} -> {canaux}"


def test_la_bande_standard_ne_gagne_aucune_voie_critique(a):
    """Cote <= 4 : la nouvelle voie ne doit rien y changer. Sans canal
    premium pour masquer le résultat, 25 % d'EV en cote 2,10 reste sous le
    seuil critique de 35 % et ne part nulle part."""
    seul = _Espion(_cfg(premium_chat_id=None))
    assert seul.canaux(2.10, 25.0, "soccer") == []


def test_l_ancienne_voie_critique_marche_toujours(a):
    """EV >= 35 % sans limite de cote, y compris sous la bande premium."""
    assert a.canaux(1.20, 40.0, "soccer") == ["CRITIQUE"]


def test_le_live_ne_touche_jamais_le_critique(a):
    """Le critique est prématch uniquement ; la nouvelle voie n'y déroge pas."""
    assert a.canaux(5.00, 25.0, "tennis", live=True) == []


# ══ la borne, et le reglage ════════════════════════════════════════════
def test_la_borne_de_cote_est_stricte():
    """4,00 pile appartient à la bande premium standard : la voie grosses
    cotes commence AU-DESSUS, sinon elle empiéterait dessus."""
    seul = _Espion(_cfg(premium_chat_id=None))
    assert seul.canaux(4.00, 25.0, "soccer") == []
    assert seul.canaux(4.01, 25.0, "soccer") == ["CRITIQUE"]


def test_les_deux_reglages_sont_lus_dans_l_environnement(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "jeton")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "PRINCIPAL")
    monkeypatch.setenv("TELEGRAM_CRITICAL_HI_EV", "12.5")
    monkeypatch.setenv("TELEGRAM_CRITICAL_HI_MIN_ODD", "3.0")
    cfg = TelegramConfig.from_env()
    assert cfg is not None
    assert (cfg.critical_hi_min_ev, cfg.critical_hi_min_odd) == (12.5, 3.0)


def test_les_reglages_pilotent_vraiment_le_routage():
    """Un réglage qu'on peut lire mais qui ne change rien est un piège."""
    strict = _Espion(_cfg(premium_chat_id=None, critical_hi_min_ev=30.0))
    assert strict.canaux(5.00, 25.0, "soccer") == []
    large = _Espion(_cfg(premium_chat_id=None, critical_hi_min_odd=2.0))
    assert large.canaux(2.50, 25.0, "soccer") == ["CRITIQUE"]
