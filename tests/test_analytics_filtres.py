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
    """Deux books SANS jumeau, pour que ce test ne parle que de la casse :
    un jumeau Kambi en déplierait quatre et masquerait ce qu'on vérifie."""
    assert Filtres(books=("BETANO_BE", " ladbrokes_be ")).valider().books \
        == ("betano_be", "ladbrokes_be")


# ── Les jumeaux Kambi : un coché en déplie quatre ────────────────────
#
# Unibet, 711, Bingoal et Scooore servent un seul flux Kambi et cotent à
# l'identique. Les garder séparés faisait dépendre le résultat du jumeau
# coché — alors que le prix est le même — et éclatait les effectifs en quatre
# lots trop petits pour conclure quoi que ce soit.

def test_scooore_et_unibet_rendent_le_MEME_lot():
    """⚠️ LA FORME FORTE DE L'EXIGENCE, SANS DÉPENDRE DU CODE NEUF.

    Les noms sont écrits en clair et rien n'est importé du correctif : ce
    test tombe donc sur le COMPORTEMENT — avant, `scooore_be` rendait
    `('scooore_be',)` et `unibet_be` rendait `('unibet_be',)`, deux lots
    différents pour un prix identique."""
    assert (set(Filtres(books=("scooore_be",)).valider().books)
            == set(Filtres(books=("unibet_be",)).valider().books)
            == set(Filtres(books=("seven_eleven_be",)).valider().books)
            == set(Filtres(books=("bingoal_be",)).valider().books))


def test_cocher_UN_jumeau_les_deplie_TOUS():
    from src.analytics.perimetre import GROUPES_JUMEAUX
    attendu = set(GROUPES_JUMEAUX[0])
    assert set(Filtres(books=("scooore_be",)).valider().books) == attendu


def test_NIMPORTE_LEQUEL_des_jumeaux_donne_le_MEME_lot():
    """⚠️ L'EXIGENCE, EXPRIMÉE TELLE QUELLE : choisir Scooore, 711, Bingoal
    ou Unibet doit rendre rigoureusement la même chose."""
    from src.analytics.perimetre import GROUPES_JUMEAUX
    lots = {frozenset(Filtres(books=(b,)).valider().books)
            for b in GROUPES_JUMEAUX[0]}
    assert len(lots) == 1, "le résultat dépend encore du jumeau coché"


def test_lalias_kambi_donne_exactement_la_meme_chose():
    assert (set(Filtres(books=("kambi",)).valider().books)
            == set(Filtres(books=("bingoal_be",)).valider().books))


def test_un_book_SANS_jumeau_nest_pas_deplie():
    assert Filtres(books=("ladbrokes_be",)).valider().books == ("ladbrokes_be",)


def test_le_groupe_vient_de_reference_pas_dune_recopie():
    """Une seconde liste divergerait le jour où un cinquième jumeau
    apparaît — c'est ce que `src/reference.py` interdit explicitement."""
    from src.analytics.perimetre import GROUPES_JUMEAUX
    from src.reference import KAMBI_BOOKS
    assert GROUPES_JUMEAUX[0] == tuple(b.value for b in KAMBI_BOOKS)


def test_tous_les_books_de_l_enum_sont_acceptes():
    """La liste des connus vient de `models.Book`, jamais d'une copie : un
    book ajouté au moteur est acceptable ici sans que personne n'y touche."""
    assert Filtres(books=tuple(BOOKS_CONNUS)).valider().books


# ── Marchés, sports, ligues ──────────────────────────────────────────

def test_un_marche_inconnu_est_refuse():
    with pytest.raises(FiltreInvalide) as e:
        Filtres(markets=("corners",)).valider()
    assert "Marché inconnu" in str(e.value)


@pytest.mark.parametrize("m", ["h2h", "totals"])
def test_les_marches_du_perimetre_sont_acceptes(m):
    assert Filtres(markets=(m,)).valider().markets == (m,)


