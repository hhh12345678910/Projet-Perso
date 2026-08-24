"""Le run d'écriture contrôlé : ce qu'il mesure, et ce qu'il détecte. §PHASE 4

Le point le plus important n'est pas que le rapport s'affiche, c'est qu'une
modification du prématch le fasse CRIER. Un contrôle qui ne peut pas échouer
ne contrôle rien.
"""
from datetime import datetime, timedelta, timezone

import pytest

from scripts.mesure_ecriture_live import empreinte_prematch, main, octets
from src.models import Book, MarketType, Outcome, OddQuote
from src.storage import Storage


def _quote(cle, book, label, cote, quand, sid="s"):
    return OddQuote(event_key=cle, book=book, market=MarketType.H2H,
                    outcome=Outcome(label), decimal_odd=cote,
                    fetched_at=quand, source_event_id=sid)


def _db(tmp_path, t0):
    db = Storage(tmp_path / "m.db")
    db.upsert_events([("k1", "soccer", "L", "A", "B",
                       (t0 + timedelta(hours=2)).isoformat())])
    db.insert_quotes([
        _quote("k1", Book.PINNACLE, "home", 1.90, t0 - timedelta(minutes=5)),
        _quote("k1", Book.PINNACLE, "away", 2.05, t0 - timedelta(minutes=5)),
    ])
    return db


def test_l_empreinte_detecte_une_ligne_prematch_modifiee(tmp_path):
    """Compter les lignes ne prouverait rien : une ligne réécrite en place ne
    change pas le total. C'est ce que l'empreinte attrape."""
    t0 = datetime(2026, 8, 24, 19, 0, tzinfo=timezone.utc)
    db = _db(tmp_path, t0)
    with db._conn() as c:
        avant = empreinte_prematch(c, t0.isoformat())
        c.execute("UPDATE quotes SET decimal_odd = 9.99 WHERE outcome_label='home'")
        apres = empreinte_prematch(c, t0.isoformat())

    assert avant["quotes"][0] == apres["quotes"][0], "le COMPTE n'a pas bougé"
    assert avant["quotes"][1] != apres["quotes"][1], "la modification a échappé"


def test_l_empreinte_detecte_une_suppression(tmp_path):
    t0 = datetime(2026, 8, 24, 19, 0, tzinfo=timezone.utc)
    db = _db(tmp_path, t0)
    with db._conn() as c:
        avant = empreinte_prematch(c, t0.isoformat())
        c.execute("DELETE FROM quotes WHERE outcome_label='away'")
        apres = empreinte_prematch(c, t0.isoformat())
    assert avant["quotes"] != apres["quotes"]


def test_l_empreinte_ignore_ce_que_le_daemon_AJOUTE_pendant_le_run(tmp_path):
    """La croissance du prématch pendant le run est NORMALE. La compter comme
    une altération rendrait le contrôle inutilisable — il crierait à chaque
    fois."""
    t0 = datetime(2026, 8, 24, 19, 0, tzinfo=timezone.utc)
    db = _db(tmp_path, t0)
    with db._conn() as c:
        avant = empreinte_prematch(c, t0.isoformat())
    db.insert_quotes([_quote("k1", Book.UNIBET_BE, "home", 1.85,
                             t0 + timedelta(minutes=3), "src2")])
    with db._conn() as c:
        apres = empreinte_prematch(c, t0.isoformat())
    assert avant["quotes"] == apres["quotes"], \
        "un ajout postérieur au T0 est compté comme une altération"


