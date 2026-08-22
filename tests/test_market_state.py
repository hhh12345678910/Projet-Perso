"""`market_state` : le dernier prix connu, projection de `quotes`.

`quotes` est un journal en AJOUT, purgé à quelques jours : « le dernier prix »
s'y paie par un GROUP BY sur des millions de lignes. Le futur moteur LIVE
raisonne sur les VARIATIONS et ne peut pas payer ça à chaque tick.

⚠️ Le piège de cette table est SQLite lui-même, et il est silencieux : `line`
vaut NULL sur tout 1X2, et deux NULL sont DISTINCTS dans un index unique. Une
clé primaire naïve laisse l'UPSERT ne jamais se déclencher — trois écritures
identiques donnent trois lignes, sans la moindre erreur. Le premier test tient
cette garde, et il vérifie d'abord que le piège est réel.
"""
from __future__ import annotations

import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.models import Book, MarketType, OddQuote, Outcome
from src.storage import Storage

MAINTENANT = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc)
COUP_ENVOI = MAINTENANT + timedelta(hours=3)
EK = f"{COUP_ENVOI.strftime('%Y%m%d%H%M')}::anderlecht__vs__clubbrugge"


def _q(label="home", odd=2.0, line=None, book=Book.UNIBET_BE,
       market=MarketType.H2H, quand=MAINTENANT, live=False, league="Jupiler Pro League"):
    return OddQuote(event_key=EK, book=book, market=market,
                    outcome=Outcome(label=label, line=line), decimal_odd=odd,
                    fetched_at=quand, source_event_id="1", league=league,
                    from_live_feed=live)


@pytest.fixture()
def st(tmp_path):
    return Storage(str(tmp_path / "t.db"))


# ── Le piège NULL ────────────────────────────────────────────────────────

def test_le_piege_des_NULL_est_reel():
    """⚠️ À vérifier AVANT la garde, sinon le test suivant ne prouve rien.

    SQLite accepte les NULL dans une clé primaire et les considère comme
    distincts. Le schéma « évident » est donc faux, et faux en silence."""
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE naif (ek TEXT, market TEXT, label TEXT, line REAL, "
              "book TEXT, odd REAL, PRIMARY KEY (ek, market, label, line, book))")
    for _ in range(3):
        c.execute("INSERT INTO naif VALUES ('e','h2h','home',NULL,'u',2.0) "
                  "ON CONFLICT(ek,market,label,line,book) DO UPDATE SET odd=excluded.odd")
    assert c.execute("SELECT COUNT(*) FROM naif").fetchone()[0] == 3, \
        "si ce test tombe, SQLite a changé et la garde COALESCE peut être revue"


def test_trois_ecritures_identiques_sur_une_ligne_NULL_donnent_UNE_ligne(st):
    """La garde. Un 1X2 a `line` NULL : c'est le cas le plus courant du projet."""
    for _ in range(3):
        st.insert_quotes([_q()])
    lignes = st.market_state()
    assert len(lignes) == 1
    assert lignes[0]["line"] is None, "`line` doit rester lisible en NULL"


def test_une_vraie_ligne_ne_collisionne_pas_avec_le_sentinel(st):
    """Le sentinel -1e9 ne doit pas se confondre avec un total réel."""
    st.insert_quotes([_q(), _q(label="over", line=2.5, market=MarketType.TOTALS),
                      _q(label="over", line=-1.0, market=MarketType.TOTALS)])
    assert len(st.market_state()) == 3


# ── Ce que la table conserve ─────────────────────────────────────────────

def test_les_neuf_colonnes_demandees_sont_ecrites(st):
    st.insert_quotes([_q(label="over", odd=1.87, line=2.5,
                         market=MarketType.TOTALS, live=True)])
    r = st.market_state()[0]
    assert r["event_key"] == EK and r["book"] == "unibet_be"
    assert r["market"] == "totals" and r["outcome_label"] == "over"
    assert r["line"] == 2.5 and r["odd"] == 1.87
    assert r["fetched_at"] == MAINTENANT.isoformat()
    assert r["is_live"] == 1
    assert r["league"] == "Jupiler Pro League"


