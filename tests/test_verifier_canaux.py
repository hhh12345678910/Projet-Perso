"""Le controle post-installation doit DETECTER, pas rassurer.

Un verificateur qui rend GO quoi qu'il arrive est pire que pas de
verificateur : il transforme une panne en certitude. Chaque test ci-dessous
casse une chose precise et exige un NO-GO avec le bon motif.

`test_un_controle_impossible_n_est_pas_un_echec` tient l'autre bord : sur
une machine sans systemd, l'etat du service est INDETERMINE, pas mauvais.
Les confondre ferait passer un diagnostic pour une panne.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

import scripts.verifier_canaux as v
from src.alerter import TelegramConfig
from src.channels import depuis_config, installer
from src.storage import Storage

CFG = dict(bot_token="jeton", chat_id="PRINCIPAL", premium_chat_id="PREMIUM",
           critical_chat_id="CRITIQUE", premium_hi_sports_exclus=("tennis",))


@pytest.fixture
def installe(tmp_path, monkeypatch, capsys):
    """Une base avec les canaux installes, et le script pointe dessus."""
    chemin = str(tmp_path / "t.db")
    st = Storage(chemin)
    cfg = TelegramConfig(**CFG)
    installer(st, depuis_config(cfg), print_fn=lambda _s: None)
    monkeypatch.setattr(v, "ScanConfig", lambda: type("C", (), {"db_path": chemin})())
    monkeypatch.setattr(v, "load_env_file", lambda p: 0)
    monkeypatch.setattr(TelegramConfig, "from_env", staticmethod(lambda: cfg))
    monkeypatch.setattr(v, "_systemctl", lambda *a: "")   # pas de systemd
    monkeypatch.setattr("sys.argv", ["verifier_canaux"])
    return chemin


def _lancer(capsys) -> tuple[int, str]:
    code = v.main()
    return code, capsys.readouterr().out


def _sql(chemin, *requetes):
    c = sqlite3.connect(chemin)
    for r in requetes:
        c.execute(r)
    c.commit()
    c.close()


def _notifier(chemin, chat_id, n=1, ev=10.0):
    c = sqlite3.connect(chemin)
    for i in range(n):
        c.execute(
            "INSERT INTO notified_value_bets(event_key, book, market, outcome_label,"
            " line, ev_pct, notified_at, chat_id) VALUES (?,?,?,?,?,?,?,?)",
            ("202609011800::a__vs__b", "unibet_be", "h2h", "home", None, ev + i,
             datetime.now(timezone.utc).isoformat(), chat_id))
    c.commit()
    c.close()


# ══ le cas sain ════════════════════════════════════════════════════════
def test_une_installation_conforme_rend_GO(installe, capsys):
    code, sortie = _lancer(capsys)
    assert code == 0
    assert "GO —" in sortie and "NO-GO" not in sortie
    assert "0 divergence" in sortie


def test_un_controle_impossible_n_est_pas_un_echec(installe, capsys):
    """Sans systemd, l'etat du service est INDETERMINE — pas un NO-GO."""
    code, sortie = _lancer(capsys)
    assert code == 0
    assert "INDETERMINE" in sortie
    assert "3 verification(s) sur 4" in sortie


# ══ ce qu'il doit attraper ═════════════════════════════════════════════
def test_un_seuil_modifie_dans_la_base(installe, capsys):
    _sql(installe, "UPDATE channel_rules SET ev_min=6.0 WHERE ev_min=5.0")
    code, sortie = _lancer(capsys)
    assert code == 1
    assert "PRINCIPAL : la base ne correspond pas" in sortie
    assert "divergence(s) de routage" in sortie


def test_un_canal_desactive_dans_la_base(installe, capsys):
    _sql(installe, "UPDATE channels SET actif=0 WHERE nom='PREMIUM'")
    code, sortie = _lancer(capsys)
    assert code == 1
    assert "PREMIUM : la base ne correspond pas" in sortie


def test_un_canal_supprime_de_la_base(installe, capsys):
    _sql(installe, "DELETE FROM channels WHERE nom='CRITIQUE'")
    code, sortie = _lancer(capsys)
    assert code == 1
    assert "CRITIQUE absent de la base" in sortie


def test_un_marquage_global_apres_le_redemarrage(installe, capsys):
    """La signature du bug le plus couteux : `main.py` marquerait encore
    globalement en plus du marquage par canal, et cette ligne NULL
    bloquerait ensuite TOUS les canaux — elle compte pour chacun."""
    _notifier(installe, None)
    code, sortie = _lancer(capsys)
    assert code == 1
    assert "marquage(s) GLOBAUX" in sortie


def test_un_couple_opportunite_canal_au_dela_du_plafond(installe, capsys):
    _notifier(installe, "PRINCIPAL", n=3)
    code, sortie = _lancer(capsys)
    assert code == 1
    assert "au-dela du plafond" in sortie


def test_le_plafond_atteint_pile_ne_declenche_rien(installe, capsys):
    """Deux alertes par canal, c'est le reglage — pas un doublon."""
    _notifier(installe, "PRINCIPAL", n=2)
    code, sortie = _lancer(capsys)
    assert code == 0, sortie


def test_deux_canaux_pour_la_meme_opportunite_ne_sont_pas_un_doublon(installe, capsys):
    """Le comportement voulu : un pari peut partir sur plusieurs canaux."""
    _notifier(installe, "PRINCIPAL")
    _notifier(installe, "PREMIUM")
    code, sortie = _lancer(capsys)
    assert code == 0, sortie