@pytest.mark.parametrize("m", ["handicap", "btts", "h2h_h1", "totals_h1"])
def test_un_marche_hors_perimetre_est_REFUSE_et_non_ignore(m):
    """PHASE 4 — le contrat a changé, et le refus est le point.

    Ces marchés existent bien dans le moteur, et `Filtres` les connaît : ce
    n'est pas une faute de frappe qu'on rejette. C'est que `clv.settle` ne
    sait régler ni un handicap, ni un BTTS, ni une mi-temps — ils ressortent
    NON RÉGLÉS quel que soit le score. Les accepter en silence rendrait un lot
    dont le ROI est structurellement vide sous un en-tête normal.

    Le message doit nommer le périmètre, sinon l'utilisateur ne sait pas quoi
    corriger."""
    with pytest.raises(FiltreInvalide) as e:
        Filtres(markets=(m,)).valider()
    assert "périmètre" in str(e.value)
    assert "h2h" in str(e.value) and "totals" in str(e.value)


def test_les_sports_sont_un_ensemble_FERME_par_le_perimetre():
    """PHASE 4 — les sports ne sont plus un ensemble ouvert.

    Avant, un sport inconnu passait en paramètre et ne rapprochait rien. Le
    périmètre Analytics le REFUSE désormais : aucune source de résultats
    n'existe hors football et tennis, donc aucun ROI ne peut en sortir.
    Rendre zéro ligne aurait laissé croire à une absence de données ; le refus
    dit la vraie raison."""
    for sport in ("padel", "basketball", "hockey", "volleyball", "unknown"):
        with pytest.raises(FiltreInvalide) as e:
            Filtres(sports=(sport,)).valider()
        assert "périmètre" in str(e.value)


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
        "sports[]": ["soccer", "tennis"], "bookmakers[]": ["betano_be"],
        "odds_min": 2, "odds_max": 4, "ev_min": 5, "ev_max": 20,
        "date_from": "2026-08-01", "date_to": "2026-09-12",
        "population": "settled", "played": "oui",
        "delay_min": 2, "delay_max": 24,
    }).valider()
    assert f.sports == ("soccer", "tennis") and f.books == ("betano_be",)
    assert f.cote_min == 2 and f.delai_max_h == 24
    assert f.population is Population.SETTLED and f.joue == "oui"


def test_depuis_dict_accepte_le_SINGULIER_comme_le_pluriel():
    """Les deux arrivent réellement d'une query string ; en refuser un serait
    un piège pour rien."""
    assert Filtres.depuis_dict({"sport": "soccer"}).valider().sports \
        == ("soccer",)
    assert Filtres.depuis_dict({"market": "h2h"}).valider().markets == ("h2h",)


def test_une_liste_separee_par_des_virgules_est_acceptee():
    f = Filtres.depuis_dict({"bookmakers": "betano_be,ladbrokes_be"}).valider()
    assert f.books == ("betano_be", "ladbrokes_be")


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


# ── Bandes de cote : validation ──────────────────────────────────────

def test_une_bande_de_cote_inconnue_est_refusee():
    with pytest.raises(FiltreInvalide) as e:
        Filtres(cote_bandes=("2.0-2.5",)).valider()
    assert "Bande de cote inconnue" in str(e.value)


def test_les_bandes_de_cote_sont_remises_dans_lordre_canonique():
    """Deux sélections équivalentes doivent produire le même `Filtres`, donc
    la même clé si l'analyse est un jour mise en cache."""
    f = Filtres(cote_bandes=("> 6.0", "1.0-1.8")).valider()
    assert f.cote_bandes == ("1.0-1.8", "> 6.0")


def test_un_sport_hors_perimetre_est_refuse_sur_la_cote():
    with pytest.raises(FiltreInvalide) as e:
        Filtres(cote_par_sport=(("basketball", ("1.8-2.3",)),)).valider()
    assert "hors périmètre" in str(e.value)


