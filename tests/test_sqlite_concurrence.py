"""Deux écrivains sur la même base : attendre plutôt que perdre.

Le §21.22 a chiffré ce que coûte l'inverse : **695 collectes perdues en 11 h
sur `database is locked`**, à UN seul processus. La cause n'était pas SQLite
mais la patience qu'on lui accordait — cinq valeurs différentes cohabitaient
sur le même fichier (3 s et 5 s dans l'alerter, 10 s sur le chemin chaud du
cycle, 60 s pour la purge et VACUUM), et c'est le chemin le plus court qui
lâchait en premier, c'est-à-dire celui du cycle.

Ces tests ne vérifient pas qu'une constante vaut 60. Ils vérifient que **le
réglage change le résultat** : sous contention réelle, une attente courte perd
l'écriture, une attente longue la sauve.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from src.config import SQLITE_BUSY_TIMEOUT_SEC
from src.storage import Storage


def _verrou_pris(chemin: Path, tenu_sec: float) -> threading.Event:
    """Prend le verrou d'écriture et le tient `tenu_sec`, dans un fil à part."""
    pris = threading.Event()

    def _tenir():
        con = sqlite3.connect(str(chemin), timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("BEGIN IMMEDIATE")          # verrou d'écriture, tout de suite
        con.execute("INSERT INTO teams(normalized_name, display_name, last_seen_at)"
                    " VALUES ('x','X','2026-01-01')")
        pris.set()
        time.sleep(tenu_sec)
        con.commit()
        con.close()

    threading.Thread(target=_tenir, daemon=True).start()
    assert pris.wait(5), "le verrou n'a pas été pris"
    return pris


def test_une_attente_courte_PERD_l_ecriture(tmp_path):
    """⚠️ Le comportement d'AVANT, reproduit. Sans cette démonstration, le test
    suivant ne prouverait rien : il passerait aussi bien sans contention."""
    db = tmp_path / "t.db"
    Storage(str(db))                       # crée le schéma
    _verrou_pris(db, tenu_sec=1.5)

    court = sqlite3.connect(str(db), timeout=0.2)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        court.execute("INSERT INTO teams(normalized_name, display_name, last_seen_at)"
                      " VALUES ('y','Y','2026-01-01')")
        court.commit()
    court.close()


def test_l_attente_du_projet_SAUVE_l_ecriture(tmp_path):
    """Même contention, même durée — seule la patience change."""
    db = tmp_path / "t.db"
    st = Storage(str(db))
    _verrou_pris(db, tenu_sec=1.5)

    debut = time.monotonic()
    st.upsert_event("202601011200::a__vs__b", "soccer", "L", "a", "b",
                    __import__("datetime").datetime(2026, 1, 1, 12, 0,
                                                    tzinfo=__import__("datetime").timezone.utc))
    attendu = time.monotonic() - debut
    assert attendu >= 1.0, "l'écriture aurait dû ATTENDRE le verrou, pas passer au travers"
    with st._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_tous_les_chemins_partagent_la_meme_attente(tmp_path):
    """⚠️ Le vrai défaut n'était pas la valeur : c'était d'en avoir cinq. Un
    seul chemin impatient suffit à perdre une écriture, et c'était le chemin
    chaud."""
    st = Storage(str(tmp_path / "t.db"))
    attendu_ms = int(SQLITE_BUSY_TIMEOUT_SEC * 1000)
    with st._conn() as c:
        assert c.execute("PRAGMA busy_timeout").fetchone()[0] == attendu_ms

    from src import alerter, storage
    assert alerter.SQLITE_BUSY_TIMEOUT_SEC is SQLITE_BUSY_TIMEOUT_SEC
    assert storage.SQLITE_BUSY_TIMEOUT_SEC is SQLITE_BUSY_TIMEOUT_SEC

    src = Path("src")
    for fichier in ("storage.py", "alerter.py"):
        texte = (src / fichier).read_text(encoding="utf-8")
        assert "busy_timeout=3000" not in texte
        assert "busy_timeout=5000" not in texte
        assert "busy_timeout=60000" not in texte
        assert "timeout=10)" not in texte


def test_le_wal_est_bien_actif(tmp_path):
    """WAL était déjà en place et n'a pas été touché — mais rien ne le
    vérifiait. Sans lui, un lecteur bloquerait un écrivain et l'attente unifiée
    ci-dessus ne suffirait pas."""
    st = Storage(str(tmp_path / "t.db"))
    with st._conn() as c:
        assert c.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
