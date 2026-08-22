"""`alert-test-system` — la commande qui ferme le §20.6.

La vérification de bout en bout de l'alerte « softbook muet » manquait depuis
le 18/08, et pour une raison bête : `alert-test` couvre les canaux value,
surebet et CLV, **jamais les alertes SYSTÈME**. `send_system_alert` n'avait
donc aucune commande qui la déclenche.

⚠️ Ce qui est vérifié ici, c'est que la commande emprunte le VRAI chemin —
`_book_health` décide, `send_system_alert` livre. Une commande qui fabriquerait
son propre message prouverait qu'on sait envoyer un message, pas que l'alerte
fonctionne (§17.7 : une sonde qui recalcule autre chose que la production ment).
"""
from __future__ import annotations

import pytest
from typer.testing import CliRunner

from src.main import app

runner = CliRunner()


def test_sans_config_telegram_elle_le_dit_et_echoue(monkeypatch):
    """L'échec du §20.10, mais explicite : une config vide doit produire un
    message clair et un code de sortie non nul, pas un silence."""
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        monkeypatch.delenv(var, raising=False)
    r = runner.invoke(app, ["alert-test-system"])
    assert r.exit_code != 0
    assert "§20.10" in r.output


def test_elle_passe_par_le_vrai_book_health(monkeypatch):
    """Le cœur : c'est `_book_health` qui doit décider d'alerter, et
    `send_system_alert` qui doit livrer. On espionne le second."""
    from src import main as m

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100PRINCIPAL")
    envois: list[str] = []
    monkeypatch.setattr(m, "send_system_alert",
                        lambda cfg, text, **kw: envois.append(text) or True)

    r = runner.invoke(app, ["alert-test-system", "--book", "montest"])
    assert r.exit_code == 0, r.output
    # Deux messages : la panne, puis le retour.
    assert len(envois) == 2, envois
    assert "montest muet" in envois[0]
    assert "de retour" in envois[1]
    # Et le message porte la durée réelle, pas un texte figé.
    assert "cycles" in envois[0]


def test_elle_ne_laisse_aucune_trace_dans_l_etat_global(monkeypatch):
    """Le daemon garde ses compteurs dans des globales de module. Une commande
    de test qui les laisserait sales fausserait la surveillance réelle."""
    from src import main as m

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100PRINCIPAL")
    monkeypatch.setattr(m, "send_system_alert", lambda *a, **k: True)

    avant = (dict(m._BOOK_FAILS), set(m._BOOK_SEEN),
             dict(m._BOOK_DOWN_SINCE), set(m._BOOK_ALERTED))
    runner.invoke(app, ["alert-test-system", "--book", "montest"])
    apres = (dict(m._BOOK_FAILS), set(m._BOOK_SEEN),
             dict(m._BOOK_DOWN_SINCE), set(m._BOOK_ALERTED))
    assert avant == apres, "la commande a pollué l'état de surveillance"


def test_l_indice_du_pont_apparait_pour_les_books_a_onglet(monkeypatch):
    """Les trois books à pont navigateur sont la cause la plus fréquente et la
    seule que l'utilisateur corrige en dix secondes."""
    from src import main as m

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-100PRINCIPAL")
    envois: list[str] = []
    monkeypatch.setattr(m, "send_system_alert",
                        lambda cfg, text, **kw: envois.append(text) or True)

    runner.invoke(app, ["alert-test-system", "--book", "betano_be"])
    assert "onglet" in envois[0]

    envois.clear()
    runner.invoke(app, ["alert-test-system", "--book", "elitesports"])
    assert "onglet" not in envois[0], "EliteSports n'a pas de pont navigateur"
