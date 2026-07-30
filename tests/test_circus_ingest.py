"""Serveur d'ingestion — refus d'un push Circus mal routé.

Un onglet resté ouvert sur une ancienne version du userscript a écrit le tennis
dans soccer.json en production. Le daemon écarte déjà les ligues étrangères,
mais trop tard : le bon fichier a été écrasé et le book disparaît jusqu'au
cycle suivant. La barrière utile est ici, avant l'écriture.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "scripts" / "betano_ingest_server.py"
_spec = importlib.util.spec_from_file_location("betano_ingest_server", _SRC)
server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(server)

circus_sport_mismatch = server.circus_sport_mismatch


def block(*sport_ids: int) -> dict:
    return {"Leagues": [{"SportId": s, "Events": []} for s in sport_ids]}


def test_matching_sport_is_accepted():
    assert circus_sport_mismatch([block(844), block(844)], "soccer") is None
    assert circus_sport_mismatch([block(848)], "tennis") is None


def test_swapped_push_is_refused():
    """Le cas vu en production : le tennis poussé vers soccer.json."""
    assert circus_sport_mismatch([block(848)], "soccer") == {848}
    assert circus_sport_mismatch([block(844)], "tennis") == {844}


def test_mixed_push_is_refused():
    """Les blocs se mélangent aussi, pas seulement s'échangent : 380+261
    événements au lieu de 464+177, mêmes totaux, mauvaise répartition."""
    assert circus_sport_mismatch([block(844), block(848)], "soccer") == {844, 848}


def test_a_push_without_sport_ids_is_not_blocked():
    """Ne pas savoir juger n'est pas une raison de couper le book."""
    assert circus_sport_mismatch([{"Leagues": []}], "soccer") is None
    assert circus_sport_mismatch([{"Leagues": [{"Events": []}]}], "soccer") is None


def test_an_unknown_sport_is_not_blocked():
    """Ajouter un sport au userscript ne doit pas exiger de toucher au
    serveur d'abord, sinon le pont casse le temps du déploiement."""
    assert circus_sport_mismatch([block(999)], "basketball") is None


def test_ids_stay_aligned_with_the_daemon():
    """Deux tables de SportId dans deux fichiers : elles divergeront un jour,
    et le symptôme serait un book qui disparaît en silence."""
    from src.main import CIRCUS_SPORTS

    assert server.CIRCUS_SPORT_IDS == dict(CIRCUS_SPORTS)
