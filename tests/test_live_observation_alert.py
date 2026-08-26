"""L'alerte LIVE OBSERVATION : ce qu'elle dit, et où elle ne va JAMAIS.

Aucun test ne touche le réseau. Ce qui est vérifié ici tient en trois
propriétés : le message porte tout ce qui permet de juger l'occasion à l'œil,
il ne porte AUCUN bouton, et il ne part jamais vers le canal prématch.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.alerter import (
    FRAICHEUR_SUSPECTE_SEC, TelegramConfig, format_live_observation,
    send_live_observation)
from src.live_value import Opportunite, Statut
from src.models import Book, MarketType

MAINTENANT = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)


def _opp(**kw) -> Opportunite:
    base = dict(
        detecte_a=MAINTENANT,
        event_key="202608261930::orebrosk__vs__varbergsbois",
        home="orebrosk", away="varbergsbois", market=MarketType.H2H,
        line=None, outcome="home", book=Book.UNIBET_BE, cote_preneur=4.00,
        fair_prob=0.49, fair_cote=2.04, ev_pct=96.1,
        statut=Statut.OBSERVEE_SCORE_INCONNU, kelly_pct=12.34,
        age_fair_sec=9.0, age_preneur_sec=0.4, delai_calcul_sec=0.25,
        feed_score="1:0", source_event_id_fair="1634601234",
        source_event_id_preneur="9001", minute_ecoulee=42.0)
    base.update(kw)
    return Opportunite(**base)


class _FauxAlerter:
    """Capture les appels à `_send` au lieu de parler à Telegram."""

    def __init__(self):
        self.envois = []

    def _send(self, text, chat_id, reply_markup=None):
        self.envois.append((text, chat_id, reply_markup))
        return True


def _cfg(**kw) -> TelegramConfig:
    base = dict(bot_token="jeton", chat_id="PREMATCH",
                surebet_chat_id="SUREBET_PREMATCH",
                live_surebet_chat_id="LIVE")
    base.update(kw)
    return TelegramConfig(**base)


# ══ contenu du message ═════════════════════════════════════════════════
def test_le_message_porte_TOUT_ce_qui_permet_de_juger():
    t = format_live_observation(_opp())
    for attendu in ("🧪", "LIVE OBSERVATION", "Score : 1-0", "AsianOdds",
                    "Marché", "Sélection", "Cote", "Fair", "EV",
                    "+96.1 %", "Âge fair", "Temps de détection",
                    "Orebrosk", "42 min"):
        assert attendu in t, f"{attendu!r} absent du message :\n{t}"


def test_le_score_vient_d_AsianOdds_et_porte_son_AGE():
    """« ⚽ Score : X-X » sans son âge laisserait croire qu'il est actuel.
    Une fair de 90 s peut porter un état de jeu révolu — c'est la seule chose
    qui permet au lecteur de s'en apercevoir."""
    t = format_live_observation(_opp(feed_score="2:1", age_fair_sec=87.0))
    assert "Score : 2-1" in t
    assert "il y a 87.0 s" in t
    # La minute rejoint le score : les deux disent où en est le match.
    assert "🕐 42 min" in t


def test_un_score_absent_s_ecrit_N_A_et_pas_0_0():
    """Inventer « 0-0 » ferait lire un match vierge là où on ne sait rien."""
    t = format_live_observation(_opp(feed_score=None))
    assert "Score : N/A" in t
    assert "0-0" not in t


def test_une_duree_inconnue_s_ecrit_N_A_et_pas_zero():
    t = format_live_observation(_opp(age_preneur_sec=None,
                                     delai_calcul_sec=None))
    assert t.count("N/A") >= 2
    assert "0.0 s" not in t


def test_un_marche_partiel_est_annonce_dans_le_message():
    t = format_live_observation(_opp(partiel=True,
                                     issues_manquantes=("away", "draw")))
    assert "Marché partiel" in t
    assert "away, draw" in t


def test_un_marche_complet_n_annonce_RIEN(): 
    """Contre-épreuve : sans elle, un avertissement permanent passerait le
    test précédent et deviendrait invisible à force d'être là."""
    assert "Marché partiel" not in format_live_observation(_opp())


def test_une_fraicheur_limite_est_signalee():
    t = format_live_observation(_opp(age_fair_sec=FRAICHEUR_SUSPECTE_SEC + 1))
    assert "Fraîcheur limite" in t and "fair AsianOdds" in t
    assert "Fraîcheur limite" not in format_live_observation(
        _opp(age_fair_sec=FRAICHEUR_SUSPECTE_SEC - 1))


