"""Le routage : quels canaux reçoivent ce pari.

`src/routing.py` est une fonction pure — elle décide, elle n'envoie rien et
ne lit rien. Ces tests la couvrent dimension par dimension, puis vérifient
les deux propriétés qui font d'elle une fonction : déterminisme et absence
d'effet de bord.

Deux garde-fous portent le vrai risque :

  * `test_une_exclusion_n_exclut_pas_sur_une_dimension_inconnue` — écarter
    par défaut supprimerait des paris d'un sport qu'on n'a jamais voulu
    couper. C'est déjà la règle de `premium_hi_sports_exclus`, et la faute
    inverse est silencieuse : personne ne voit un pari qui n'arrive pas ;
  * `test_un_canal_sans_regle_ne_prend_rien` — un canal fraîchement créé,
    pas encore configuré, ne doit pas déverser le flux entier.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from src.routing import Borne, Canal, Critere, Regle, canaux_pour


@dataclass(frozen=True)
class _Pari:
    """Le module route en canard : il ne lit que ces quatre attributs. Un
    faux pari suffit donc, et le test ne dépend pas de `models`."""
    ev_pct: float = 10.0
    odd_taken: float = 2.0
    book: str = "unibet_be"
    market: str = "h2h"


def _canal(nom="C", chat="CHAT", **kw) -> Canal:
    kw.setdefault("regles", (Regle(),))
    return Canal(chat_id=chat, nom=nom, **kw)


def _noms(bet, canaux, **kw) -> list[str]:
    return [c.nom for c in canaux_pour(bet, canaux=canaux, **kw)]


# ══ le cardinal du resultat ════════════════════════════════════════════
def test_aucun_canal_correspondant():
    c = _canal(regles=(Regle(ev_min=50.0),))
    assert canaux_pour(_Pari(ev_pct=10.0), canaux=[c]) == []


def test_aucun_canal_du_tout():
    assert canaux_pour(_Pari(), canaux=[]) == []


def test_un_seul_canal_correspondant():
    oui = _canal("OUI", regles=(Regle(ev_min=5.0),))
    non = _canal("NON", chat="B", regles=(Regle(ev_min=50.0),))
    assert _noms(_Pari(ev_pct=10.0), [oui, non]) == ["OUI"]


def test_plusieurs_canaux_pour_le_meme_pari():
    """Le besoin central : une opportunité, plusieurs destinations."""
    tennis = _canal("TENNIS", chat="T", regles=(Regle(
        ev_min=10.0, odd_max=4.0,
        criteres=(Critere("sport", frozenset({"tennis"})),)),))
    grosses = _canal("GROSSES_EV", chat="G", regles=(Regle(ev_min=20.0),))
    unibet = _canal("UNIBET", chat="U", regles=(Regle(
        ev_min=10.0,
        criteres=(Critere("book", frozenset({"unibet_be"})),)),))
    pari = _Pari(ev_pct=25.0, odd_taken=3.0, book="unibet_be")
    assert _noms(pari, [tennis, grosses, unibet], sport="tennis") == [
        "GROSSES_EV", "TENNIS", "UNIBET"]


# ══ ET / OU ════════════════════════════════════════════════════════════
def test_deux_regles_dans_un_canal_font_un_OU():
    c = _canal(regles=(Regle(ev_min=35.0),
                       Regle(ev_min=20.0, odd_min=Borne(4.0, stricte=True))))
    assert _noms(_Pari(ev_pct=40.0, odd_taken=2.0), [c]) == ["C"]   # 1re regle
    assert _noms(_Pari(ev_pct=25.0, odd_taken=5.0), [c]) == ["C"]   # 2e regle
    assert _noms(_Pari(ev_pct=25.0, odd_taken=2.0), [c]) == []      # aucune


def test_plusieurs_dimensions_dans_une_regle_font_un_ET():
    c = _canal(regles=(Regle(criteres=(
        Critere("sport", frozenset({"tennis"})),
        Critere("book", frozenset({"unibet_be"})),
        Critere("market", frozenset({"h2h"})),
    )),))
    ok = dict(sport="tennis")
    assert _noms(_Pari(book="unibet_be", market="h2h"), [c], **ok) == ["C"]
    assert _noms(_Pari(book="betano_be", market="h2h"), [c], **ok) == []
    assert _noms(_Pari(book="unibet_be", market="totals"), [c], **ok) == []
    assert _noms(_Pari(book="unibet_be", market="h2h"), [c], sport="soccer") == []


def test_plusieurs_valeurs_dans_une_dimension_font_un_OU():
    c = _canal(regles=(Regle(criteres=(
        Critere("book", frozenset({"unibet_be", "starcasino_sport"})),)),))
    assert _noms(_Pari(book="unibet_be"), [c]) == ["C"]
    assert _noms(_Pari(book="starcasino_sport"), [c]) == ["C"]
    assert _noms(_Pari(book="betano_be"), [c]) == []


def test_deux_criteres_sur_la_meme_dimension_sont_refuses():
    """« sport=tennis ET sport=soccer » ne peut jamais passer : la structure
    trahit une intention de OU, qui s'écrit avec plusieurs valeurs."""
    with pytest.raises(ValueError, match="même dimension"):
        Regle(criteres=(Critere("sport", frozenset({"tennis"})),
                        Critere("sport", frozenset({"soccer"}))))


