"""Le dédoublonnage doit pouvoir compter PAR CANAL, sans rien changer avant.

Pourquoi ce commit existe
-------------------------
`notified_value_bets` n'avait aucune colonne de canal, et le filtre tourne
dans `main.py` AVANT l'envoi. En multi-canal, un pari envoyé au canal Tennis
serait marqué « notifié » globalement et ne partirait JAMAIS au canal Grosses
Cotes, même si ses critères correspondent. C'est le blocage qui empêche tout
le reste ; ce commit le lève, et rien d'autre.

Deux garde-fous portent le risque :

  * `test_sans_chat_id_rien_ne_change` — la production n'appelle encore
    JAMAIS ces fonctions avec un canal. Le comportement doit être identique
    au bit près, sinon ce commit modifie la production alors qu'il prétend ne
    pas le faire ;
  * `test_une_ligne_heritee_compte_pour_tous_les_canaux` — les lignes
    d'avant la bascule ont chat_id NULL. Les ignorer ferait paraître chaque
    pari vivant « jamais notifié » sur chaque canal le jour du déploiement :
    une rafale de doublons proportionnelle au nombre de canaux, au moment
    précis où personne ne regarde.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src.storage import Storage

T0 = datetime(2026, 8, 30, 12, 0, tzinfo=timezone.utc)
CLE = "202609011800::equipe_a__vs__equipe_b"
PARI = (CLE, "unibet_be", "h2h", "home", None)


@pytest.fixture
def st(tmp_path) -> Storage:
    return Storage(str(tmp_path / "t.db"))


def _marque(st, *, ev=10.0, canal=None, decalage_min=0):
    st.mark_value_bet_notified(*PARI, ev, T0 + timedelta(minutes=decalage_min),
                               chat_id=canal)


# ══ ce qui ne doit PAS changer ═════════════════════════════════════════
def test_sans_chat_id_rien_ne_change(st):
    """Le seul appel que fait la production aujourd'hui."""
    assert st.value_bet_notify_count(*PARI) == 0
    assert st.value_bet_already_notified(*PARI, current_ev_pct=10.0,
                                         ev_delta_pct=2.0) is False
    _marque(st, ev=10.0)
    assert st.value_bet_notify_count(*PARI) == 1
    assert st.value_bet_already_notified(*PARI, current_ev_pct=10.5,
                                         ev_delta_pct=2.0) is True
    assert st.value_bet_already_notified(*PARI, current_ev_pct=15.0,
                                         ev_delta_pct=2.0) is False


def test_sans_chat_id_le_compte_reste_global(st):
    """Trois canaux, un plafond commun : c'est le comportement d'avant, et il
    doit rester atteignable pour ne pas casser `main.py` tant qu'il n'a pas
    bougé."""
    _marque(st, canal="A")
    _marque(st, canal="B")
    _marque(st, canal=None)
    assert st.value_bet_notify_count(*PARI) == 3


def test_la_colonne_est_ecrite_a_NULL_par_defaut(st):
    """Une ligne sans canal doit être NULL, pas une chaîne vide : la clause
    `chat_id IS NULL` en dépend."""
    _marque(st)
    with sqlite3.connect(st.path) as c:
        assert c.execute(
            "SELECT chat_id FROM notified_value_bets").fetchone()[0] is None


# ══ le comportement nouveau ════════════════════════════════════════════
def test_deux_canaux_comptent_separement(st):
    _marque(st, canal="TENNIS")
    assert st.value_bet_notify_count(*PARI, chat_id="TENNIS") == 1
    assert st.value_bet_notify_count(*PARI, chat_id="GROSSES_EV") == 0


def test_un_canal_ne_voit_pas_le_delta_d_EV_d_un_autre(st):
    """Le cœur du besoin : le même pari doit pouvoir partir sur un second
    canal même s'il vient de partir sur le premier."""
    _marque(st, ev=10.0, canal="TENNIS")
    assert st.value_bet_already_notified(
        *PARI, current_ev_pct=10.5, ev_delta_pct=2.0, chat_id="TENNIS") is True
    assert st.value_bet_already_notified(
        *PARI, current_ev_pct=10.5, ev_delta_pct=2.0, chat_id="GROSSES_EV") is False


def test_le_plafond_par_canal_est_independant(st):
    _marque(st, canal="TENNIS", decalage_min=0)
    _marque(st, canal="TENNIS", decalage_min=1)
    _marque(st, canal="UNIBET", decalage_min=2)
    assert st.value_bet_notify_count(*PARI, chat_id="TENNIS") == 2
    assert st.value_bet_notify_count(*PARI, chat_id="UNIBET") == 1
    assert st.value_bet_notify_count(*PARI) == 3


