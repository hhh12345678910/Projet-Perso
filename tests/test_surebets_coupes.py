"""Surebets coupés — calcul ET diffusion, réactivables d'un réglage.

Demande du 21/08 : cesser de diffuser les surebets sur les deux canaux
Telegram (prématch et live) ET cesser de les calculer, en gardant le système
intact pour pouvoir le remettre en service à tout moment.

Trois choses sont vérifiées ici, et la troisième est la plus importante :

1. par défaut, le calcul ne part pas ;
2. `SCAN_SUREBETS=1` le remet — une coupure qu'on ne sait pas annuler est une
   suppression déguisée ;
3. rien n'a été retiré : module, table, réglages Telegram et commande manuelle
   sont toujours là.

⚠️ Le piège que ces tests gardent : on ne coupe PAS en vidant
`TELEGRAM_SUREBET_CHAT_ID`. `effective_surebet_chat_id` retombe alors sur le
canal PRINCIPAL, et les surebets iraient le polluer au lieu de disparaître.
"""
from __future__ import annotations

import importlib
import os

import pytest

from src.config import ScanConfig


@pytest.fixture(autouse=True)
def _env_propre(monkeypatch):
    monkeypatch.delenv("SCAN_SUREBETS", raising=False)
    yield


def test_par_defaut_les_surebets_sont_coupes():
    assert ScanConfig().scan_surebets is False


def test_la_reactivation_fonctionne(monkeypatch):
    """Une coupure qu'on ne sait pas annuler est une suppression déguisée."""
    monkeypatch.setenv("SCAN_SUREBETS", "1")
    assert ScanConfig().scan_surebets is True
    monkeypatch.setenv("SCAN_SUREBETS", "0")
    assert ScanConfig().scan_surebets is False


def test_le_reglage_est_lu_a_la_CONSTRUCTION_pas_a_l_import(monkeypatch):
    """Le §19.11 : une valeur figée à l'import rendrait `.env` sans effet, et
    rien ne dirait pourquoi. C'est exactement le piège de `provider_for`."""
    import src.config as cfgmod
    monkeypatch.setenv("SCAN_SUREBETS", "1")
    assert cfgmod.ScanConfig().scan_surebets is True   # sans re-import


def test_le_systeme_est_intact():
    """« Garder le système pour pouvoir le réactiver » : rien n'est supprimé."""
    from src.surebet import find_surebets                       # le moteur
    from src.main import canonicalize_for_surebets, scan_surebets  # + la commande
    from src.alerter import TelegramConfig, send_surebet_alerts    # + la diffusion
    assert callable(find_surebets) and callable(canonicalize_for_surebets)
    assert callable(scan_surebets) and callable(send_surebet_alerts)
    # Les réglages Telegram survivent : les rendre inaccessibles obligerait à
    # les redécouvrir le jour de la réactivation.
    cfg = TelegramConfig(bot_token="x", chat_id="y")
    for attr in ("surebet_chat_id", "live_surebet_chat_id",
                 "min_surebet_margin_pct", "surebet_dedup",
                 "surebet_max_alerts", "min_critical_surebet_pct"):
        assert hasattr(cfg, attr), attr


def test_la_table_de_dedup_survit(tmp_path):
    from src.storage import Storage

    """`notified_surebets` reste créée : la vider ferait re-alerter tout
    l'historique le jour de la réactivation."""
    st = Storage(str(tmp_path / "t.db"))
    with st._conn() as c:
        noms = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "notified_surebets" in noms


def test_vider_le_chat_id_ne_coupe_PAS(monkeypatch):
    """⚠️ Le piège. Vider l'identifiant fait RETOMBER sur le canal principal —
    les surebets y arriveraient au lieu de disparaître. C'est pour ça que la
    coupure passe par SCAN_SUREBETS et pas par les identifiants."""
    from src.alerter import TelegramConfig

    cfg = TelegramConfig(bot_token="x", chat_id="-100PRINCIPAL",
                         surebet_chat_id=None, live_surebet_chat_id=None)
    assert cfg.effective_surebet_chat_id == "-100PRINCIPAL"
    assert cfg.effective_live_surebet_chat_id == "-100PRINCIPAL"


def test_les_middles_ne_sont_pas_touches():
    """La demande portait sur les surebets seuls. Les middles continuent."""
    from src.middle import find_middles
    assert callable(find_middles)
    assert not hasattr(ScanConfig(), "scan_middles"), \
        "aucun interrupteur n'a été posé sur les middles — ce n'était pas demandé"