# ══ les quatre dimensions ══════════════════════════════════════════════
def test_dimension_sport():
    c = _canal(regles=(Regle(criteres=(Critere("sport", frozenset({"tennis"})),)),))
    assert _noms(_Pari(), [c], sport="tennis") == ["C"]
    assert _noms(_Pari(), [c], sport="soccer") == []


def test_dimension_book():
    c = _canal(regles=(Regle(criteres=(Critere("book", frozenset({"unibet_be"})),)),))
    assert _noms(_Pari(book="unibet_be"), [c]) == ["C"]
    assert _noms(_Pari(book="betano_be"), [c]) == []


def test_dimension_market():
    c = _canal(regles=(Regle(criteres=(Critere("market", frozenset({"totals"})),)),))
    assert _noms(_Pari(market="totals"), [c]) == ["C"]
    assert _noms(_Pari(market="h2h"), [c]) == []


def test_dimension_league():
    c = _canal(regles=(Regle(criteres=(
        Critere("league", frozenset({"Suisse - Super League"})),)),))
    assert _noms(_Pari(), [c], league="Suisse - Super League") == ["C"]
    assert _noms(_Pari(), [c], league="Belgique - Pro League") == []


def test_une_dimension_inconnue_est_refusee_a_la_construction():
    with pytest.raises(ValueError, match="dimension inconnue"):
        Critere("competition", frozenset({"x"}))


def test_un_critere_sans_valeur_est_refuse():
    with pytest.raises(ValueError, match="sans valeur"):
        Critere("sport", frozenset())


def test_la_comparaison_ignore_la_casse_et_les_espaces():
    c = _canal(regles=(Regle(criteres=(Critere("sport", frozenset({" Tennis "})),)),))
    assert _noms(_Pari(), [c], sport="TENNIS") == ["C"]


def test_un_enum_est_lu_par_sa_valeur():
    """Book et MarketType sont des `str, Enum`. Le module ne les importe pas
    — il lit `.value` par getattr."""
    from src.models import Book, MarketType
    c = _canal(regles=(Regle(criteres=(
        Critere("book", frozenset({"unibet_be"})),
        Critere("market", frozenset({"h2h"})),
    )),))
    pari = _Pari(book=Book.UNIBET_BE, market=MarketType.H2H)
    assert _noms(pari, [c]) == ["C"]


# ══ inclusion / exclusion / dimension absente ══════════════════════════
def test_inclusion():
    c = _canal(regles=(Regle(criteres=(
        Critere("sport", frozenset({"tennis"}), inclut=True),)),))
    assert _noms(_Pari(), [c], sport="tennis") == ["C"]
    assert _noms(_Pari(), [c], sport="soccer") == []


def test_exclusion():
    c = _canal(regles=(Regle(criteres=(
        Critere("sport", frozenset({"tennis"}), inclut=False),)),))
    assert _noms(_Pari(), [c], sport="soccer") == ["C"]
    assert _noms(_Pari(), [c], sport="tennis") == []


def test_dimension_absente_vaut_toutes():
    c = _canal(regles=(Regle(ev_min=5.0),))
    for sport in ("tennis", "soccer", "basketball", None):
        assert _noms(_Pari(ev_pct=10.0), [c], sport=sport) == ["C"], sport