def test_le_rapport_complet_tourne_et_conclut_propre(tmp_path, monkeypatch, capsys):
    """Bout en bout, sans réseau : le script doit produire les neuf points et
    conclure PROPRE quand rien n'a bougé."""
    import scripts.mesure_ecriture_live as m
    from tests.test_asianodds_live import EVF_DECIMAL, _FausseSession

    # Horloge RÉELLE de bout en bout. Le script prend son T0 avec
    # `datetime.now()` et sélectionne les échantillons sur `fetched_at >= T0` :
    # une horloge figée dans le passé écrirait des lignes antérieures à son
    # propre T0, et la section 9 sortirait vide sans que rien ne soit cassé.
    t0 = datetime.now(timezone.utc)
    db = Storage(tmp_path / "m.db")
    db.upsert_events([("202608241800::z", "soccer", "SERBIA SUPER LIGA",
                       "Crvena Zvezda", "Cukaricki",
                       (t0 - timedelta(hours=1)).isoformat())])
    db.insert_quotes([_quote("202608241800::z", Book.PINNACLE, "home", 1.90,
                             t0 - timedelta(minutes=5))])

    vraie_collecte = m.collect
    monkeypatch.setattr(m, "collect", lambda storage, u, p, **kw: vraie_collecte(
        storage, u, p, dry_run=False, sport=kw.get("sport", 1),
        session_factory=lambda: _FausseSession([{"EVF": EVF_DECIMAL}]),
        log=lambda *a: None))
    monkeypatch.setenv("AO_USER", "vraiuser")
    monkeypatch.setenv("AO_PASS", "vraipass")
    monkeypatch.setattr("sys.argv", ["m", "--db", str(tmp_path / "m.db"),
                                     "--minutes", "0.01"])

    code = main()
    sortie = capsys.readouterr().out

    assert code == 0, sortie
    assert "VERDICT : PROPRE" in sortie
    for attendu in ("1. lignes market_state", "2. taille de la base",
                    "3-4-6. écriture", "5. erreurs SQLite",
                    "7. impact sur le prématch", "8. intégrité",
                    "9. échantillons réels"):
        assert attendu in sortie, f"section manquante : {attendu}"
    assert "integrity_check    : ok" in sortie
    assert "quotes         : INTACT" in sortie
    assert "asianodds : 0 → 12" in sortie
    # L'échantillon doit porter le contexte LIVE, sinon la colonne ne sert
    # à rien : c'est tout l'objet de l'extension de market_state.
    assert "0:0 à 67'" in sortie
    assert "lignes asianodds sans contexte LIVE complet : 0" in sortie


def test_le_rapport_crie_si_le_prematch_a_bouge(tmp_path, monkeypatch, capsys):
    """Falsification du contrôle lui-même : si écrire du LIVE altérait le
    prématch, le rapport doit sortir À EXAMINER et un code non nul."""
    import scripts.mesure_ecriture_live as m
    from tests.test_asianodds_live import EVF_DECIMAL, _FausseSession

    t0 = datetime(2020, 1, 1, 19, 0, tzinfo=timezone.utc)
    chemin = tmp_path / "m.db"
    db = Storage(chemin)
    db.upsert_events([("202608241800::z", "soccer", "L", "Crvena Zvezda",
                       "Cukaricki", (t0 - timedelta(hours=1)).isoformat())])
    db.insert_quotes([_quote("202608241800::z", Book.PINNACLE, "home", 1.90,
                             t0 - timedelta(minutes=5))])

    def collecte_qui_abime(storage, u, p, **kw):
        with storage._conn() as c:
            c.execute("UPDATE quotes SET decimal_odd = 1.01")
        from src.asianodds_live import collect as vrai
        return vrai(storage, u, p, dry_run=False,
                    session_factory=lambda: _FausseSession([{"EVF": EVF_DECIMAL}]),
                    now_fn=lambda: t0, log=lambda *a: None)

    monkeypatch.setattr(m, "collect", collecte_qui_abime)
    monkeypatch.setenv("AO_USER", "vraiuser")
    monkeypatch.setenv("AO_PASS", "vraipass")
    monkeypatch.setattr("sys.argv", ["m", "--db", str(chemin), "--minutes", "0.01"])

    code = main()
    sortie = capsys.readouterr().out
    assert code == 1
    assert "quotes         : MODIFIÉ" in sortie
    assert "VERDICT : À EXAMINER" in sortie


def test_le_placeholder_est_refuse_avant_toute_ecriture(tmp_path, monkeypatch):
    monkeypatch.setenv("AO_USER", "ton_identifiant_asianodds")
    monkeypatch.setenv("AO_PASS", "x")
    monkeypatch.setattr("sys.argv", ["m", "--db", str(tmp_path / "m.db")])
    assert main() == 2


@pytest.mark.parametrize("n,attendu", [
    (0, "0 o"), (512, "512 o"), (1536, "1.5 Ko"),
    (5 * 1024 ** 2, "5.0 Mo"), (-2048, "-2.0 Ko"),
])
def test_octets_reste_lisible(n, attendu):
    assert octets(n) == attendu