# ══ la migration ═══════════════════════════════════════════════════════
def test_une_ligne_heritee_compte_pour_tous_les_canaux(st):
    """Le garde-fou anti-rafale. Une ligne écrite avant la bascule (NULL)
    doit interdire un renvoi sur N'IMPORTE quel canal."""
    _marque(st, ev=10.0, canal=None)
    for canal in ("TENNIS", "GROSSES_EV", "UNIBET"):
        assert st.value_bet_notify_count(*PARI, chat_id=canal) == 1
        assert st.value_bet_already_notified(
            *PARI, current_ev_pct=10.5, ev_delta_pct=2.0, chat_id=canal) is True


def test_l_heritage_et_un_canal_s_additionnent(st):
    _marque(st, canal=None)
    _marque(st, canal="TENNIS", decalage_min=1)
    assert st.value_bet_notify_count(*PARI, chat_id="TENNIS") == 2
    assert st.value_bet_notify_count(*PARI, chat_id="UNIBET") == 1


def test_la_colonne_apparait_sur_une_base_existante(tmp_path):
    """Migration en place : une base d'avant le commit doit gagner la colonne
    SANS perdre ses lignes."""
    chemin = str(tmp_path / "ancienne.db")
    Storage(chemin)
    with sqlite3.connect(chemin) as c:
        c.execute("DROP TABLE notified_value_bets")
        c.execute("CREATE TABLE notified_value_bets ("
                  "id INTEGER PRIMARY KEY AUTOINCREMENT, event_key TEXT NOT NULL,"
                  "book TEXT NOT NULL, market TEXT NOT NULL,"
                  "outcome_label TEXT NOT NULL, line REAL, ev_pct REAL NOT NULL,"
                  "notified_at TEXT NOT NULL)")
        c.execute("INSERT INTO notified_value_bets"
                  "(event_key, book, market, outcome_label, line, ev_pct, notified_at)"
                  " VALUES (?,?,?,?,?,?,?)",
                  (CLE, "unibet_be", "h2h", "home", None, 10.0, T0.isoformat()))

    st = Storage(chemin)   # rouvre : les migrations s'appliquent
    with sqlite3.connect(chemin) as c:
        colonnes = [r[1] for r in c.execute("PRAGMA table_info(notified_value_bets)")]
    assert "chat_id" in colonnes
    assert st.value_bet_notify_count(*PARI) == 1, "la ligne d'origine a disparu"
    assert st.value_bet_notify_count(*PARI, chat_id="TENNIS") == 1


def test_l_index_par_canal_existe(st):
    with sqlite3.connect(st.path) as c:
        noms = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
    assert "idx_nvb_canal" in noms


# ══ les cas limites ════════════════════════════════════════════════════
def test_la_ligne_reste_dans_la_cle(st):
    """Deux lignes différentes du même marché ne doivent pas se confondre,
    canal ou pas."""
    st.mark_value_bet_notified(CLE, "unibet_be", "totals", "over", 2.5,
                               10.0, T0, chat_id="TENNIS")
    assert st.value_bet_notify_count(CLE, "unibet_be", "totals", "over", 2.5,
                                     chat_id="TENNIS") == 1
    assert st.value_bet_notify_count(CLE, "unibet_be", "totals", "over", 3.5,
                                     chat_id="TENNIS") == 0


def test_la_tolerance_sur_l_heure_de_coup_d_envoi_survit(st):
    """`_event_key_like` rend le dédoublonnage robuste à un coup d'envoi
    décalé de quelques minutes. La clause de canal ne doit pas la casser."""
    _marque(st, canal="TENNIS")
    decale = "202609011805::equipe_a__vs__equipe_b"
    assert st.value_bet_notify_count(decale, "unibet_be", "h2h", "home", None,
                                     chat_id="TENNIS") == 1


def test_la_purge_ne_regarde_pas_le_canal(st):
    """`prune_notifications` supprime par date. Rien à changer, mais une
    purge qui épargnerait les lignes NULL ferait resurgir des doublons."""
    st.mark_value_bet_notified(*PARI, 10.0,
                               datetime.now(timezone.utc) - timedelta(days=90),
                               chat_id=None)
    st.mark_value_bet_notified(*PARI, 10.0,
                               datetime.now(timezone.utc) - timedelta(days=90),
                               chat_id="TENNIS")
    assert st.prune_notifications(retention_days=30) == 2
    assert st.value_bet_notify_count(*PARI) == 0
