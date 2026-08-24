"""Extension LIVE de `market_state` : migration, isolation du prématch, écriture.

Ces tests doivent ÉCHOUER si l'extension casse quoi que ce soit du prématch.
D'où deux d'entre eux qui ne testent pas le LIVE du tout, mais l'absence
d'effet du LIVE sur le chemin existant.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.models import Book, MarketType, OddQuote, Outcome
from src.storage import Storage


def _quote(book=Book.PINNACLE, label="home", line=None, odd=1.90,
           when=None, live=False, key="2026-01-01T20:00|a|b"):
    return OddQuote(
        event_key=key,
        book=book,
        market=MarketType.H2H,
        outcome=Outcome(label=label, line=line),
        decimal_odd=odd,
        fetched_at=when or datetime(2026, 1, 1, 19, 0, tzinfo=timezone.utc),
        source_event_id="src-1",
        league="TEST LEAGUE",
        from_live_feed=live,
    )


def _live_row(key="2026-01-01T20:00|a|b", book="asianodds", market="handicap",
              label="home", line=-0.5, odd=1.90, fetched="2026-01-01T19:00:00",
              observed="2026-01-01T18:59:59", hs=1, aw=0, feed="1:0", igm=33):
    return (key, book, market, label, line, odd, fetched, 1, "TEST LEAGUE",
            observed, hs, aw, feed, igm)


@pytest.fixture
def db(tmp_path):
    return Storage(tmp_path / "t.db")


# ── migration ────────────────────────────────────────────────────────────
def test_les_cinq_colonnes_existent(db):
    with db._conn() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(market_state)")}
    assert {"observed_at", "home_score", "away_score", "feed_score", "igm"} <= cols


def test_migration_idempotente(tmp_path):
    """Rejouer l'init ne doit ni échouer ni dupliquer une colonne."""
    p = tmp_path / "t.db"
    Storage(p)
    Storage(p)
    Storage(p)
    with Storage(p)._conn() as c:
        noms = [r["name"] for r in c.execute("PRAGMA table_info(market_state)")]
    assert len(noms) == len(set(noms)), "colonne dupliquée par une re-migration"
    assert "observed_at" in noms