def test_is_live_vient_de_from_live_feed_pas_d_un_nouveau_champ(st):
    """Le drapeau existait déjà sur `OddQuote` : il n'est pas persisté, c'est
    tout. Rien de neuf n'a été inventé côté modèle."""
    st.insert_quotes([_q(live=False), _q(book=Book.BETANO_BE, live=True)])
    par_book = {r["book"]: r["is_live"] for r in st.market_state()}
    assert par_book == {"unibet_be": 0, "betano_be": 1}


def test_une_ligue_connue_n_est_jamais_ecrasee_par_un_vide(st):
    """Même règle que `upsert_events` : une ligue jamais remplie ne se rattrape
    pas, une ligue déjà connue ne doit pas être perdue."""
    st.insert_quotes([_q()])
    st.insert_quotes([_q(odd=2.4, league=None)])
    r = st.market_state()[0]
    assert r["odd"] == 2.4 and r["league"] == "Jupiler Pro League"


# ── Les trois lectures du LIVE ───────────────────────────────────────────

def test_les_trois_filtres_de_lecture(st):
    autre = f"{COUP_ENVOI.strftime('%Y%m%d%H%M')}::genk__vs__gand"
    st.insert_quotes([
        _q(),
        _q(book=Book.BETANO_BE, live=True, odd=2.1),
        OddQuote(event_key=autre, book=Book.UNIBET_BE, market=MarketType.H2H,
                 outcome=Outcome(label="home"), decimal_odd=3.0,
                 fetched_at=MAINTENANT + timedelta(minutes=5), source_event_id="2"),
    ])
    assert len(st.market_state()) == 3
    assert len(st.market_state(event_key=EK)) == 2
    assert {r["book"] for r in st.market_state(live_only=True)} == {"betano_be"}
    recents = st.market_state(since=MAINTENANT + timedelta(minutes=1))
    assert len(recents) == 1 and recents[0]["event_key"] == autre


# ── L'écriture creuse, et les DEUX instances ─────────────────────────────

def test_deux_ecritures_identiques_n_ecrivent_qu_une_fois(st):
    """Le comportement d'origine, inchangé : un marché qui n'a pas bougé n'est
    pas réécrit dans le journal."""
    assert st.insert_quotes_sparse([_q()]) == 1
    assert st.insert_quotes_sparse([_q()]) == 0


def test_une_vraie_variation_est_ecrite(st):
    assert st.insert_quotes_sparse([_q(odd=2.0)]) == 1
    assert st.insert_quotes_sparse([_q(odd=2.05)]) == 1
    r = st.market_state()[0]
    assert r["odd"] == 2.05, "l'état courant doit porter la DERNIÈRE valeur"


def test_deux_instances_ne_reecrivent_pas_le_meme_instantane(tmp_path):
    """⚠️ LE point de cette phase. Avant, l'état ne vivait qu'en mémoire
    d'instance : une seconde instance partait d'un dictionnaire vide et
    réécrivait tout, y compris ce que la première venait d'écrire."""
    db = str(tmp_path / "t.db")
    a, b = Storage(db), Storage(db)

    assert a.insert_quotes_sparse([_q()]) == 1
    assert b.insert_quotes_sparse([_q()]) == 0, \
        "B doit VOIR ce que A a écrit, via market_state"

    # A modifie : B doit le voir aussi, et ne pas réécrire par-dessus.
    assert a.insert_quotes_sparse([_q(odd=2.5)]) == 1
    c = Storage(db)
    assert c.insert_quotes_sparse([_q(odd=2.5)]) == 0
    assert c.market_state()[0]["odd"] == 2.5


