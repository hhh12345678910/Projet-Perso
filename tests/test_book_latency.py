"""La sonde lit-elle vraiment ce que la production écrit ?

`scripts/book_latency.py` dit où passe le temps d'un cycle en parsant les
lignes que `fetch_all_parallel` imprime. Deux façons pour ce lien de se
rompre EN SILENCE — la sonde afficherait un tableau vide ou incomplet en ayant
l'air de marcher, ce qui est le mode de défaillance dominant du projet (§11) :

  1. le format change et la regex ne suit pas ;
  2. la ligne dépasse 80 colonnes et `rich`, hors terminal, l'enveloppe en
     deux — la regex ne matche plus rien alors que le format n'a pas bougé.

Le second est vicieux : il ne se déclenche que sur les noms de sport longs, et
jamais quand on teste à la main dans un terminal large.
"""
from __future__ import annotations

import io

import pytest
from rich.console import Console

from scripts.book_latency import RE_KO, RE_OK
from src.orchestration import ligne_book

# Le nom de sport le plus long du projet et le nom de book le plus long : c'est
# leur combinaison qui approche les 80 colonnes.
LONGS = [("volleyball", "MagicBetting"), ("basketball", "GoldenPalace"),
         ("soccer", "Pinnacle"), ("tennis", "EliteSports")]


def _rendu(markup: str) -> str:
    """La ligne telle qu'elle atterrit dans valuebet.log.

    ⚠️ `width=80` n'est pas arbitraire : c'est la valeur que `rich` prend
    lui-même quand sa sortie n'est pas un terminal, donc exactement le cas de
    `scan-daemon.sh` qui redirige vers un fichier."""
    buf = io.StringIO()
    Console(file=buf, width=80).print(markup)
    return buf.getvalue().rstrip("\n")


@pytest.mark.parametrize("sport,book", LONGS)
def test_la_ligne_de_succes_est_lue_par_la_sonde(sport, book):
    rendu = _rendu(ligne_book(sport, book, 15.1, 1234))
    m = RE_OK.match(rendu.strip())
    assert m, f"la regex ne matche pas : {rendu!r}"
    assert m.group(1) == sport
    assert int(m.group(2)) == 1234
    assert m.group(3) == book
    assert float(m.group(4)) == pytest.approx(15.1)


@pytest.mark.parametrize("sport,book", LONGS)
def test_la_ligne_d_echec_est_lue_par_la_sonde(sport, book):
    rendu = _rendu(ligne_book(sport, book, 3.2, 0, erreur="HTTP 403"))
    m = RE_KO.match(rendu.strip())
    assert m, f"la regex ne matche pas : {rendu!r}"
    assert m.group(2) == book
    assert float(m.group(3)) == pytest.approx(3.2)


@pytest.mark.parametrize("sport,book", LONGS)
def test_aucune_ligne_ne_depasse_80_colonnes(sport, book):
    """Une seule ligne, jamais deux — sinon la regex perd la moitié."""
    for markup in (ligne_book(sport, book, 999.9, 99999),
                   ligne_book(sport, book, 999.9, 0, erreur="HTTP 403")):
        rendu = _rendu(markup)
        assert "\n" not in rendu, f"enveloppée par rich : {rendu!r}"
        assert len(rendu) <= 80, f"{len(rendu)} colonnes : {rendu!r}"


def test_un_book_a_zero_cote_est_quand_meme_imprime():
    """« pas branché » et « branché mais vide » se ressemblaient trait pour
    trait : la ligne ne s'imprimait qu'avec des cotes. Un book muet qui coûte
    20 s est précisément ce qu'on cherche."""
    m = RE_OK.match(_rendu(ligne_book("soccer", "Circus", 20.4, 0)).strip())
    assert m and int(m.group(2)) == 0
    assert float(m.group(4)) == pytest.approx(20.4)


def test_une_erreur_a_deux_points_ne_casse_pas_la_lecture():
    """Les messages d'exception contiennent des `:` et des chiffres suivis de
    « s » — la regex d'échec doit s'ancrer sur la durée, pas sur le message."""
    rendu = _rendu(ligne_book("soccer", "Betano", 7.5, 0,
                              erreur="timeout: 30s elapsed: read"))
    m = RE_KO.match(rendu.strip())
    assert m and float(m.group(3)) == pytest.approx(7.5)
