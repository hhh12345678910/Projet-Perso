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
    serait pire qu'inutile : il finirait par être lu comme un signal.

    Ce test lit L'OBJET QUE LE LANCEUR ENVOIE VRAIMENT. Ma première version
    en construisait un sosie dans le test — elle passait donc même après
    avoir remplacé les noms bidons par « Örebro SK vs Varbergs BoIS » dans le
    lanceur. Elle ne protégeait rien.
    """
    from src.alerter import format_live_observation

    t = format_live_observation(le.opportunite_de_test()).lower()
    # Le marqueur doit vivre dans les NOMS D'ÉQUIPE : seul endroit du message
    # qui s'affiche toujours, quel que soit le statut. Le `motif` ne convient
    # pas — depuis l'allègement du format, il ne sort que pour un REJET.
    assert "ceci est un test" in t
    assert "aucun pari" in t


# ══ le lanceur doit aller JUSQU'AU BOUT ════════════════════════════════
#
# Trois plantages de suite ont echappe aux tests parce que ceux-ci
# s'arretaient avant le rapport final : deux sur un `cfg` a None, et un sur
# `Statut` devenu une locale de `main()` a cause d'un import place dans un
# bloc. Ce dernier plantait le rapport MEME QUAND LE BLOC NE S'EXECUTAIT PAS,
# apres cinq minutes de collecte. Il fallait un test qui traverse tout.

def _monde(tmp_path):
    """Une base et un payload Kambi minimaux, sans reseau."""
    from datetime import datetime, timedelta, timezone
    from src.asianodds_live import LiveRow
    from src.matcher import event_key
    from src.models import MarketType
    from src.storage import Storage

    now = datetime.now(timezone.utc)
    ko = now - timedelta(minutes=40)
    cle = event_key("Alfa FC", "Beta SK", ko)
    db = Storage(tmp_path / "e.db")
    db.upsert_events([(cle, "soccer", "T", "alfafc", "betask", ko.isoformat())])
    obs = now - timedelta(seconds=4)
    db.upsert_live_state([LiveRow(
        event_key=cle, market=MarketType.H2H, outcome_label=l, line=None,
        odd=c, observed_at=obs, home_score=1, away_score=0, feed_score="1:0",
        igm=40, league="T", source_event_id="1634601234",
        source_inverse=False, matched_at=obs).as_upsert_row(obs)
        for l, c in zip(("home", "draw", "away"), (2.00, 3.60, 4.00))])
    payload = {"events": [{
        "event": {"id": 1001, "homeName": "Alfa FC", "awayName": "Beta SK",
                  "start": ko.strftime("%Y-%m-%dT%H:%M:%SZ"), "group": "T"},
        "betOffers": [{"betOfferType": {"id": 2}, "outcomes": [
            {"type": "OT_ONE", "odds": 6000},
            {"type": "OT_CROSS", "odds": 9000},
            {"type": "OT_TWO", "odds": 12000}]}]}]}
    return str(tmp_path / "e.db"), payload


class _FauxScraper:
    def __init__(self, payload):
        self._p = payload

    def fetch_listview(self, sport="soccer", path_suffix=""):
        return self._p

    def close(self):
        pass


@pytest.fixture
def lanceur(tmp_path, monkeypatch):
    import src.unibet_live as ul
    chemin, payload = _monde(tmp_path)
    vrai = ul.UnibetLive
    monkeypatch.setattr(
        le, "UnibetLive",
        lambda sport="soccer", **kw: vrai(sport, scraper=_FauxScraper(payload)))
    return chemin


def test_le_lanceur_va_jusqu_au_rapport_final(lanceur, monkeypatch, capsys):
    """`main()` doit rendre 0 ET imprimer son entonnoir.

    C'est ce test qui aurait attrapé l'UnboundLocalError sur `Statut` :
    l'erreur ne se déclenchait qu'à la toute fin, après la boucle, donc après
    cinq minutes de collecte réelle sur la VM.
    """
    monkeypatch.setattr(
        sys, "argv",
        ["live_engine", "--minutes", "0.04", "--periode", "1", "--db", lanceur])
    assert le.main() == 0
    sortie = capsys.readouterr().out
    for attendu in ("ENTONNOIR", "TRANCHES D'EV", "OCCASIONS", "TOP EV",
                    "TOP KELLY"):
        assert attendu in sortie, f"{attendu!r} absent du rapport"


def test_le_lanceur_va_jusqu_au_bout_AVEC_telegram_a_blanc(
        lanceur, monkeypatch, capsys):
    """Le chemin Telegram traverse le même rapport final."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "jeton")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "PREMATCH")
    monkeypatch.setenv("TELEGRAM_LIVE_SUREBET_CHAT_ID", "LIVE")
    monkeypatch.setattr(
        sys, "argv",
        ["live_engine", "--minutes", "0.04", "--periode", "1",
         "--db", lanceur, "--telegram-blanc"])
    assert le.main() == 0
    sortie = capsys.readouterr().out
    assert "ENTONNOIR" in sortie
    assert "LIVE OBSERVATION" in sortie, "aucun message n'a été formaté"
    assert "TELEGRAM :" in sortie


def test_aucun_import_de_main_ne_masque_un_nom_du_module():
    """La cause racine, interdite structurellement.

    Un `from … import Statut` placé dans un bloc de `main()` fait de `Statut`
    une LOCALE pour toute la fonction — y compris pour le code qui s'exécute
    quand ce bloc ne s'exécute pas. Le rapport final plantait donc toujours.
    """
    import ast
    import pathlib

    arbre = ast.parse(pathlib.Path("scripts/live_engine.py").read_text())
    fn = next(n for n in arbre.body
              if isinstance(n, ast.FunctionDef) and n.name == "main")
    locaux, globaux = set(), set()
    for n in ast.walk(fn):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            locaux.update(a.asname or a.name.split(".")[0] for a in n.names)
    for n in arbre.body:
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            globaux.update(a.asname or a.name.split(".")[0] for a in n.names)
    assert not (locaux & globaux), (
        f"import local masquant un nom du module : {sorted(locaux & globaux)}")
