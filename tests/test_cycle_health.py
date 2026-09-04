"""L'alerte de lenteur — celle qui n'existait pas.

`_pinnacle_health` et `_book_health` surveillent tous deux l'ABSENCE de
données. Pendant un gel de trois minutes, chaque book finit par répondre, en
retard : aucun n'est « muet », et rien ne se déclenche. Mesuré sur 10 192
cycles, ces gels coûtent 6,3 minutes de cécité par jour — sans détection ET
sans capture de clôture — sans qu'aucune alerte ne parte jamais.

Le piège de cette alerte est la fenêtre de silence pendant la purge. Sans
elle, elle partirait chaque nuit et on apprendrait à l'ignorer. Mais si la
fenêtre arrêtait le COMPTEUR au lieu du seul ENVOI, une panne réelle commencée
pendant la purge serait effacée par elle. C'est le test central de ce fichier.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import src.main as m


@pytest.fixture(autouse=True)
def _remise_a_zero():
    m._CYCLE_SLOW.update(suite=0, depuis=None, alerte=False, pire=0.0, perdu=0.0)
    yield
    m._CYCLE_SLOW.update(suite=0, depuis=None, alerte=False, pire=0.0, perdu=0.0)


@pytest.fixture
def envois(monkeypatch):
    recus: list[str] = []
    monkeypatch.setattr(m, "send_system_alert",
                        lambda cfg, texte, **kw: recus.append(texte) or True)
    return recus


def _t(h: int, mi: int = 0) -> datetime:
    return datetime(2026, 9, 3, h, mi, tzinfo=timezone.utc)


# ── Le déclenchement ───────────────────────────────────────────────────────

def test_un_seul_cycle_lent_n_alerte_pas(envois):
    """Un cycle isolé arrive — un book qui hoquette. Ce n'est pas un état."""
    m._cycle_health(200.0, None, quand=_t(12))
    assert envois == []


def test_deux_cycles_lents_d_affilee_alertent(envois):
    m._cycle_health(200.0, None, quand=_t(12, 0))
    m._cycle_health(150.0, None, quand=_t(12, 3))
    assert len(envois) == 1
    assert "Cycles ralentis" in envois[0]
    assert "200" in envois[0], "le pire cycle doit figurer dans le message"


def test_l_alerte_ne_part_qu_une_fois(envois):
    for i in range(6):
        m._cycle_health(200.0, None, quand=_t(12, i))
    assert len(envois) == 1, "une alerte par épisode, pas par cycle"


def test_un_cycle_normal_entre_deux_lents_remet_le_compteur(envois):
    m._cycle_health(200.0, None, quand=_t(12, 0))
    m._cycle_health(28.0, None, quand=_t(12, 1))     # retour à la normale
    m._cycle_health(200.0, None, quand=_t(12, 2))
    assert envois == [], "deux cycles lents SÉPARÉS ne font pas un épisode"


def test_le_retour_a_la_normale_est_annonce(envois):
    m._cycle_health(200.0, None, quand=_t(12, 0))
    m._cycle_health(200.0, None, quand=_t(12, 3))
    m._cycle_health(28.0, None, quand=_t(12, 6))
    assert len(envois) == 2
    assert "revenus à la normale" in envois[1]
    assert "clôtures" in envois[1], (
        "le message doit dire ce que l'épisode a pu coûter")


def test_pas_de_retour_a_la_normale_sans_alerte_prealable(envois):
    """Un cycle lent isolé puis la normale ne doit RIEN envoyer."""
    m._cycle_health(200.0, None, quand=_t(12, 0))
    m._cycle_health(28.0, None, quand=_t(12, 1))
    assert envois == []


# ── La fenêtre de silence : le cœur du risque ──────────────────────────────

def test_la_purge_ne_declenche_pas_l_alerte(envois):
    """04:00 UTC : documenté, assumé (§18.4). Une alerte par nuit est une
    alerte qu'on apprend à ignorer."""
    for i in range(5):
        m._cycle_health(250.0, None, quand=_t(4, i))
    assert envois == []


def test_le_compteur_avance_quand_meme_pendant_la_fenetre(envois):
    """LE TEST CENTRAL. Si la fenêtre arrêtait le compteur au lieu du seul
    envoi, une panne commencée à 04:30 et durant jusqu'à 06:00 serait effacée
    par la purge — exactement la panne qu'on veut voir."""
    for i in range(4):
        m._cycle_health(250.0, None, quand=_t(4, 30 + i))
    assert envois == [], "silence attendu pendant la fenêtre"
    assert m._CYCLE_SLOW["suite"] == 4, "le compteur DOIT avoir avancé"
    # sortie de fenêtre, toujours lent → l'alerte part immédiatement
    m._cycle_health(250.0, None, quand=_t(6, 0))
    assert len(envois) == 1
    assert "5 cycles" in envois[0], "les cycles de la fenêtre doivent compter"


def test_hors_fenetre_l_alerte_part_normalement(envois):
    """07 h porte 19 % des gels et reste inexpliqué : surtout pas silencé."""
    m._cycle_health(200.0, None, quand=_t(7, 0))
    m._cycle_health(200.0, None, quand=_t(7, 2))
    assert len(envois) == 1


# ── Le parsing de la fenêtre ───────────────────────────────────────────────

@pytest.mark.parametrize("heure,minute,attendu", [
    (3, 44, False), (3, 45, True), (4, 30, True),
    (5, 29, True), (5, 30, False), (12, 0, False),
])
def test_les_bornes_de_la_fenetre(heure, minute, attendu):
    assert m._dans_fenetre(_t(heure, minute), "03:45-05:30") is attendu


@pytest.mark.parametrize("heure,minute,attendu", [
    (23, 15, False),   # avant le début
    (23, 30, True),    # borne de début, incluse
    (23, 45, True),
    (0, 15, True),     # après minuit, toujours dedans
    (0, 30, False),    # borne de fin, EXCLUE
    (2, 0, False),
])
def test_une_fenetre_a_cheval_sur_minuit(heure, minute, attendu):
    """La première version de ce test attendait « dedans » à 00:45, qui est
    APRÈS la borne de fin de 00:30. C'était le test qui avait tort."""
    assert m._dans_fenetre(_t(heure, minute), "23:30-00:30") is attendu


@pytest.mark.parametrize("reglage", ["", "   ", "n'importe quoi", "04:00",
                                     "04:00-", "aa:bb-cc:dd"])
def test_un_reglage_illisible_ne_silence_rien(reglage):
    """Ni muette ni bavarde : un réglage cassé ne doit pas suspendre l'alerte
    — le défaut sûr est de la laisser parler."""
    assert m._dans_fenetre(_t(4, 0), reglage) is False