@pytest.mark.parametrize("ev", [10.1, 100.0, 500.0, 5000.0])
def test_aucune_EV_n_est_tronquee_ni_plafonnee_dans_le_message(ev):
    """Le formateur n'est pas un endroit où réintroduire un plafond."""
    assert f"{ev:+.1f} %" in format_live_observation(_opp(ev_pct=ev))


# ══ où le message va, et où il ne va pas ═══════════════════════════════
def test_AUCUN_bouton_n_est_jamais_attache():
    """Pas de bouton = aucune action bookmaker possible depuis l'alerte.
    C'est une contrainte de sécurité, pas une préférence d'affichage."""
    faux = _FauxAlerter()
    send_live_observation([_opp()], _cfg(), alerter=faux)
    assert faux.envois and all(m is None for _, _, m in faux.envois)


def test_l_alerte_part_vers_le_canal_LIVE_et_lui_seul():
    faux = _FauxAlerter()
    send_live_observation([_opp(), _opp()], _cfg(), alerter=faux)
    assert {c for _, c, _ in faux.envois} == {"LIVE"}


def test_SANS_canal_live_dedie_on_N_ENVOIE_RIEN(tmp_path):
    """LE test de sécurité de ce commit.

    `effective_live_surebet_chat_id` retombe SILENCIEUSEMENT sur le canal des
    surebets prématch quand `TELEGRAM_LIVE_SUREBET_CHAT_ID` est absent. Sans
    ce refus, une variable d'environnement oubliée déverserait les
    observations LIVE dans le canal prématch — précisément ce qui est
    interdit. On préfère ne rien envoyer.
    """
    faux = _FauxAlerter()
    dit = []
    n = send_live_observation([_opp()], _cfg(live_surebet_chat_id=None),
                              alerter=faux, log=dit.append)
    assert n == 0
    assert faux.envois == [], "un message est parti vers le canal prématch"
    assert any("LIVE_SUREBET_CHAT_ID" in m for m in dit)


def test_le_repli_du_projet_pointe_BIEN_vers_le_prematch():
    """Ce test justifie le précédent : il constate que le repli existe.
    Si un jour `effective_live_surebet_chat_id` cesse de retomber sur le
    prématch, ce test tombe et le refus ci-dessus pourra être reconsidéré."""
    cfg = _cfg(live_surebet_chat_id=None)
    assert cfg.effective_live_surebet_chat_id == "SUREBET_PREMATCH"


def test_sans_jeton_aucun_envoi_et_aucune_erreur():
    faux = _FauxAlerter()
    assert send_live_observation([_opp()], None, alerter=faux, log=lambda _: None) == 0
    assert send_live_observation([_opp()], _cfg(bot_token=""), alerter=faux,
                                 log=lambda _: None) == 0
    assert faux.envois == []


def test_aucune_opportunite_aucun_message():
    faux = _FauxAlerter()
    assert send_live_observation([], _cfg(), alerter=faux) == 0
    assert faux.envois == []


# ══ le message allégé garde ce qui protège ═════════════════════════════
def test_le_statut_NORMAL_ne_pollue_pas_le_message():
    """`OBSERVEE_SCORE_INCONNU` est l'état de TOUTES les alertes de cette
    phase. L'afficher à chaque fois était du bruit constant : une ligne qui
    ne varie jamais ne porte aucune information."""
    assert "statut" not in format_live_observation(_opp())


def test_un_REJET_reste_VISIBLE_dans_le_message():
    """Un rejet, lui, doit se voir : c'est la seule chose qui dit pourquoi
    l'alerte ne vaut rien. Sans ça, `--envoyer-rejets` enverrait des alertes
    indiscernables des bonnes."""
    t = format_live_observation(_opp(statut=Statut.REJET_FAIR_PERIMEE,
                                     motif="3329s > 60s"))
    assert "REJET_FAIR_PERIMEE" in t
    # Le motif contient un « > » : il DOIT sortir échappé, sinon Telegram
    # refuse le message entier pour HTML invalide et l'alerte se perd.
    assert "3329s &gt; 60s" in t
    assert "3329s > 60s" not in t


def test_le_message_allege_garde_les_DEUX_avertissements():
    """Kelly et les identifiants sortent du message — ils restent au journal.
    Les avertissements, eux, ne sortent jamais : ils disent que la donnée
    elle-même est douteuse."""
    t = format_live_observation(_opp(partiel=True, issues_manquantes=("away",),
                                     age_fair_sec=FRAICHEUR_SUSPECTE_SEC + 1))
    assert "Marché partiel" in t and "away" in t
    assert "Fraîcheur limite" in t


def test_une_minute_inconnue_n_ecrit_pas_d_horloge_vide():
    t = format_live_observation(_opp(minute_ecoulee=None))
    assert "🕐" not in t
    assert "Score : 1-0" in t
