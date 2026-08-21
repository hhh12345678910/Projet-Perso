"""La sonde de surveillance des mi-temps — §21.14.

Son message le plus important n'est pas un chiffre mais une absence : un book
mappé dont aucune cote n'arrive. Pinnacle seul ne produira jamais la moindre
détection, donc c'est cette ligne qui dit si le chantier avance.
"""
from __future__ import annotations

from scripts.check_half_time import _books_mappes


def test_les_books_mappes_sont_lus_dans_le_code_pas_ecrits_en_dur():
    """Ce message a affirmé « seul Betano est mappé » le lendemain du jour où
    Circus l'a été aussi. Un inventaire écrit en dur vieillit mal et finit par
    dire le contraire du code : il doit se déduire des tables de marché.
    """
    mappes = _books_mappes()
    assert mappes.get("circus_be") == 4, "les quatre codes Circus (§21.14)"
    assert mappes.get("betano_be") == 1, "OUH1"


def test_un_book_sans_mi_temps_n_apparait_pas():
    """MagicBetting n'expose aucun marché de mi-temps — relevé le 20/08 sur
    17 identifiants. Il ne doit pas figurer dans l'inventaire."""
    assert "magicbetting" not in _books_mappes()


def test_l_inventaire_suit_l_ajout_d_un_code(monkeypatch):
    """La preuve que rien n'est figé : ajouter un code doit se voir."""
    from src.models import MarketType
    import src.scrapers.circus as circus

    avant = _books_mappes()["circus_be"]
    monkeypatch.setitem(circus._MARKETS, "faux-code-de-test", MarketType.H2H_H1)
    assert _books_mappes()["circus_be"] == avant + 1


def _base(tmp_path, lignes):
    """Une base minimale avec les cotes données : (book, market)."""
    import sqlite3
    import datetime as dt
    import sys
    sys.path.insert(0, ".")
    from src.storage import SCHEMA

    chemin = tmp_path / "t.db"
    c = sqlite3.connect(chemin)
    c.executescript(SCHEMA)
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for book, marche in lignes:
        c.execute(
            "INSERT INTO quotes(event_key,book,market,outcome_label,line,"
            "decimal_odd,fetched_at,source_event_id) "
            "VALUES('e',?,?,'over',1.5,2.0,?,'1')", (book, marche, now))
    c.commit()
    c.close()
    return str(chemin)


def _lancer(db, capsys):
    import sys
    from unittest.mock import patch
    from scripts.check_half_time import main
    with patch.object(sys, "argv", ["check_half_time", "--db", db]):
        main()
    return capsys.readouterr().out


def test_un_book_muet_partout_pointe_vers_l_ingestion(tmp_path, capsys):
    """Betano mappé, aucune cote du tout : c'est l'ingestion, pas le mappage.

    Sans cette distinction, on irait relire un mappage correct pendant qu'un
    pont navigateur est en panne.
    """
    db = _base(tmp_path, [("pinnacle", "totals_h1"), ("circus_be", "totals_h1")])
    out = _lancer(db, capsys)
    assert "betano_be" in out
    assert "AUCUNE cote du tout" in out
    assert "BETANO_PREMATCH_MAX_AGE_MIN" in out


def test_un_book_actif_sans_mi_temps_pointe_vers_les_codes(tmp_path, capsys):
    """Le cas opposé : le book remonte, mais pas ses mi-temps."""
    db = _base(tmp_path, [("pinnacle", "totals_h1"), ("betano_be", "totals")])
    out = _lancer(db, capsys)
    assert "ce book" in out and "remonte bien" in out
    assert "discover_half_time" in out
