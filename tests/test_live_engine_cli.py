"""Le lanceur LIVE : ses chemins Telegram ne doivent jamais planter.

Aucun test ici ne touche le réseau ni Telegram : on vérifie que chaque
drapeau qui parle à Telegram passe bien par la mise en place de la
configuration, et sort proprement quand elle manque.
"""
from __future__ import annotations

import runpy
import sys

import pytest

import scripts.live_engine as le

#: TOUS les drapeaux qui déclenchent un chemin Telegram. Un drapeau ajouté
#: au lanceur sans être ajouté ici échappe à la vérification — et c'est
#: exactement comme j'ai introduit deux plantages de suite.
DRAPEAUX = ["--telegram", "--telegram-blanc", "--test-telegram"]


def test_les_drapeaux_telegram_du_lanceur_sont_TOUS_couverts_ici():
    """Le garde-fou du garde-fou : si le lanceur gagne un drapeau Telegram,
    ce test tombe tant qu'il n'est pas ajouté à `DRAPEAUX`."""
    import argparse
    import contextlib
    import io

    aide = io.StringIO()
    p = argparse.ArgumentParser()
    with contextlib.redirect_stdout(aide), pytest.raises(SystemExit):
        sys.argv = ["x", "--help"]
        le.main()
    texte = aide.getvalue()
    trouves = {m for m in ("--telegram", "--telegram-blanc", "--test-telegram")
               if m in texte}
    # Tout drapeau contenant "telegram" doit être dans DRAPEAUX.
    import re
    dans_aide = set(re.findall(r"--[a-z-]*telegram[a-z-]*", texte))
    assert dans_aide <= set(DRAPEAUX), (
        f"drapeau Telegram non couvert par ce test : {dans_aide - set(DRAPEAUX)}")


@pytest.mark.parametrize("drapeau", DRAPEAUX)
def test_tout_drapeau_telegram_passe_par_la_mise_en_place(drapeau, monkeypatch):
    """Sans TELEGRAM_BOT_TOKEN ni TELEGRAM_CHAT_ID, `from_env()` rend None.

    Chaque chemin doit alors SORTIR proprement en code 2 — pas planter sur un
    `cfg` à None. J'ai introduit ce plantage deux fois : d'abord en lisant
    `cfg.live_surebet_chat_id`, puis en appelant `alerte[1]`. Les deux fois,
    le drapeau neuf ne passait pas par la mise en place.
    """
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(sys, "argv", ["live_engine", drapeau, "--minutes", "0"])
    assert le.main() == 2


def test_le_message_de_test_dit_qu_il_est_un_test():
    """Un message de vérification qui ressemblerait à une vraie occasion
    serait pire qu'inutile : il finirait par être lu comme un signal."""
    from datetime import datetime, timezone
    from src.alerter import format_live_observation
    from src.live_value import Opportunite, Statut
    from src.models import Book, MarketType

    o = Opportunite(
        detecte_a=datetime.now(timezone.utc),
        event_key="TEST::ceci_est_un_test__vs__aucun_pari",
        home="CECI EST UN TEST", away="AUCUN PARI", market=MarketType.H2H,
        line=None, outcome="home", book=Book.UNIBET_BE, cote_preneur=2.00,
        fair_prob=0.60, fair_cote=1.67, ev_pct=20.0,
        statut=Statut.OBSERVEE_SCORE_INCONNU,
        motif="message de vérification du canal — données inventées",
        kelly_pct=20.0)
    t = format_live_observation(o).lower()
    assert "test" in t and "vérification" in t
