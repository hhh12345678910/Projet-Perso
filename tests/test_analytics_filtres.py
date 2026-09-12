"""Les filtres — validés AVANT de toucher la base, jamais concaténés dedans.

POURQUOI CE FICHIER
-------------------
Une borne absurde qui passe rend zéro ligne, et zéro ligne se lit « aucun pari
n'est rentable » au lieu de « ta bande est à l'envers ». Une date mal écrite
filtre tout ou rien selon la comparaison de chaînes, en silence. Les deux sont
le mode de panne dominant du projet (§11) : ces tests sont la barrière.
"""
from __future__ import annotations

import pytest

from src.analytics.filtres import (ALIAS_BOOKS, BOOKS_CONNUS, Filtres,
                                   FiltreInvalide)
from src.analytics.populations import Population


# ── Dates ────────────────────────────────────────────────────────────

def test_une_date_valide_est_conservee_telle_quelle():
    f = Filtres(date_from="2026-08-01", date_to="2026-09-12").valider()
    assert f.date_from == "2026-08-01" and f.date_to == "2026-09-12"


@pytest.mark.parametrize("brut", ["01/08/2026", "2026-13-01", "aout", "x",
                                  "2026-08-32", "20260801", "2026/08/01"])
def test_une_date_invalide_dit_le_format(brut):
    with pytest.raises(FiltreInvalide) as e:
        Filtres(date_from=brut).valider()
    assert "AAAA-MM-JJ" in str(e.value)
    assert "2026-08-01" in str(e.value), "l'exemple manque"


def test_une_periode_a_l_envers_est_refusee():
    with pytest.raises(FiltreInvalide) as e:
        Filtres(date_from="2026-09-12", date_to="2026-08-01").valider()
    assert "vide par construction" in str(e.value)


def test_la_borne_haute_couvre_la_JOURNEE_ENTIERE():
    """⚠️ Couper à `date_to` comparerait « 2026-09-12T23:59 » à « 2026-09-12 »
    et jetterait la dernière journée en silence."""
    f = Filtres(date_to="2026-09-12").valider()
    assert f.borne_haute_exclusive() == "2026-09-13"


def test_sans_date_haute_il_n_y_a_pas_de_borne():
    assert Filtres().valider().borne_haute_exclusive() is None


def test_la_borne_haute_passe_un_changement_de_mois():
    assert Filtres(date_to="2026-08-31").valider().borne_haute_exclusive() \
        == "2026-09-01"


# ── Cotes ────────────────────────────────────────────────────────────

def test_une_bande_de_cotes_a_l_envers_est_refusee():
    with pytest.raises(FiltreInvalide) as e:
        Filtres(cote_min=4.0, cote_max=2.0).valider()
    assert "à l'envers" in str(e.value)


@pytest.mark.parametrize("v", [1.0, 0.5, 0.0, -2.0])
def test_une_cote_sous_1_est_refusee(v):
    """Une cote décimale vaut TOUJOURS plus que 1 : sous ce seuil, le filtre
    ne décrit aucun pari possible."""
    with pytest.raises(FiltreInvalide) as e:
        Filtres(cote_min=v).valider()
    assert "1,00" in str(e.value)


def test_une_seule_borne_de_cote_est_admise():
    f = Filtres(cote_min=2.0).valider()
    assert f.cote_min == 2.0 and f.cote_max is None


# ── EV et délai ──────────────────────────────────────────────────────

def test_une_bande_d_ev_a_l_envers_est_refusee():
    with pytest.raises(FiltreInvalide):
        Filtres(ev_min=20, ev_max=5).valider()


def test_une_ev_negative_est_ADMISE():
    """Une EV négative existe : c'est une détection qui s'est retournée. La
    refuser interdirait d'analyser précisément les cas qui intéressent."""
    assert Filtres(ev_min=-10, ev_max=0).valider().ev_min == -10


def test_une_bande_de_delai_a_l_envers_est_refusee():
    with pytest.raises(FiltreInvalide):
        Filtres(delai_min_h=48, delai_max_h=2).valider()


def test_un_delai_negatif_est_ADMIS():
    """Un délai négatif est une détection LIVE. La bande existe pour que leur
    présence SE VOIE, pas pour être interdite."""
    assert Filtres(delai_min_h=-5, delai_max_h=0).valider().delai_min_h == -5


# ── Books ────────────────────────────────────────────────────────────

def test_un_book_inconnu_est_refuse_et_la_liste_est_donnee():
    """⚠️ L'ignorer en silence rendrait un lot amputé sous un en-tête normal."""
    with pytest.raises(FiltreInvalide) as e:
        Filtres(books=("pinaccle",)).valider()
    assert "inconnu" in str(e.value)
    assert "unibet_be" in str(e.value), "la liste des books connus manque"


def test_l_alias_kambi_se_deplie_en_quatre_books():
    f = Filtres(books=("kambi",)).valider()
    assert set(f.books) == set(ALIAS_BOOKS["kambi"])
    assert len(f.books) == 4, "les quatre books Kambi servent des prix identiques"


def test_l_alias_ne_duplique_pas_un_book_deja_nomme():
    f = Filtres(books=("kambi", "unibet_be")).valider()
    assert len(f.books) == len(set(f.books))


def test_la_casse_du_book_est_normalisee():
    assert Filtres(books=("UNIBET_BE", " ladbrokes_be ")).valider().books \
        == ("unibet_be", "ladbrokes_be")


def test_tous_les_books_de_l_enum_sont_acceptes():
    """La liste des connus vient de `models.Book`, jamais d'une copie : un
    book ajouté au moteur est acceptable ici sans que personne n'y touche."""
    assert Filtres(books=tuple(BOOKS_CONNUS)).valider().books