def test_migration_ne_perd_aucune_donnee(tmp_path):
    """Une base écrite AVANT la migration garde ses lignes et ses valeurs.

    On simule l'ancien schéma en supprimant les colonnes ajoutées, on écrit,
    puis on rouvre : c'est exactement ce que vivra la base de production."""
    p = tmp_path / "t.db"
    db = Storage(p)
    with db._conn() as c:
        # L'index porte sur observed_at : il faut l'ôter avant la colonne,
        # sinon SQLite refuse. La re-migration doit le recréer.
        c.execute("DROP INDEX IF EXISTS idx_ms_observed")
        for col in ("observed_at", "home_score", "away_score", "feed_score", "igm"):
            c.execute(f"ALTER TABLE market_state DROP COLUMN {col}")
    db.insert_quotes([_quote(odd=2.34)])
    avant = Storage(p).market_state()
    assert len(avant) == 1 and avant[0]["odd"] == 2.34

    db2 = Storage(p)                           # rouvre => re-migre
    apres = db2.market_state()
    assert len(apres) == 1
    assert apres[0]["odd"] == 2.34, "valeur perdue à la migration"
    assert apres[0]["observed_at"] is None, "colonne neuve doit naître NULL"
    with db2._conn() as c:
        idx = {r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_ms_observed" in idx, "index non recréé par la re-migration"


# ── isolation du prématch ────────────────────────────────────────────────
def test_ecriture_prematch_laisse_les_colonnes_live_nulles(db):
    db.insert_quotes([_quote()])
    (row,) = db.market_state()
    assert row["odd"] == 1.90
    for col in ("observed_at", "home_score", "away_score", "feed_score", "igm"):
        assert row[col] is None, f"le prématch a écrit {col}"


def test_prematch_et_live_cohabitent_sur_le_meme_event(db):
    """Books différents => clés naturelles différentes => aucun conflit."""
    db.upsert_live_state([_live_row(market="h2h", line=None)])
    db.insert_quotes([_quote()])               # même event, book différent

    etat = {r["book"]: r for r in db.market_state()}
    assert set(etat) == {"asianodds", "pinnacle"}
    assert etat["asianodds"]["feed_score"] == "1:0"
    assert etat["asianodds"]["igm"] == 33
    assert etat["pinnacle"]["observed_at"] is None


def test_le_prematch_n_ecrase_pas_le_contexte_live(db):
    """Le test qui protège le vrai risque, sur le chemin de CONFLIT.

    Une première version de ce test écrivait le LIVE et le prématch sous des
    books différents : les clés naturelles diffèrent, l'ON CONFLICT ne se
    déclenche jamais, et le test passait même en sabotant l'UPSERT prématch
    pour qu'il remette `feed_score` à NULL. Il ne protégeait rien.

    Ici les deux écrivent sous le MÊME book et la MÊME sélection. L'UPSERT
    prématch entre donc dans son DO UPDATE, et doit mettre à jour la cote sans
    toucher aux colonnes LIVE — parce qu'il ne les nomme pas."""
    key = "2026-01-01T20:00|a|b"
    db.upsert_live_state([_live_row(
        key=key, book=Book.PINNACLE.value, market="h2h", line=None, odd=1.90)])
    db.insert_quotes([_quote(book=Book.PINNACLE, label="home",
                             line=None, odd=2.50, key=key)])

    rows = db.market_state()
    assert len(rows) == 1, "conflit non déclenché : le test ne teste rien"
    assert rows[0]["odd"] == 2.50, "le prématch doit bien mettre la cote à jour"
    assert rows[0]["feed_score"] == "1:0", "le prématch a écrasé feed_score"
    assert rows[0]["igm"] == 33, "le prématch a écrasé igm"
    assert rows[0]["observed_at"] == "2026-01-01T18:59:59"
    assert rows[0]["home_score"] == 1 and rows[0]["away_score"] == 0


# ── écriture LIVE ────────────────────────────────────────────────────────
def test_upsert_live_met_a_jour_en_place(db):
    db.upsert_live_state([_live_row(odd=1.90, feed="0:0", hs=0, aw=0)])
    db.upsert_live_state([_live_row(odd=2.10, feed="1:0", hs=1, aw=0, igm=41)])
    rows = db.market_state()
    assert len(rows) == 1, "l'UPSERT a inséré au lieu de mettre à jour"
    assert rows[0]["odd"] == 2.10
    assert rows[0]["feed_score"] == "1:0"
    assert rows[0]["igm"] == 41


def test_upsert_live_ligne_nulle_ne_duplique_pas(db):
    """Le piège du §Phase 2 : deux NULL sont DISTINCTS dans un index unique.

    Sans l'index à expression avec COALESCE, trois UPSERT sur un 1X2 (line
    NULL) produisent trois lignes en silence."""
    for odd in (1.90, 2.00, 2.10):
        db.upsert_live_state([_live_row(market="h2h", line=None, odd=odd)])
    rows = db.market_state()
    assert len(rows) == 1
    assert rows[0]["odd"] == 2.10


def test_upsert_live_vide_ne_fait_rien(db):
    assert db.upsert_live_state([]) == 0
    assert db.market_state() == []


def test_upsert_live_n_ecrit_rien_dans_quotes(db):
    """Le LIVE reprice toutes les 2,7 s sur les marchés actifs : le déverser
    dans le journal prématch gonflerait une table que rien ne lit."""
    db.upsert_live_state([_live_row()])
    with db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 0


# ── lecture par fraîcheur ────────────────────────────────────────────────
def test_observed_since_filtre_sur_observed_at_pas_fetched_at(db):
    """Le cas qui justifie la colonne : réécrit récemment, observé il y a
    longtemps. `since` le voit, `observed_since` doit le rejeter."""
    t0 = datetime(2026, 1, 1, 19, 0, tzinfo=timezone.utc)
    db.upsert_live_state([_live_row(
        fetched=t0.isoformat(),
        observed=(t0 - timedelta(minutes=13)).isoformat())])

    seuil = t0 - timedelta(seconds=60)
    assert len(db.market_state(since=seuil)) == 1
    assert db.market_state(observed_since=seuil) == [], (
        "un prix vu il y a 13 min est passé pour frais")


def test_observed_since_garde_ce_qui_est_frais(db):
    t0 = datetime(2026, 1, 1, 19, 0, tzinfo=timezone.utc)
    db.upsert_live_state([_live_row(
        fetched=t0.isoformat(),
        observed=(t0 - timedelta(seconds=5)).isoformat())])
    assert len(db.market_state(observed_since=t0 - timedelta(seconds=60))) == 1


def test_index_observed_at_existe(db):
    with db._conn() as c:
        idx = {r["name"] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_ms_observed" in idx
