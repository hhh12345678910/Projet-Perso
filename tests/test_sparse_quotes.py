"""L'écriture parcimonieuse de `quotes` ne doit RIEN changer à la CLV.

`quotes` pèse 34 Go pour deux jours alors que line_speed mesure 99,6 % de
cotes Pinnacle identiques d'un cycle à l'autre : plus de 99 % de ce qui est
écrit répète ce qui est déjà en base. N'écrire que les marchés qui ont bougé
divise le volume par plusieurs dizaines.

Mais `quotes` est la table dont dépend TOUTE la mesure de CLV : le prix de
clôture n'existe que dans cette capture. Ces tests sont le filet de sécurité —
ils comparent la clôture obtenue avant et après le changement, et échouent au
moindre écart.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.models import Book, MarketType, OddQuote, Outcome
from src.storage import Storage

T0 = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
KICKOFF = datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)
EK = "202608142000::alpha__vs__beta"


def _market(odds: dict[str, float], at: datetime, book=Book.PINNACLE):
    return [
        OddQuote(event_key=EK, book=book, market=MarketType.H2H,
                 outcome=Outcome(label=lbl), decimal_odd=o,
                 fetched_at=at, source_event_id="x")
        for lbl, o in odds.items()
    ]


def _closing(st: Storage) -> dict[str, float]:
    grp = st.closing_group(EK, "h2h", None, KICKOFF, book="pinnacle")
    return {r["outcome_label"]: r["decimal_odd"] for r in grp}


def test_closing_is_identical_with_dense_and_sparse_writes(tmp_path):
    """LE test qui compte. Même flux de cotes, deux modes d'écriture, et la
    clôture doit être rigoureusement la même."""
    seq = [
        (0,  {"home": 2.00, "away": 2.10}),
        (1,  {"home": 2.00, "away": 2.10}),   # inchangé
        (2,  {"home": 2.00, "away": 2.10}),   # inchangé
        (3,  {"home": 1.95, "away": 2.16}),   # bouge
        (4,  {"home": 1.95, "away": 2.16}),   # inchangé
        (5,  {"home": 1.90, "away": 2.22}),   # bouge -> c'est la clôture
        (6,  {"home": 1.90, "away": 2.22}),   # inchangé
    ]

    dense = Storage(str(tmp_path / "dense.db"))
    for i, odds in seq:
        dense.insert_quotes(_market(odds, T0 + timedelta(minutes=i)))

    sparse = Storage(str(tmp_path / "sparse.db"))
    for i, odds in seq:
        sparse.insert_quotes_sparse(_market(odds, T0 + timedelta(minutes=i)))

    assert _closing(dense) == _closing(sparse)
    assert _closing(sparse) == {"home": 1.90, "away": 2.22}


def test_sparse_writes_far_fewer_rows(tmp_path):
    st = Storage(str(tmp_path / "s.db"))
    written = 0
    for i in range(20):                       # 20 cycles, aucun changement
        written += st.insert_quotes_sparse(_market({"home": 2.0, "away": 2.1},
                                                   T0 + timedelta(minutes=i)))
    # Le premier cycle écrit, les suivants non (battement de cœur à 30 min).
    assert written == 2, f"attendu 2 lignes (1 marché), obtenu {written}"


def test_a_market_is_rewritten_WHOLE_when_one_outcome_moves(tmp_path):
    """Design B : on réécrit le marché ENTIER dès qu'une issue bouge, pour que
    toutes les issues d'une clôture partagent le même fetched_at — l'invariant
    dont dépend le devig."""
    st = Storage(str(tmp_path / "w.db"))
    st.insert_quotes_sparse(_market({"home": 2.00, "away": 2.10}, T0))
    n = st.insert_quotes_sparse(_market({"home": 2.00, "away": 2.30},
                                        T0 + timedelta(minutes=1)))
    assert n == 2, "les deux issues doivent être réécrites, pas seulement celle qui bouge"
    grp = st.closing_group(EK, "h2h", None, KICKOFF, book="pinnacle")
    assert len({r["fetched_at"] for r in grp}) == 1, "même instant de capture"


def test_heartbeat_rewrites_an_unchanged_market(tmp_path):
    """Un book dont les cotes ne bougent pas ne doit pas devenir indiscernable
    d'un book en panne — c'est le mode de défaillance du §11. Le battement de
    cœur garantit une trace périodique."""
    st = Storage(str(tmp_path / "h.db"), quote_heartbeat_sec=1800)
    st.insert_quotes_sparse(_market({"home": 2.0, "away": 2.1}, T0))
    n = st.insert_quotes_sparse(_market({"home": 2.0, "away": 2.1},
                                        T0 + timedelta(minutes=10)))
    assert n == 0, "10 min : pas encore de battement"
    n = st.insert_quotes_sparse(_market({"home": 2.0, "away": 2.1},
                                        T0 + timedelta(minutes=31)))
    assert n == 2, "31 min : le battement doit réécrire le marché"


def test_two_books_are_tracked_separately(tmp_path):
    st = Storage(str(tmp_path / "b.db"))
    st.insert_quotes_sparse(_market({"home": 2.0, "away": 2.1}, T0, Book.PINNACLE))
    n = st.insert_quotes_sparse(_market({"home": 2.0, "away": 2.1}, T0, Book.UNIBET_BE))
    assert n == 2, "un autre book est un autre marché, il doit être écrit"