def test_aucune_donnee_perdue_ni_dupliquee_entre_deux_instances(tmp_path):
    """Le scénario demandé, en entier : A écrit, B écrit la même, A modifie."""
    db = str(tmp_path / "t.db")
    a, b = Storage(db), Storage(db)

    a.insert_quotes_sparse([_q(odd=2.0)])
    b.insert_quotes_sparse([_q(odd=2.0)])
    a.insert_quotes_sparse([_q(odd=2.5)])

    etat = Storage(db).market_state()
    assert len(etat) == 1, "une sélection = une ligne d'état, quel que soit le nombre d'instances"
    assert etat[0]["odd"] == 2.5, "la dernière valeur correcte doit être disponible"

    with Storage(db)._conn() as c:
        n = c.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
    assert n == 2, f"journal : 2 écritures attendues (2.0 puis 2.5), {n} trouvées"


def test_l_amorcage_reconstruit_la_signature_a_l_identique(tmp_path):
    """Si la reconstruction n'était pas EXACTE, un marché inchangé paraîtrait
    changé — écriture inutile — ou l'inverse — écriture perdue."""
    db = str(tmp_path / "t.db")
    a = Storage(db)
    a.insert_quotes_sparse([
        _q(label="home", odd=2.0), _q(label="draw", odd=3.4), _q(label="away", odd=3.8),
        _q(label="over", odd=1.87, line=2.5, market=MarketType.TOTALS),
    ])
    b = Storage(db)
    b._amorcer_quote_sig()
    assert b._quote_sig == a._quote_sig, "terme à terme, clés et signatures comprises"


def test_la_base_reste_saine_apres_ecritures_croisees(tmp_path):
    db = str(tmp_path / "t.db")
    for i in range(20):
        Storage(db).insert_quotes_sparse([_q(odd=2.0 + i * 0.01)])
    with Storage(db)._conn() as c:
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert c.execute("SELECT COUNT(*) FROM market_state").fetchone()[0] == 1


# ── Deux PROCESSUS, réellement ───────────────────────────────────────────

_ENFANT = """
import sys
from datetime import datetime, timedelta, timezone
sys.path.insert(0, {racine!r})
from src.models import Book, MarketType, OddQuote, Outcome
from src.storage import Storage
q = OddQuote(event_key={ek!r}, book=Book.UNIBET_BE, market=MarketType.H2H,
             outcome=Outcome(label="home"), decimal_odd=float(sys.argv[2]),
             fetched_at=datetime.fromisoformat({quand!r}), source_event_id="1",
             league="Jupiler Pro League")
print(Storage(sys.argv[1]).insert_quotes_sparse([q]))
"""