# ── Marchés, sports, ligues ──────────────────────────────────────────

def test_un_marche_inconnu_est_refuse():
    with pytest.raises(FiltreInvalide) as e:
        Filtres(markets=("corners",)).valider()
    assert "Marché inconnu" in str(e.value)


@pytest.mark.parametrize("m", ["h2h", "totals", "handicap", "btts",
                               "h2h_h1", "totals_h1"])
def test_tous_les_marches_du_moteur_sont_acceptes(m):
    assert Filtres(markets=(m,)).valider().markets == (m,)


def test_les_sports_sont_un_ensemble_OUVERT():
    """Les sports viennent des données, pas d'une énumération : en refuser un
    inconnu empêcherait d'analyser un sport que le moteur vient d'ajouter."""
    assert Filtres(sports=("padel",)).valider().sports == ("padel",)


def test_les_sports_sont_normalises_en_minuscules():
    assert Filtres(sports=("Soccer", "TENNIS")).valider().sports \
        == ("soccer", "tennis")


def test_les_ligues_gardent_leur_casse():
    """Une ligue est comparée telle quelle à `events.league`, qui porte des
    majuscules. La normaliser ne rapprocherait plus rien."""
    assert Filtres(leagues=("Jupiler Pro League",)).valider().leagues \
        == ("Jupiler Pro League",)


# ── Population et joué ───────────────────────────────────────────────

@pytest.mark.parametrize("p", list(Population))
def test_chaque_population_est_acceptee(p):
    assert Filtres(population=p).valider().population is p


def test_une_population_inconnue_est_refusee():
    with pytest.raises(ValueError):
        Filtres(population="inventee").valider()


@pytest.mark.parametrize("v", ["tous", "oui", "non"])
def test_les_trois_valeurs_de_joue(v):
    assert Filtres(joue=v).valider().joue == v


def test_une_valeur_de_joue_inconnue_est_refusee():
    with pytest.raises(FiltreInvalide) as e:
        Filtres(joue="peut-etre").valider()
    assert "tous" in str(e.value)


# ── Mise ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("v", [0, -5])
def test_une_mise_non_positive_est_refusee(v):
    """Le ROI divise par la mise : zéro donnerait une division par zéro ou un
    ROI infini, et négative inverserait le signe de tout le tableau."""
    with pytest.raises(FiltreInvalide):
        Filtres(stake=v).valider()


def test_la_mise_voyage_avec_les_filtres():
    """Un ROI dont on ignore la mise n'est pas reproductible."""
    assert Filtres(stake=10).valider().en_dict()["stake"] == 10


# ── Sérialisation — la base de « enregistrer cette analyse » ─────────

def test_l_aller_retour_json_conserve_tout():
    f = Filtres(sports=("soccer",), books=("unibet_be", "ladbrokes_be"),
                markets=("h2h",), leagues=("Jupiler Pro League",),
                cote_min=2.0, cote_max=4.0, ev_min=10, ev_max=20,
                date_from="2026-08-01", date_to="2026-09-12",
                delai_min_h=2, delai_max_h=48,
                population=Population.SETTLED, joue="oui", stake=50).valider()
    refait = Filtres.depuis_dict(f.en_dict()).valider()
    assert refait == f


def test_le_dict_est_du_json_pur():
    """Pas d'Enum, pas de tuple, pas de datetime : c'est ce qui rendra
    « enregistrer cette analyse » trivial."""
    import json
    d = Filtres(population=Population.SENT, books=("kambi",)).valider().en_dict()
    assert json.loads(json.dumps(d)) == d
    assert isinstance(d["population"], str)
    assert isinstance(d["bookmakers"], list)


def test_depuis_dict_accepte_les_noms_de_l_API():
    f = Filtres.depuis_dict({
        "sports[]": ["soccer", "tennis"], "bookmakers[]": ["unibet_be"],
        "odds_min": 2, "odds_max": 4, "ev_min": 5, "ev_max": 20,
        "date_from": "2026-08-01", "date_to": "2026-09-12",
        "population": "settled", "played": "oui",
        "delay_min": 2, "delay_max": 24,
    }).valider()
    assert f.sports == ("soccer", "tennis") and f.books == ("unibet_be",)
    assert f.cote_min == 2 and f.delai_max_h == 24
    assert f.population is Population.SETTLED and f.joue == "oui"


def test_depuis_dict_accepte_le_SINGULIER_comme_le_pluriel():
    """Les deux arrivent réellement d'une query string ; en refuser un serait
    un piège pour rien."""
    assert Filtres.depuis_dict({"sport": "soccer"}).valider().sports \
        == ("soccer",)
    assert Filtres.depuis_dict({"market": "h2h"}).valider().markets == ("h2h",)


def test_une_liste_separee_par_des_virgules_est_acceptee():
    f = Filtres.depuis_dict({"bookmakers": "unibet_be,ladbrokes_be"}).valider()
    assert f.books == ("unibet_be", "ladbrokes_be")


def test_un_dict_vide_donne_les_valeurs_par_defaut():
    f = Filtres.depuis_dict({}).valider()
    assert f.population is Population.DETECTED and f.joue == "tous"
    assert f.sports == () and f.books == ()


def test_les_filtres_sont_immuables():
    """Deux découpes d'une même analyse partent du même objet : l'une ne doit
    pas pouvoir le modifier sous les pieds de l'autre."""
    import dataclasses
    f = Filtres().valider()
    with pytest.raises(dataclasses.FrozenInstanceError):
        f.cote_min = 2.0