def test_un_sport_en_double_est_refuse_sur_la_cote():
    with pytest.raises(FiltreInvalide) as e:
        Filtres(cote_par_sport=(("soccer", ("1.8-2.3",)),
                                ("soccer", ("> 6.0",)))).valider()
    assert "apparaît deux fois" in str(e.value)


def test_une_regle_de_cote_VIDE_est_retiree_pas_conservee():
    """« Aucune bande cochée pour le tennis » veut dire « pas de règle propre
    au tennis », et non « aucune opportunité de tennis »."""
    f = Filtres(cote_par_sport=(("tennis", ()),)).valider()
    assert f.cote_par_sport == ()


def test_les_bandes_de_cote_font_laller_retour_JSON():
    f = Filtres(cote_bandes=("1.8-2.3",),
                cote_par_sport=(("tennis", ("> 6.0",)),)).valider()
    r = Filtres.depuis_dict(f.en_dict()).valider()
    assert r.cote_bandes == f.cote_bandes
    assert r.cote_par_sport == f.cote_par_sport


def test_la_query_string_porte_les_bandes_de_cote_par_sport():
    f = Filtres.depuis_dict({"odds_bands": "1.8-2.3",
                             "odds_bands_tennis": ["> 6.0"]}).valider()
    assert f.cote_bandes == ("1.8-2.3",)
    assert f.cote_par_sport == (("tennis", ("> 6.0",)),)


# ── Bornes LIBRES d'EV par sport ─────────────────────────────────────

def test_les_bornes_libres_dev_par_sport_sont_validees():
    f = Filtres(ev_libre_par_sport=(("soccer", (5, 15)),
                                    ("tennis", (25, None)))).valider()
    assert f.ev_libre_par_sport == (("soccer", (5.0, 15.0)),
                                    ("tennis", (25.0, None)))


def test_une_borne_dev_a_lenvers_est_refusee():
    with pytest.raises(FiltreInvalide) as e:
        Filtres(ev_libre_par_sport=(("soccer", (20, 5)),)).valider()
    assert "au-dessus de la haute" in str(e.value)


def test_un_sport_hors_perimetre_est_refuse_sur_lev_libre():
    with pytest.raises(FiltreInvalide):
        Filtres(ev_libre_par_sport=(("basketball", (5, 15)),)).valider()


def test_deux_bornes_vides_retirent_la_regle():
    """Une règle qui ne filtre rien ferait perdre au sport la règle GLOBALE
    dont il devrait hériter."""
    assert Filtres(ev_libre_par_sport=(("soccer", (None, None)),)
                   ).valider().ev_libre_par_sport == ()


def test_la_query_string_porte_ev_min_et_ev_max_par_sport():
    f = Filtres.depuis_dict({"ev_min_soccer": 5, "ev_max_soccer": 15,
                             "ev_min_tennis": 25}).valider()
    assert f.ev_libre_par_sport == (("soccer", (5.0, 15.0)),
                                    ("tennis", (25.0, None)))


def test_ev_min_GLOBAL_nest_pas_pris_pour_un_sport():
    """⚠️ `ev_min` nu est la borne globale. La lire comme le sport « » la
    ferait disparaître de la règle globale — un filtre qui se déplace tout
    seul est pire qu'un filtre absent."""
    f = Filtres.depuis_dict({"ev_min": 8, "ev_max": 20}).valider()
    assert f.ev_min == 8.0 and f.ev_max == 20.0
    assert f.ev_libre_par_sport == ()


def test_les_bornes_libres_par_sport_font_laller_retour_JSON():
    f = Filtres(ev_libre_par_sport=(("soccer", (5, 15)),)).valider()
    assert (Filtres.depuis_dict(f.en_dict()).valider().ev_libre_par_sport
            == f.ev_libre_par_sport)
