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
import re
import time

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


# ── Les lignes de phases : mêmes deux pièges que les lignes de book ─────────

class _ChronoFige:
    """Un `Chrono` aux valeurs imposées, pour rendre la ligne sans dormir."""

    def __init__(self, par_phase: dict, total: float):
        self.par_phase, self._t = par_phase, total

    def total(self) -> float:
        return self._t

    def reste(self) -> float:
        return self._t - sum(self.par_phase.values())


@pytest.mark.parametrize("sport", ["volleyball", "basketball", "soccer"])
def test_la_ligne_de_phases_est_lue_par_la_sonde(sport):
    from scripts.book_latency import RE_PHASES
    from src.orchestration import ligne_phases

    ch = _ChronoFige({"fetch": 12.0, "base": 8.0, "fair": 1.0, "marques": 0.5}, 26.0)
    rendu = _rendu(ligne_phases(sport, ch))
    assert "\n" not in rendu, f"enveloppée par rich : {rendu!r}"
    assert len(rendu) <= 80, f"{len(rendu)} colonnes : {rendu!r}"
    m = RE_PHASES.match(rendu.strip())
    assert m, f"la regex ne matche pas : {rendu!r}"
    assert m.group(1) == sport
    assert float(m.group(3)) == pytest.approx(26.0)
    paires = dict(re.findall(r"([a-zéè]+) ([\d.]+)", m.group(2)))
    vus = {k: float(v) for k, v in paires.items()}
    # `marques` est sous le seuil d'impression de 0,05 s ? Non — 0,5 s. Mais
    # les noms sont ABRÉGÉS : la sonde lit ce qui est imprimé, pas les clés.
    assert vus["fetch"] == 12.0 and vus["base"] == 8.0
    assert vus["reste"] == pytest.approx(4.5)


@pytest.mark.parametrize("sport", ["volleyball", "soccer"])
def test_la_ligne_tient_sous_80_colonnes_avec_NEUF_phases(sport):
    """Le piège qui a réellement mordu le 04/09 : ajouter une phase a fait
    dépasser 80 colonnes, `rich` a coupé la ligne, `tot` est passé à la ligne
    suivante — et le tableau des phases est devenu incohérent avec le journal
    SANS RIEN DIRE. Neuf phases, c'est ce que la production imprime."""
    from scripts.book_latency import RE_PHASES as R
    from src.orchestration import ligne_phases
    ch = _ChronoFige({"fetch": 123.4, "base": 98.7, "fair": 45.6,
                      "retards": 33.2, "marques": 12.1, "clv_alertes": 95.2,
                      "detection": 12.1, "surebets": 2.4, "middles": 1.1}, 999.9)
    rendu = _rendu(ligne_phases(sport, ch))
    assert "\n" not in rendu, f"ENVELOPPÉE : {rendu!r}"
    assert len(rendu) <= 80, f"{len(rendu)} colonnes : {rendu!r}"
    assert R.match(rendu.strip()), f"illisible à la sonde : {rendu!r}"
    assert "tot 999.9s" in rendu, "le total doit survivre à la troncature"
    assert "+" in rendu, "les phases omises doivent être ANNONCÉES, pas tues"


def test_une_phase_negligeable_n_est_pas_imprimee():
    """Sous 0,05 s une phase n'apprend rien et vole de la place à celles qui
    comptent. La sonde traite une phase absente comme nulle."""
    from src.orchestration import ligne_phases
    ch = _ChronoFige({"fetch": 10.0, "marques": 0.0, "retards": 0.001}, 12.0)
    rendu = _rendu(ligne_phases("soccer", ch))
    assert "marq" not in rendu and "retd" not in rendu
    assert "fetch 10.0" in rendu


def test_un_chrono_vide_reste_lisible():
    """Sortie anticipée : aucune phase, mais `reste` et `tot` doivent rester."""
    from scripts.book_latency import RE_PHASES as R
    from src.orchestration import ligne_phases
    rendu = _rendu(ligne_phases("soccer", _ChronoFige({}, 3.0)))
    assert R.match(rendu.strip()) and "tot 3.0s" in rendu


def test_le_chrono_cumule_les_passages_d_une_meme_phase():
    """Trois `insert_quotes_sparse` par scan : c'est leur TOTAL qui compte."""
    from src.orchestration import Chrono

    ch = Chrono()
    for _ in range(3):
        with ch("base"):
            time.sleep(0.02)
    assert ch.par_phase["base"] == pytest.approx(0.06, abs=0.03)
    assert len(ch.par_phase) == 1


def test_une_phase_qui_echoue_compte_quand_meme_son_temps():
    """Une phase qui échoue LENTEMENT est précisément ce qu'on cherche : si
    l'exception effaçait sa durée, le coupable serait invisible."""
    from src.orchestration import Chrono

    ch = Chrono()
    with pytest.raises(ValueError):
        with ch("fair"):
            time.sleep(0.03)
            raise ValueError("boom")
    assert ch.par_phase["fair"] >= 0.02


def test_le_reste_ne_devient_jamais_negatif():
    """Des phases imbriquées double-compteraient et rendraient `reste` négatif
    — un chiffre absurde vaut mieux caché derrière un plancher que servi."""
    from src.orchestration import Chrono

    ch = Chrono()
    ch.par_phase["a"] = 1000.0
    assert ch.reste() == 0.0
