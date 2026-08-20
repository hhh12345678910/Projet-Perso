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
