"""La purge ne doit pas assommer le daemon.

Constaté en production le 01/08 : lancer `prune` pendant que le daemon tourne
remplissait le log de « database is locked » sur tous les books, sports
entiers compris, pendant toute la durée de la purge. Les cotes de ces cycles
n'ont jamais été collectées — et avec elles les lignes de clôture, qui ne se
rattrapent pas.

La cause n'était pas les DELETE mais `wal_checkpoint(TRUNCATE)` après CHAQUE
lot : TRUNCATE prend un verrou exclusif et attend la fin de tous les lecteurs.
Rien dans les logs ne pointait vers la purge — seulement des books qui
échouent, ce qui ressemble à une panne de scraper.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from src.storage import Storage


def _seed(path, *, old: int, recent: int) -> None:
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=5)).isoformat()
    new_ts = now.isoformat()
    conn = sqlite3.connect(path)
    rows = [("k", "pinnacle", "h2h", "home", None, 2.0, old_ts, "s")] * old
    rows += [("k", "pinnacle", "h2h", "home", None, 2.0, new_ts, "s")] * recent
    conn.executemany(
        "INSERT INTO quotes (event_key, book, market, outcome_label, line,"
        " decimal_odd, fetched_at, source_event_id) VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def _count(path) -> int:
    conn = sqlite3.connect(path)
    n = conn.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
    conn.close()
    return n


def test_prune_deletes_only_what_is_older_than_the_window(tmp_path):
    db = str(tmp_path / "v.db")
    Storage(db)
    _seed(db, old=250, recent=40)

    removed = Storage(db).prune_quotes(retention_days=1, batch_size=100, pause_sec=0)

    assert removed == 250
    assert _count(db) == 40


class _SpyConn:
    """Connexion qui note le SQL exécuté, sans rien changer d'autre."""

    def __init__(self, real, log):
        self._real = real
        self._log = log

    def execute(self, sql, *args):
        self._log.append(sql)
        return self._real.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_truncate_checkpoint_runs_once_at_the_end_not_per_batch(tmp_path, monkeypatch):
    """Le garde-fou central. PASSIVE recycle le WAL sans jamais bloquer ; c'est
    le découpage en lots, et non TRUNCATE, qui borne la taille du fichier -wal.
    Un TRUNCATE par lot ne gagne donc rien et bloque tout."""
    import src.storage as storage_mod

    db = str(tmp_path / "v.db")
    Storage(db)
    _seed(db, old=500, recent=10)

    log: list[str] = []
    real_connect = sqlite3.connect
    monkeypatch.setattr(
        storage_mod.sqlite3, "connect",
        lambda *a, **k: _SpyConn(real_connect(*a, **k), log),
    )

    removed = Storage(db).prune_quotes(retention_days=1, batch_size=100, pause_sec=0)
    assert removed == 500

    truncates = [s for s in log if "TRUNCATE" in s.upper()]
    passives = [s for s in log if "PASSIVE" in s.upper()]
    deletes = [s for s in log if s.strip().upper().startswith("DELETE")]

    assert len(deletes) >= 5, "la suppression doit rester découpée en lots"
    assert len(truncates) == 1, (
        f"{len(truncates)} checkpoints TRUNCATE — un seul, à la fin. Chacun "
        f"prend un verrou exclusif et attend tous les lecteurs : c'est ce qui "
        f"verrouillait la base pour le daemon pendant toute la purge."
    )
    assert passives, "les lots doivent quand même recycler le WAL, en PASSIVE"


def test_prune_on_an_empty_table_is_a_no_op(tmp_path):
    """Aucun lot supprimé : la boucle doit sortir immédiatement, sans tourner
    à vide ni lever."""
    db = str(tmp_path / "v.db")
    Storage(db)
    assert Storage(db).prune_quotes(retention_days=1, pause_sec=0) == 0
