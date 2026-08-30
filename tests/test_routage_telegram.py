"""Le diagnostic de routage doit dire vrai, et ne rien envoyer par défaut.

Un outil qui se trompe sur le routage est pire que pas d'outil : il donne une
certitude fausse sur la question qu'on ne peut pas vérifier autrement. D'où
deux exigences, chacune tenue par un test qui casse si on l'enlève :

  * il REJOUE le routage réel (`send_value_bet`) au lieu de le réimplémenter —
    `test_le_tableau_suit_la_config` le prouve en changeant la config et en
    exigeant que le tableau change ;
  * il n'envoie RIEN sans `--envoyer`, et il restaure `_send` en sortant. Un
    `_send` espion laissé en place transformerait tout envoi ultérieur du
    processus en silence — exactement la panne muette que ce projet passe son
    temps à traquer.
"""
from __future__ import annotations

import pytest

import src.alerter as al
from scripts.routage_telegram import CAS, espionner, main, panne, tableau
from src.alerter import TelegramConfig


def _cfg(**kw) -> TelegramConfig:
    base = dict(bot_token="jeton", chat_id="PRINCIPAL", premium_chat_id="PREMIUM",
                critical_chat_id="CRITIQUE", maintenance_chat_id="MAINTENANCE",
                min_minutes_to_kickoff=0)
    base.update(kw)
    return TelegramConfig(**base)


@pytest.fixture(autouse=True)
def _sans_disque(monkeypatch):
    monkeypatch.setattr(al, "_load_played_keys", lambda: (set(), set()))
    monkeypatch.setattr(al, "_load_books_alert_off", lambda: set())


@pytest.fixture
def muet():
    return lambda *a, **k: None


# ══ ne rien envoyer par defaut ═════════════════════════════════════════
def test_sans_envoyer_aucun_message_ne_part(monkeypatch, muet):
    """Le garde-fou qui compte : un diagnostic ne doit pas pouvoir polluer les
    canaux qu'il inspecte."""
    reels: list[str] = []
    monkeypatch.setattr(al.TelegramAlerter, "_send",
                        lambda self, t, chat_id, reply_markup=None:
                        reels.append(chat_id) or True)
    tableau(_cfg(), envoyer=False, print_fn=muet)
    panne(_cfg(), envoyer=False)
    assert reels == []


def test_avec_envoyer_les_messages_partent_vraiment(monkeypatch, muet):
    reels: list[str] = []
    monkeypatch.setattr(al.TelegramAlerter, "_send",
                        lambda self, t, chat_id, reply_markup=None:
                        reels.append(chat_id) or True)
    tableau(_cfg(), envoyer=True, print_fn=muet)
    assert reels, "--envoyer doit atteindre le vrai _send"


def test_le_chat_vise_est_releve_meme_en_mode_reel(monkeypatch, muet):
    """Sinon `--envoyer` n'afficherait plus aucune destination."""
    monkeypatch.setattr(al.TelegramAlerter, "_send",
                        lambda self, t, chat_id, reply_markup=None: True)
    assert panne(_cfg(), envoyer=True)[0] == ["MAINTENANCE"]


def test_send_est_restaure_meme_sur_exception():
    """Un espion laissé en place rendrait muet tout envoi ultérieur."""
    avant = al.TelegramAlerter._send
    with pytest.raises(RuntimeError):
        with espionner([], envoyer=False):
            raise RuntimeError("boum")
    assert al.TelegramAlerter._send is avant


# ══ dire vrai ══════════════════════════════════════════════════════════
def test_le_tableau_suit_la_config(muet):
    """Preuve qu'il rejoue le routage au lieu de le décrire : sans canal
    premium, le pari de la bande standard doit basculer ailleurs."""
    i = CAS.index((2.10, 50.0, "soccer", "bande premium standard"))
    avec = tableau(_cfg(), envoyer=False, print_fn=muet)[i]
    sans = tableau(_cfg(premium_chat_id=None), envoyer=False, print_fn=muet)[i]
    assert avec == ["PREMIUM"]
    assert sans == ["CRITIQUE"]


def test_l_exclusion_tennis_est_visible(muet):
    """La bande longue fermée au tennis, et elle seule : le soccer au même
    couple cote/EV doit continuer d'aller au premium."""
    lignes = tableau(_cfg(premium_hi_sports_exclus=("tennis",)),
                     envoyer=False, print_fn=muet)
    assert lignes[CAS.index((5.00, 25.0, "soccer", "bande premium longue"))] == ["PREMIUM"]
    assert lignes[CAS.index((5.00, 25.0, "tennis", "bande longue, sport exclu"))] == []


def test_la_panne_part_sur_le_canal_maintenance(muet):
    assert panne(_cfg(), envoyer=False)[0] == ["MAINTENANCE"]


def test_sans_maintenance_la_panne_retombe_sur_le_critique(muet):
    assert panne(_cfg(maintenance_chat_id=None), envoyer=False)[0] == ["CRITIQUE"]


# ══ la ligne de commande ═══════════════════════════════════════════════
def test_sans_config_telegram_le_script_sort_en_erreur(monkeypatch, capsys):
    """Code 2, pas 0 : une config absente n'est pas un diagnostic réussi."""
    monkeypatch.setattr("scripts.routage_telegram.load_env_file", lambda p: 0)
    monkeypatch.setattr(TelegramConfig, "from_env", staticmethod(lambda: None))
    monkeypatch.setattr("sys.argv", ["routage_telegram"])
    assert main() == 2


def test_le_script_va_jusqu_au_bout_et_n_envoie_rien(monkeypatch, capsys):
    reels: list[str] = []
    monkeypatch.setattr(al.TelegramAlerter, "_send",
                        lambda self, t, chat_id, reply_markup=None:
                        reels.append(chat_id) or True)
    monkeypatch.setattr("scripts.routage_telegram.load_env_file", lambda p: 7)
    monkeypatch.setattr(TelegramConfig, "from_env", staticmethod(_cfg))
    monkeypatch.setattr("sys.argv", ["routage_telegram"])
    assert main() == 0
    sortie = capsys.readouterr().out
    assert "MAINTENANCE" in sortie and "AUCUN CANAL" in sortie
    assert reels == []