def test_une_inclusion_ne_passe_pas_sur_une_dimension_inconnue():
    """Un « canal tennis » ne doit pas accepter un pari dont on ignore le
    sport : l'inclusion est une exigence positive, non satisfaite."""
    c = _canal(regles=(Regle(criteres=(Critere("sport", frozenset({"tennis"})),)),))
    assert _noms(_Pari(), [c], sport=None) == []


def test_une_exclusion_n_exclut_pas_sur_une_dimension_inconnue():
    """L'autre moitié de l'asymétrie, et la plus coûteuse à rater : écarter
    sur une donnée absente supprimerait des paris d'un sport qu'on n'a
    jamais voulu couper, sans que rien ne le signale."""
    c = _canal(regles=(Regle(criteres=(
        Critere("sport", frozenset({"tennis"}), inclut=False),)),))
    assert _noms(_Pari(), [c], sport=None) == ["C"]
    assert _noms(_Pari(), [c], league=None) == ["C"]


# ══ les bornes numeriques ══════════════════════════════════════════════
def test_ev_min_et_max():
    c = _canal(regles=(Regle(ev_min=5.0, ev_max=8.0),))
    assert _noms(_Pari(ev_pct=4.9), [c]) == []
    assert _noms(_Pari(ev_pct=5.0), [c]) == ["C"]    # inclusive
    assert _noms(_Pari(ev_pct=8.0), [c]) == ["C"]    # inclusive
    assert _noms(_Pari(ev_pct=8.1), [c]) == []


def test_cote_min_et_max():
    c = _canal(regles=(Regle(odd_min=1.5, odd_max=4.0),))
    assert _noms(_Pari(odd_taken=1.49), [c]) == []
    assert _noms(_Pari(odd_taken=1.50), [c]) == ["C"]
    assert _noms(_Pari(odd_taken=4.00), [c]) == ["C"]
    assert _noms(_Pari(odd_taken=4.01), [c]) == []


def test_une_borne_stricte_exclut_la_valeur_pile():
    """La configuration réelle en a besoin des deux côtés : le canal
    principal s'arrête à `EV < 8` tandis que sa bande de cote inclut 4,00,
    et la voie critique grosses cotes commence à `cote > 4,0`."""
    strict = _canal(regles=(Regle(odd_min=Borne(4.0, stricte=True)),))
    assert _noms(_Pari(odd_taken=4.00), [strict]) == []
    assert _noms(_Pari(odd_taken=4.01), [strict]) == ["C"]

    haut = _canal(regles=(Regle(ev_max=Borne(8.0, stricte=True)),))
    assert _noms(_Pari(ev_pct=7.99), [haut]) == ["C"]
    assert _noms(_Pari(ev_pct=8.00), [haut]) == []


def test_une_borne_nue_est_inclusive():
    assert Borne.coerce(4.0) == Borne(4.0, stricte=False)
    assert Borne.coerce(None) is None
    assert Borne.coerce(Borne(4.0, stricte=True)).stricte is True


# ══ prematch / live ════════════════════════════════════════════════════
def test_phase_prematch():
    c = _canal(regles=(Regle(phase="prematch"),))
    assert _noms(_Pari(), [c], is_live=False) == ["C"]
    assert _noms(_Pari(), [c], is_live=True) == []


def test_phase_live():
    c = _canal(regles=(Regle(phase="live"),))
    assert _noms(_Pari(), [c], is_live=True) == ["C"]
    assert _noms(_Pari(), [c], is_live=False) == []


def test_phase_absente_vaut_les_deux():
    c = _canal(regles=(Regle(),))
    assert _noms(_Pari(), [c], is_live=True) == ["C"]
    assert _noms(_Pari(), [c], is_live=False) == ["C"]


def test_une_phase_inconnue_est_refusee():
    with pytest.raises(ValueError, match="phase inconnue"):
        Regle(phase="mi-temps")


# ══ l'etat du canal ════════════════════════════════════════════════════
def test_un_canal_inactif_est_ignore():
    c = _canal(actif=False, regles=(Regle(),))
    assert canaux_pour(_Pari(), canaux=[c]) == []


def test_un_canal_sans_regle_ne_prend_rien():
    """Un canal créé mais pas encore configuré doit rester muet."""
    assert canaux_pour(_Pari(), canaux=[Canal("CHAT", "NEUF", regles=())]) == []


def test_une_regle_sans_critere_prend_tout():
    """La distinction avec le test précédent : « aucune règle » et « une
    règle sans condition » ne veulent pas dire la même chose."""
    c = _canal(regles=(Regle(),))
    assert _noms(_Pari(ev_pct=-50.0, odd_taken=999.0), [c]) == ["C"]