def _enfant(db: str, odd: float) -> int:
    """Un vrai processus séparé : même base, aucun état mémoire partagé."""
    code = _ENFANT.format(racine=str(Path(__file__).resolve().parent.parent),
                          ek=EK, quand=MAINTENANT.isoformat())
    r = subprocess.run([sys.executable, "-c", code, db, str(odd)],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return int(r.stdout.strip())


def test_deux_processus_separes_partagent_la_decision(tmp_path):
    """⚠️ Deux instances dans un même interpréteur partagent le cache disque et
    l'import : ça ne prouve pas le multi-processus. Ici ce sont de VRAIS
    processus, sans rien en commun que le fichier."""
    db = str(tmp_path / "t.db")
    Storage(db)                                  # schéma

    assert _enfant(db, 2.00) == 1, "premier processus : écriture"
    assert _enfant(db, 2.00) == 0, "second processus : rien à écrire"
    assert _enfant(db, 2.50) == 1, "vraie variation : écriture"

    st = Storage(db)
    etat = st.market_state()
    assert len(etat) == 1 and etat[0]["odd"] == 2.5
    with st._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 2
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# ── La purge ─────────────────────────────────────────────────────────────

def test_les_matchs_finis_sont_oublies(st):
    vieux_ko = MAINTENANT - timedelta(days=5)
    vieux = f"{vieux_ko.strftime('%Y%m%d%H%M')}::vieux__vs__match"
    st.insert_quotes([_q(), OddQuote(event_key=vieux, book=Book.UNIBET_BE,
                                     market=MarketType.H2H, outcome=Outcome(label="home"),
                                     decimal_odd=2.0, fetched_at=MAINTENANT,
                                     source_event_id="1")])
    assert len(st.market_state()) == 2
    assert st.prune_market_state(max_age_days=2.0, now=MAINTENANT) == 1
    assert {r["event_key"] for r in st.market_state()} == {EK}


def test_le_critere_est_le_coup_d_envoi_pas_la_derniere_ecriture(st):
    """⚠️ Un marché qu'on ne cote plus n'est plus rafraîchi : son `fetched_at`
    se fige, et le prendre pour critère le rendrait immortel au moment même où
    il devient inutile."""
    vieux_ko = MAINTENANT - timedelta(days=5)
    vieux = f"{vieux_ko.strftime('%Y%m%d%H%M')}::vieux__vs__match"
    st.insert_quotes([OddQuote(event_key=vieux, book=Book.UNIBET_BE,
                               market=MarketType.H2H, outcome=Outcome(label="home"),
                               decimal_odd=2.0, fetched_at=MAINTENANT,   # écrit à l'instant
                               source_event_id="1")])
    assert st.prune_market_state(max_age_days=2.0, now=MAINTENANT) == 1


def test_une_cle_illisible_part_aussi(st):
    st.insert_quotes([_q()])
    with st._conn() as c:
        c.execute("UPDATE market_state SET event_key = 'pas-une-cle'")
    assert st.prune_market_state(max_age_days=2.0, now=MAINTENANT) == 1
    assert st.market_state() == []


def test_la_purge_vide_aussi_le_cache_en_memoire(st):
    """Sinon le cache affirmerait un état que la base n'a plus, et le marché ne
    serait jamais réécrit."""
    vieux_ko = MAINTENANT - timedelta(days=5)
    vieux = f"{vieux_ko.strftime('%Y%m%d%H%M')}::vieux__vs__match"
    q = OddQuote(event_key=vieux, book=Book.UNIBET_BE, market=MarketType.H2H,
                 outcome=Outcome(label="home"), decimal_odd=2.0,
                 fetched_at=MAINTENANT, source_event_id="1")
    st.insert_quotes_sparse([q])
    assert st.insert_quotes_sparse([q]) == 0
    st.prune_market_state(max_age_days=2.0, now=MAINTENANT)
    assert st.insert_quotes_sparse([q]) == 1, "après purge, le marché redevient inconnu"


# ── Migration ────────────────────────────────────────────────────────────

def test_la_migration_est_idempotente_et_ne_detruit_rien(tmp_path):
    """Une installation existante ne doit rien perdre, et rouvrir la base dix
    fois ne doit rien changer."""
    db = str(tmp_path / "t.db")
    st = Storage(db)
    st.insert_quotes([_q()])
    st.upsert_event(EK, "soccer", "Jupiler Pro League", "a", "b", COUP_ENVOI)
    for _ in range(10):
        Storage(db)
    st2 = Storage(db)
    assert len(st2.market_state()) == 1
    with st2._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


def test_une_base_ANCIENNE_sans_market_state_se_met_a_niveau(tmp_path):
    """Le cas réel de la VM : la base existe déjà, avec des données, et sans
    cette table."""
    db = tmp_path / "ancienne.db"
    c = sqlite3.connect(str(db))
    c.executescript("""
        CREATE TABLE quotes (id INTEGER PRIMARY KEY, event_key TEXT, book TEXT,
            market TEXT, outcome_label TEXT, line REAL, decimal_odd REAL,
            fetched_at TEXT, source_event_id TEXT);
        INSERT INTO quotes(event_key, book, market, outcome_label, line,
            decimal_odd, fetched_at, source_event_id)
        VALUES ('vieux', 'unibet_be', 'h2h', 'home', NULL, 1.5, '2026-01-01', 'x');
    """)
    c.commit(); c.close()

    st = Storage(str(db))
    with st._conn() as cc:
        assert cc.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == 1, \
            "les données antérieures doivent survivre"
        assert cc.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='market_state'"
        ).fetchone()[0] == 1
    assert st.market_state() == []
