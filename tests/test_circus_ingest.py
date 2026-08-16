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


# --- MagicBetting : même barrière, même raison -------------------------------

magic_sport_mismatch = server.magic_sport_mismatch


def _mev(sid: int) -> dict:
    return {"Id": 1, "SId": sid, "HT": "A", "AT": "B", "StakeTypes": []}


def test_magic_matching_sport_is_accepted():
    assert magic_sport_mismatch([_mev(1), _mev(1)], "soccer") is None
    assert magic_sport_mismatch([_mev(3)], "tennis") is None


def test_magic_swapped_push_is_refused():
    """Le tennis poussé vers soccer.json, exactement le cas Circus."""
    assert magic_sport_mismatch([_mev(3)], "soccer") == {3}
    assert magic_sport_mismatch([_mev(1)], "tennis") == {1}


def test_magic_unknown_sport_passes():
    """Ajouter un sport ne doit rien casser, et un oubli ici ne doit jamais
    rendre un book muet."""
    assert magic_sport_mismatch([_mev(9)], "basket") is None


# --- Balayage MagicBetting : le plan, la rotation, l'assemblage --------------


def test_magic_event_url_carries_the_right_stake_types():
    """Les marchés demandés diffèrent par sport, et s'y tromper est muet."""
    soccer = server.magic_event_url("soccer", 4535)
    tennis = server.magic_event_url("tennis", 1135)
    assert "tournamentId=4535" in soccer
    assert "stakeTypes=1" in soccer and "stakeTypes=3" in soccer
    assert "stakeTypes=702" in tennis and "stakeTypes=3" in tennis
    assert "702" not in soccer


def test_magic_urls_stay_relative():
    """Aucune URL construite ici ne doit porter d'origine ni de session.

    L'identifiant de session de 36 caractères n'est connu que du navigateur et
    expire. Le coder côté VM le ferait périmer sans prévenir."""
    for u in (server.magic_countries_url(1), server.magic_tournaments_url(1225),
              server.magic_event_url("soccer", 4535)):
        assert u.startswith("/common/")
        assert "http" not in u


def test_magic_plan_rotates_over_the_long_tail(monkeypatch):
    """Les grosses compétitions reviennent à chaque tour, la traîne défile.

    Sans rotation, les dernières ne seraient jamais rafraîchies et serviraient
    des cotes mortes ; en tout rafraîchir, le budget d'appels exploserait."""
    monkeypatch.setattr(server, "MAGIC_MAX_CALLS", 4)
    monkeypatch.setattr(server, "MAGIC_BUDGET_EVENTS", 10_000)
    tours = [{"id": i, "name": str(i), "events": 20 - i} for i in range(10)]
    monkeypatch.setitem(server.MAGIC_CATALOG, "soccer",
                        {"tournaments": tours, "at": 0, "cursor": 0})

    first = [p["part"] for p in server.magic_plan("soccer", 0)]
    second = [p["part"] for p in server.magic_plan("soccer", 0)]
    # Les deux plus grosses (la moitié du plafond) reviennent toujours.
    assert first[:2] == [0, 1] and second[:2] == [0, 1]
    # La traîne, elle, a avancé.
    assert first[2:] != second[2:]


def test_magic_plan_is_empty_without_catalog():
    server.MAGIC_CATALOG.pop("hockey", None)
    assert server.magic_plan("hockey", 0) == []


def test_magic_rebuild_merges_and_drops_stale(tmp_path, monkeypatch):
    """Un morceau périmé sort de l'assemblage ; les Id se dédoublonnent."""
    import json as _json
    import os as _os
    import time as _time

    monkeypatch.setattr(server, "MAGIC_DIR", tmp_path)
    monkeypatch.setattr(server, "MAGIC_PARTS_DIR", tmp_path / "parts")
    monkeypatch.setattr(server, "MAGIC_PART_MAX_AGE", 600)
    d = tmp_path / "parts" / "soccer"
    d.mkdir(parents=True)
    (d / "1.json").write_text(_json.dumps([{"Id": 10}, {"Id": 11}]))
    (d / "2.json").write_text(_json.dumps([{"Id": 11}, {"Id": 12}]))
    vieux = d / "3.json"
    vieux.write_text(_json.dumps([{"Id": 99}]))
    old = _time.time() - 1200
    _os.utime(vieux, (old, old))

    n_ev, n_parts = server.magic_rebuild("soccer", force=True)
    assert (n_ev, n_parts) == (3, 2)          # 10, 11, 12 — jamais 99
    merged = _json.loads((tmp_path / "soccer.json").read_text())
    assert sorted(e["Id"] for e in merged) == [10, 11, 12]


def test_magic_rebuild_is_throttled(tmp_path, monkeypatch):
    """Réécrire plusieurs mégaoctets à chaque morceau userait le disque."""
    monkeypatch.setattr(server, "MAGIC_DIR", tmp_path)
    monkeypatch.setattr(server, "MAGIC_PARTS_DIR", tmp_path / "parts")
    monkeypatch.setattr(server, "MAGIC_REBUILD_EVERY", 999)
    server._MAGIC_LAST_REBUILD.pop("soccer", None)
    assert server.magic_rebuild("soccer") == (0, 0)     # premier passage
    assert server.magic_rebuild("soccer") == (-1, -1)   # différé, pas échoué