# ══ ordre, priorite, exclusivite ═══════════════════════════════════════
def test_l_ordre_suit_la_priorite():
    a = _canal("A", chat="a", priorite=10)
    b = _canal("B", chat="b", priorite=5)
    assert _noms(_Pari(), [a, b]) == ["B", "A"]


def test_l_ordre_ne_depend_pas_de_l_ordre_d_entree():
    a = _canal("ALPHA", chat="a")
    b = _canal("BETA", chat="b")
    assert _noms(_Pari(), [a, b]) == _noms(_Pari(), [b, a]) == ["ALPHA", "BETA"]


def test_le_defaut_est_independant():
    """Aucun canal n'est exclusif par défaut : le pari part dans les trois."""
    canaux = [_canal("A", chat="a"), _canal("B", chat="b"), _canal("C2", chat="c")]
    assert _noms(_Pari(), canaux) == ["A", "B", "C2"]


def test_un_canal_exclusif_arrete_les_suivants():
    """Reproduit le débordement actuel : le critique ne reçoit que ce
    qu'aucune bande premium n'a pris."""
    premium = _canal("PREMIUM", chat="p", priorite=10, exclusif=True)
    critique = _canal("CRITIQUE", chat="c", priorite=20)
    assert _noms(_Pari(), [premium, critique]) == ["PREMIUM"]


def test_un_canal_exclusif_qui_ne_prend_pas_n_arrete_personne():
    premium = _canal("PREMIUM", chat="p", priorite=10, exclusif=True,
                     regles=(Regle(ev_min=99.0),))
    critique = _canal("CRITIQUE", chat="c", priorite=20)
    assert _noms(_Pari(ev_pct=10.0), [premium, critique]) == ["CRITIQUE"]


def test_un_canal_exclusif_inactif_n_arrete_personne():
    premium = _canal("PREMIUM", chat="p", priorite=10, exclusif=True, actif=False)
    critique = _canal("CRITIQUE", chat="c", priorite=20)
    assert _noms(_Pari(), [premium, critique]) == ["CRITIQUE"]


def test_profile_id_ne_change_rien():
    """Prévu pour le multi-utilisateur, lu par personne en V1."""
    a = _canal("A", chat="a", profile_id=None)
    b = _canal("B", chat="b", profile_id=42)
    assert _noms(_Pari(), [a, b]) == ["A", "B"]


# ══ fonction pure ══════════════════════════════════════════════════════
def test_resultat_deterministe():
    canaux = [_canal("A", chat="a", regles=(Regle(ev_min=5.0),)),
              _canal("B", chat="b", regles=(Regle(odd_max=3.0),))]
    pari = _Pari(ev_pct=10.0, odd_taken=2.0)
    premier = canaux_pour(pari, canaux=canaux, sport="tennis")
    for _ in range(20):
        assert canaux_pour(pari, canaux=canaux, sport="tennis") == premier


def test_aucun_effet_secondaire():
    """Ni le pari, ni les canaux, ni la liste reçue ne doivent bouger."""
    import copy
    canaux = [_canal("A", chat="a"), _canal("B", chat="b", actif=False)]
    pari = _Pari(ev_pct=10.0, odd_taken=2.0)
    avant_canaux = copy.deepcopy(canaux)
    avant_pari = copy.deepcopy(pari)
    taille = len(canaux)

    canaux_pour(pari, canaux=canaux, sport="tennis")

    assert canaux == avant_canaux
    assert pari == avant_pari
    assert len(canaux) == taille


def test_la_liste_rendue_ne_partage_rien_avec_l_entree():
    """Muter le résultat ne doit pas altérer la configuration."""
    canaux = [_canal("A", chat="a"), _canal("B", chat="b")]
    sortie = canaux_pour(_Pari(), canaux=canaux)
    sortie.clear()
    assert len(canaux) == 2


def test_les_canaux_peuvent_etre_un_iterateur():
    """L'appelant fournira une requête, pas forcément une liste."""
    canaux = (_canal("A", chat="a"), _canal("B", chat="b"))
    assert _noms(_Pari(), iter(canaux)) == ["A", "B"]


def test_les_structures_sont_immuables():
    c = _canal()
    with pytest.raises(Exception):
        c.actif = False
