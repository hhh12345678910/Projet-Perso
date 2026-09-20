"""PHASE 4 — périmètre, libellés, tranches d'EV, et l'EV PAR SPORT.

La question qui gouverne ce fichier : **le filtre est-il RÉELLEMENT appliqué
au lot, ou seulement à l'affichage ?** Un filtre côté navigateur laisserait
les KPI, les découpes et la matrice décrire un autre lot que le tableau de
détail, sans qu'aucun d'eux n'ait l'air faux. Tous les tests d'EV par sport
vérifient donc le CONTENU du lot rendu, jamais la présence d'un paramètre.
"""
from __future__ import annotations

import pytest

from src.analytics.filtres import Filtres, FiltreInvalide
from src.analytics.perimetre import (BORNES_EV, MARCHES_ANALYTICS,
                                     PALIERS_ECHANTILLON, SPORTS_ANALYTICS,
                                     bande_delai, libelle_book, libelle_marche,
                                     libelle_sport, taille_echantillon)
from src.analytics.service import analyser, valeurs_disponibles
from tests.analytics_base import Opp, monter


def _jeu(tmp_path):
    """Un lot où chaque (sport, tranche d'EV) porte exactement 4 opportunités.

    Les effectifs sont égaux par construction : un écart observé vient alors
    forcément du filtre, jamais d'un déséquilibre du jeu de test."""
    opps, i = [], 0
    for sport in ("soccer", "tennis"):
        for ev in (3.0, 6.0, 10.0, 20.0, 40.0):
            for k in range(4):
                i += 1
                opps.append(Opp(id=i, sport=sport, ev=ev, odd=2.0 + k * 0.5,
                                home=f"H{i}", away=f"A{i}", cloture=1.95,
                                gagnant="home", outcome="home",
                                market="totals" if k == 3 else "h2h",
                                line=2.5 if k == 3 else None))
    for sport in ("basketball", "hockey", "volleyball", "unknown"):
        for k in range(3):
            i += 1
            opps.append(Opp(id=i, sport=sport, home=f"X{i}", away=f"Y{i}",
                            cloture=1.95, gagnant="home", outcome="home"))
    return monter(tmp_path, opps)


def _n(p, **kw):
    return analyser(p, Filtres(**kw))["summary"]["opportunities"]


def _par_sport(p, **kw):
    a = analyser(p, Filtres(**kw))
    return {t["key"]: t["opportunities"] for t in a["by_sport"]}


# ══ Les bornes d'EV ne sont pas une seconde définition ═══════════════

def test_les_bornes_dEV_sont_VERROUILLEES_sur_le_moteur():
    """⚠️ LE TEST LE PLUS IMPORTANT DE CE FICHIER.

    `perimetre.BORNES_EV` existe parce qu'il faut des INTERVALLES pour écrire
    du SQL, alors que `main._ev_bucket` ne rend qu'une étiquette. C'est donc
    une seconde écriture de la même règle — exactement ce que le §17.7
    interdit — et la seule chose qui la rend acceptable est ce balayage : si
    l'une des deux bougeait, ce test tomberait avant que le moindre chiffre ne
    soit faux dans l'interface."""
    from src.main import _ev_bucket

    def bande(ev):
        for lab, (lo, hi) in BORNES_EV.items():
            if (lo is None or ev >= lo) and (hi is None or ev < hi):
                return lab
        return None

    # De −20 % à +200 %, au centième : 22 001 valeurs.
    for x in range(-2000, 20001):
        ev = x / 100.0
        assert bande(ev) == _ev_bucket(ev), f"désaccord à EV = {ev}"


def test_les_bornes_hautes_sont_EXCLUES():
    """Une borne haute incluse compterait un pari à 8 % dans DEUX tranches."""
    from src.main import _ev_bucket
    assert _ev_bucket(8.0) == "8-15%"
    assert BORNES_EV["5-8%"][1] == 8.0
    assert BORNES_EV["8-15%"][0] == 8.0


# ══ Le périmètre : sports ═══════════════════════════════════════════

def test_sans_filtre_seuls_les_sports_du_perimetre_sont_comptes(tmp_path):
    p = _jeu(tmp_path)
    assert _n(p) == 40, "20 soccer + 20 tennis, les 12 autres écartés"
    assert set(_par_sport(p)) == set(SPORTS_ANALYTICS)


@pytest.mark.parametrize("sport", ["soccer", "tennis"])
def test_un_seul_sport(tmp_path, sport):
    p = _jeu(tmp_path)
    assert _n(p, sports=(sport,)) == 20
    assert set(_par_sport(p, sports=(sport,))) == {sport}


def test_les_deux_sports_ensemble(tmp_path):
    p = _jeu(tmp_path)
    assert _par_sport(p, sports=("soccer", "tennis")) == {"soccer": 20,
                                                          "tennis": 20}


@pytest.mark.parametrize("exclu", ["basketball", "hockey", "volleyball",
                                   "unknown"])
def test_un_sport_exclu_est_REFUSE_et_absent_des_totaux(tmp_path, exclu):
    """Deux propriétés d'un coup : le demander est refusé, et ne pas le
    demander ne le fait pas entrer par la bande dans les agrégats."""
    p = _jeu(tmp_path)
    with pytest.raises(FiltreInvalide):
        _n(p, sports=(exclu,))
    assert exclu not in _par_sport(p)


def test_les_donnees_exclues_restent_EN_BASE(tmp_path):
    """⚠️ Le périmètre est un filtre de LECTURE. Rien n'est supprimé."""
    import sqlite3
    p = _jeu(tmp_path)
    analyser(p, Filtres())
    con = sqlite3.connect(str(p))
    n = con.execute("SELECT COUNT(*) FROM events WHERE sport = 'basketball'"
                    ).fetchone()[0]
    con.close()
    assert n == 3, "le basket a disparu de la base — interdit"


# ══ Le périmètre : marchés ══════════════════════════════════════════

@pytest.mark.parametrize("m", ["h2h", "totals"])
def test_un_marche_du_perimetre_passe(tmp_path, m):
    p = _jeu(tmp_path)
    assert _n(p, markets=(m,)) > 0


@pytest.mark.parametrize("m", ["h2h_h1", "totals_h1", "handicap", "btts"])
def test_un_marche_hors_perimetre_est_refuse(tmp_path, m):
    p = _jeu(tmp_path)
    with pytest.raises(FiltreInvalide) as e:
        _n(p, markets=(m,))
    assert "périmètre" in str(e.value)


def test_les_deux_marches_somment_au_total(tmp_path):
    p = _jeu(tmp_path)
    a = analyser(p, Filtres())
    par = {t["key"]: t["opportunities"] for t in a["by_market"]}
    assert set(par) <= set(MARCHES_ANALYTICS)
    assert sum(par.values()) == a["summary"]["opportunities"]


# ══ Sélection multiple ══════════════════════════════════════════════

def test_plusieurs_bookmakers_en_union(tmp_path):
    opps = [Opp(id=i, home=f"H{i}", away=f"A{i}",
                book=b, cloture=1.9, gagnant="home", outcome="home")
            for i, b in enumerate(
                ["unibet_be"] * 5 + ["betano_be"] * 3 + ["betfirst"] * 2, 1)]
    p = monter(tmp_path, opps)
    assert _n(p, books=("unibet_be",)) == 5
    assert _n(p, books=("unibet_be", "betano_be")) == 8
    assert _n(p, books=("unibet_be", "betano_be", "betfirst")) == 10


def test_plusieurs_tranches_dEV_en_UNION(tmp_path):
    p = _jeu(tmp_path)
    assert _n(p, ev_bandes=("5-8%",)) == 8          # 4 soccer + 4 tennis
    assert _n(p, ev_bandes=("8-15%",)) == 8
    assert _n(p, ev_bandes=("5-8%", "8-15%")) == 16, "l'union doit ADDITIONNER"
    assert _n(p, ev_bandes=("5-8%", "8-15%", "15-35%")) == 24


def test_les_tranches_et_les_bornes_libres_se_CUMULENT(tmp_path):
    """Les deux mécanismes se composent en ET, pas en OU : cocher « 8-15 % »
    puis écrire « min 10 » doit rendre 10-15 %, pas 8-15 %."""
    p = _jeu(tmp_path)
    assert _n(p, ev_bandes=("5-8%", "8-15%")) == 16
    assert _n(p, ev_bandes=("5-8%", "8-15%"), ev_min=9.0) == 8


# ══ L'EV PAR SPORT — le cœur de la Phase 4 ══════════════════════════

def test_une_regle_par_sport_est_appliquee_AU_LOT(tmp_path):
    """Soccer en 5-8 %, tennis en 15-35 % : le lot doit contenir 4 + 4."""
    p = _jeu(tmp_path)
    f = dict(ev_par_sport=(("soccer", ("5-8%",)),
                           ("tennis", ("15-35%",))))
    assert _n(p, **f) == 8
    assert _par_sport(p, **f) == {"soccer": 4, "tennis": 4}


def test_AUCUNE_FUITE_dune_regle_de_sport_vers_lautre(tmp_path):
    """⚠️ LE TEST QUI COMPTE. On vérifie l'EV RÉELLEMENT présente dans le lot,
    pas le nombre de lignes : une fuite pourrait rendre le bon effectif avec
    les mauvais paris."""
    p = _jeu(tmp_path)
    from src.analytics.service import detail
    d = detail(p, Filtres(ev_par_sport=(("soccer", ("5-8%",)),
                                        ("tennis", ("35%+",)))),
               par_page=500)
    for it in d["items"]:
        if it["sport"] == "soccer":
            assert 5.0 <= it["ev_pct"] < 8.0, "une règle tennis a fui vers le foot"
        elif it["sport"] == "tennis":
            assert it["ev_pct"] >= 35.0, "une règle foot a fui vers le tennis"
        else:
            pytest.fail(f"sport hors périmètre dans le détail : {it['sport']}")


def test_un_sport_SANS_regle_propre_retombe_sur_la_regle_globale(tmp_path):
    """Le tennis a sa règle ; le football n'en a pas et doit suivre la
    sélection globale — ni disparaître, ni hériter de celle du tennis."""
    p = _jeu(tmp_path)
    f = dict(ev_bandes=("8-15%",), ev_par_sport=(("tennis", ("35%+",)),))
    assert _par_sport(p, **f) == {"soccer": 4, "tennis": 4}
    from src.analytics.service import detail
    for it in detail(p, Filtres(**f), par_page=500)["items"]:
        attendu = (8.0 <= it["ev_pct"] < 15.0 if it["sport"] == "soccer"
                   else it["ev_pct"] >= 35.0)
        assert attendu, f"{it['sport']} à EV {it['ev_pct']} ne suit aucune règle"


def test_un_sport_sans_regle_ET_sans_regle_globale_nest_pas_filtre(tmp_path):
    p = _jeu(tmp_path)
    f = dict(ev_par_sport=(("tennis", ("35%+",)),))
    assert _par_sport(p, **f) == {"soccer": 20, "tennis": 4}


def test_la_regle_appliquee_est_RELISIBLE_dans_la_reponse(tmp_path):
    """Une règle qu'on ne peut pas relire est une règle qu'on croit posée."""
    p = _jeu(tmp_path)
    a = analyser(p, Filtres(ev_bandes=("8-15%",),
                            ev_par_sport=(("tennis", ("35%+",)),)))
    assert a["ev_rules"]["global"] == ["8-15%"]
    assert a["ev_rules"]["by_sport"] == {"tennis": ["35%+"]}
    assert set(a["ev_rules"]["sports_analyses"]) == set(SPORTS_ANALYTICS)


def test_la_MATRICE_respecte_la_regle_par_sport(tmp_path):
    """La matrice vient du MÊME lot que les KPI : elle ne peut pas contenir
    une tranche d'EV que la règle exclut."""
    p = _jeu(tmp_path)
    a = analyser(p, Filtres(ev_par_sport=(("soccer", ("5-8%",)),
                                          ("tennis", ("5-8%",)))))
    peuplees = {c["ev"] for c in a["matrix"]["cells"] if c["opportunities"]}
    assert peuplees == {"5-8%"}
    assert (sum(c["opportunities"] for c in a["matrix"]["cells"])
            == a["summary"]["opportunities"])


def test_une_regle_par_sport_pour_un_sport_hors_perimetre_est_refusee():
    with pytest.raises(FiltreInvalide):
        Filtres(ev_par_sport=(("hockey", ("5-8%",)),)).valider()


def test_une_bande_dEV_inventee_est_refusee():
    with pytest.raises(FiltreInvalide) as e:
        Filtres(ev_bandes=("12-13%",)).valider()
    assert "Bande d'EV inconnue" in str(e.value)


def test_deux_regles_pour_le_meme_sport_sont_refusees():
    """Laquelle s'appliquerait ? Refuser vaut mieux que choisir en silence."""
    with pytest.raises(FiltreInvalide):
        Filtres(ev_par_sport=(("soccer", ("5-8%",)),
                              ("soccer", ("8-15%",)))).valider()


def test_une_regle_VIDE_ne_filtre_pas_tout(tmp_path):
    """« Aucune tranche cochée pour le tennis » veut dire « pas de règle
    propre », jamais « aucune opportunité de tennis »."""
    p = _jeu(tmp_path)
    assert _par_sport(p, ev_par_sport=(("tennis", ()),)) == {"soccer": 20,
                                                             "tennis": 20}


# ══ Libellés ════════════════════════════════════════════════════════

@pytest.mark.parametrize("canonique,attendu", [
    ("unibet_be", "Unibet BE"), ("betano_be", "Betano BE"),
    ("betfirst", "Betfirst"), ("magicbetting", "MagicBetting"),
    ("ladbrokes_be", "Ladbrokes"), ("napoleon_be", "Napoleon BE"),
    ("circus_be", "Circus BE"), ("starcasino_sport", "StarCasino Sport"),
])
def test_les_libelles_de_books(canonique, attendu):
    assert libelle_book(canonique) == attendu


def test_TOUS_les_books_du_moteur_ont_un_libelle():
    """Un book sans libellé ressortirait en identifiant brut au milieu de noms
    propres — et on ne s'en apercevrait qu'en le voyant."""
    from src.models import Book
    from src.analytics.perimetre import LIBELLE_BOOK
    manquants = [b.value for b in Book if b.value not in LIBELLE_BOOK]
    assert manquants == []


def test_les_libelles_de_sports_et_marches():
    assert libelle_sport("soccer") == "Soccer"
    assert libelle_sport("tennis") == "Tennis"
    assert libelle_marche("h2h") == "H2H"
    assert libelle_marche("totals") == "Totals"


def test_un_libelle_inconnu_rend_la_valeur_BRUTE():
    """Pas un « ? » : une valeur inattendue doit rester identifiable."""
    assert libelle_book("book_de_demain") == "book_de_demain"


def test_les_decoupes_portent_key_ET_label(tmp_path):
    """`key` repart vers l'API, `label` s'affiche. Les confondre renverrait
    « Unibet BE » au serveur, qui ne connaît que `unibet_be`."""
    p = _jeu(tmp_path)
    a = analyser(p, Filtres())
    from src.analytics.perimetre import libelle_groupe_book
    for t in a["by_book"]:
        assert t["key"] == t["key"].lower()
        # La DÉCOUPE porte le libellé du groupe ; `libelle_book` reste celui
        # du détail, qui doit nommer le book où le prix a été vu.
        assert t["label"] == libelle_groupe_book(t["key"])
    for t in a["by_sport"]:
        assert t["label"] == libelle_sport(t["key"])


def test_les_valeurs_disponibles_sont_bornees_au_perimetre(tmp_path):
    p = _jeu(tmp_path)
    v = valeurs_disponibles(p)
    assert v["sports"] == list(SPORTS_ANALYTICS)
    assert set(v["markets"]) <= set(MARCHES_ANALYTICS)
    assert v["sports_labels"] == {"soccer": "Soccer", "tennis": "Tennis"}
    assert v["perimetre"]["sports"] == list(SPORTS_ANALYTICS)


def test_les_exclusions_du_perimetre_sont_COMPTEES(tmp_path):
    """Un écart de total qu'on ne peut pas chiffrer passe pour un bug."""
    p = _jeu(tmp_path)
    ex = valeurs_disponibles(p)["perimetre"]["exclus"]
    assert ex["total_detections"] == 52
    assert ex["sport_hors_perimetre"] == 12
    assert ex["sans_evenement"] == 0


# ══ Taille d'échantillon — VOLUME, jamais significativité ═══════════

@pytest.mark.parametrize("n,niveau", [
    (5000, "tres_bon"), (1000, "tres_bon"), (999, "bon"), (300, "bon"),
    (299, "moyen"), (100, "moyen"), (99, "petit"), (0, "petit"),
])
def test_les_paliers_dechantillon(n, niveau):
    assert taille_echantillon(n)["niveau"] == niveau


def test_aucun_palier_ne_parle_de_SIGNIFICATIVITE():
    """⚠️ Le mot est interdit : un effectif ne décide pas de la
    significativité — il faudrait la variance, la taille de l'effet cherché et
    le nombre de comparaisons. Le barème qualifie un VOLUME."""
    for _, _, libelle in PALIERS_ECHANTILLON:
        assert "significat" not in libelle.lower()


def test_le_resume_porte_TROIS_echantillons_distincts(tmp_path):
    """Opportunités, réglés et CLV n'ont pas le même effectif ; un seul badge
    cacherait toujours le plus fragile des trois."""
    opps = [Opp(id=i, home=f"H{i}", away=f"A{i}",
                cloture=1.9 if i <= 30 else None,
                gagnant="home" if i <= 10 else None, outcome="home")
            for i in range(1, 51)]
    p = monter(tmp_path, opps)
    s = analyser(p, Filtres())["summary"]
    assert s["sample"]["n"] == 50
    assert s["sample_settled"]["n"] == 10
    assert s["sample_clv"]["n"] == 30


# ══ Bandes de délai ═════════════════════════════════════════════════

@pytest.mark.parametrize("h,bande", [
    (-3.0, "LIVE / < 1 h"), (0.0, "LIVE / < 1 h"), (0.9, "LIVE / < 1 h"),
    (1.0, "1-3 h"), (2.9, "1-3 h"), (3.0, "3-6 h"), (5.9, "3-6 h"),
    (6.0, "6-12 h"), (11.9, "6-12 h"), (12.0, "12-24 h"), (23.9, "12-24 h"),
    (24.0, "> 24 h"), (900.0, "> 24 h"), (None, "?"),
])
def test_les_bandes_de_delai_couvrent_la_droite_sans_trou(h, bande):
    assert bande_delai(h) == bande


def test_la_decoupe_par_delai_somme_au_total(tmp_path):
    p = _jeu(tmp_path)
    a = analyser(p, Filtres())
    assert (sum(t["opportunities"] for t in a["by_delay"])
            == a["summary"]["opportunities"])


# ══ Les cumuls temporels ════════════════════════════════════════════

def test_le_pnl_cumule_est_une_SOMME_et_la_clv_cumulee_une_PONDERATION(tmp_path):
    """⚠️ Une moyenne de moyennes donnerait au bruit le même poids qu'au
    signal : une semaine à +20 % sur 3 paris pèserait autant qu'une semaine à
    +2 % sur 3 000."""
    opps = []
    # Semaine 1 : 3 paris à forte CLV. Semaine 2 : 30 paris à faible CLV.
    for i in range(1, 4):
        opps.append(Opp(id=i, home=f"H{i}", away=f"A{i}", jour="2026-08-03",
                        odd=2.4, cloture=2.0, gagnant="home", outcome="home"))
    for i in range(4, 34):
        opps.append(Opp(id=i, home=f"H{i}", away=f"A{i}", jour="2026-08-10",
                        odd=2.02, cloture=2.0, gagnant="home", outcome="home"))
    p = monter(tmp_path, opps)
    t = analyser(p, Filtres(), granularite="semaine")["by_time"]
    assert len(t) == 2
    # Le cumul final doit être proche du faible, pas à mi-chemin.
    assert t[-1]["clv_n_cumul"] == 33
    assert t[-1]["clv_cumul"] < 3.0, "moyenne de moyennes détectée"
    assert t[-1]["pnl_cumul"] == pytest.approx(
        (t[0]["pnl"] or 0) + (t[1]["pnl"] or 0), abs=0.02)


# ══ LES JUMEAUX KAMBI, DE BOUT EN BOUT ═════════════════════════════
#
# Unibet, 711, Bingoal et Scooore servent un seul flux Kambi et cotent à
# l'identique. Le moteur le sait depuis toujours — `merge_twin_book_value_bets`
# fusionne leurs alertes — mais l'Analytics les traitait comme quatre
# bookmakers distincts. Conséquence : le résultat dépendait du jumeau coché,
# et les effectifs se retrouvaient éclatés en quatre lots trop petits pour
# conclure, là où il n'y a qu'un seul compte à jouer.

def _jumeaux(tmp_path):
    """Quatre matchs DISTINCTS, un par jumeau. Distincts pour que le
    dédoublonnage SQL — dont la clé ignore le book — ne les fusionne pas de
    lui-même : ce qu'on teste ici est le groupement par bookmaker, pas lui."""
    from src.analytics.perimetre import GROUPES_JUMEAUX
    return monter(tmp_path, [
        Opp(id=i, home=f"Dom{i}", away=f"Ext{i}", book=b, sport="soccer",
            odd=2.0, ev=10.0, jour="2026-08-10", cloture=1.9,
            gagnant="home", outcome="home")
        for i, b in enumerate(GROUPES_JUMEAUX[0], start=1)])


def test_les_quatre_jumeaux_ne_font_QUUNE_ligne_par_bookmaker(tmp_path):
    """⚠️ L'EXIGENCE VUE DE BOUT EN BOUT.

    Sans la fusion, `by_book` rendait quatre lignes de un pari."""
    from src.analytics.perimetre import GROUPES_JUMEAUX
    a = analyser(_jumeaux(tmp_path), Filtres())
    assert {t["key"] for t in a["by_book"]} == {GROUPES_JUMEAUX[0][0]}
    ligne = a["by_book"][0]
    assert ligne["opportunities"] == 4
    assert ligne["label"] == "Unibet / Scooore / 711 / Bingoal"


def test_NIMPORTE_LEQUEL_des_jumeaux_rend_le_MEME_resultat(tmp_path):
    """⚠️ LA DEMANDE, MOT POUR MOT : « qu'on choisisse Scooore, 711 ou
    Unibet, je veux qu'on ait les mêmes résultats »."""
    from src.analytics.perimetre import GROUPES_JUMEAUX
    p = _jumeaux(tmp_path)
    resumes = []
    for b in GROUPES_JUMEAUX[0]:
        s = analyser(p, Filtres(books=(b,)))["summary"]
        assert s["opportunities"] == 4, f"{b} ne voit pas les quatre paris"
        resumes.append((s["opportunities"], s["clv"], s["roi"], s["settled"]))
    assert len(set(resumes)) == 1, "le résultat dépend encore du jumeau choisi"


def test_un_book_sans_jumeau_reste_seul(tmp_path):
    """La fusion ne doit toucher QUE le groupe Kambi."""
    p = monter(tmp_path, [
        Opp(id=1, home="A", away="B", book="ladbrokes_be", sport="soccer",
            odd=2.0, ev=10.0, jour="2026-08-10", cloture=1.9,
            gagnant="home", outcome="home"),
        Opp(id=2, home="C", away="D", book="betano_be", sport="soccer",
            odd=2.0, ev=10.0, jour="2026-08-10", cloture=1.9,
            gagnant="home", outcome="home")])
    a = analyser(p, Filtres())
    assert {t["key"] for t in a["by_book"]} == {"ladbrokes_be", "betano_be"}
    assert analyser(p, Filtres(books=("betano_be",)))["summary"]["opportunities"] == 1


def test_la_liste_des_filtres_ne_propose_QUUNE_case_pour_les_quatre(tmp_path):
    from src.analytics.perimetre import GROUPES_JUMEAUX
    v = valeurs_disponibles(_jumeaux(tmp_path))
    assert v["bookmakers"] == [GROUPES_JUMEAUX[0][0]]
    assert v["bookmakers_labels"][GROUPES_JUMEAUX[0][0]] == \
        "Unibet / Scooore / 711 / Bingoal"


def test_le_DETAIL_nomme_le_vrai_book_pas_le_groupe(tmp_path):
    """⚠️ CE QUE LA FUSION NE DOIT PAS EMPORTER.

    Une ligne de détail dit où le prix a RÉELLEMENT été vu. La remplacer par
    le libellé du groupe ferait perdre l'information sur quel compte miser."""
    from src.analytics.service import detail
    d = detail(_jumeaux(tmp_path), Filtres(), page=1, par_page=50)
    books = {r["bookmaker"] for r in d["items"]}
    assert len(books) == 4, f"le détail a perdu le book réel : {books}"


# ══ LES TRANCHES DE COTE, GLOBALES ET PAR SPORT ════════════════════
#
# L'EV avait ses bandes, la cote n'avait que des bornes libres. Or la matrice
# cote × EV raisonne déjà par tranches : on pouvait LIRE une tranche sans
# pouvoir FILTRER dessus, et reconstruire « 1.8-2.3 » à la main en bornes
# libres ne dit rien de la convention (basse incluse, haute exclue).

def _cotes(tmp_path):
    """Quatre cotes par sport, une dans chaque tranche utile."""
    return monter(tmp_path, [
        Opp(id=i, home=f"H{i}", away=f"A{i}", sport=s, book="unibet_be",
            market="h2h", outcome="home", odd=o, ev=10.0, jour="2026-08-10",
            cloture=1.9, gagnant="home")
        for i, (s, o) in enumerate(
            [("soccer", 1.5), ("soccer", 2.0), ("soccer", 2.6), ("soccer", 7.0),
             ("tennis", 1.5), ("tennis", 2.0), ("tennis", 2.6), ("tennis", 7.0)],
            start=1)])


def test_une_bande_de_cote_filtre_vraiment(tmp_path):
    p = _cotes(tmp_path)
    r = analyser(p, Filtres(cote_bandes=("1.8-2.3",)))
    assert r["summary"]["opportunities"] == 2      # la cote 2.00 des deux sports


def test_plusieurs_bandes_sont_une_UNION(tmp_path):
    p = _cotes(tmp_path)
    r = analyser(p, Filtres(cote_bandes=("1.8-2.3", "> 6.0")))
    assert r["summary"]["opportunities"] == 4


def test_les_bandes_de_cote_PAR_SPORT(tmp_path):
    """⚠️ LA DEMANDE : des tranches de cote propres à chaque sport."""
    p = _cotes(tmp_path)
    r = analyser(p, Filtres(cote_par_sport=(("soccer", ("1.0-1.8",)),
                                            ("tennis", ("> 6.0",)))))
    par = {t["key"]: t["opportunities"] for t in r["by_sport"]}
    assert par == {"soccer": 1, "tennis": 1}


def test_un_sport_SANS_regle_propre_garde_la_regle_globale(tmp_path):
    """⚠️ LA BRANCHE QUI EMPÊCHE UNE FUITE SILENCIEUSE.

    Une règle posée sur le foot ne doit ni faire disparaître le tennis, ni lui
    imposer la règle du foot : il retombe sur la sélection globale."""
    p = _cotes(tmp_path)
    r = analyser(p, Filtres(cote_bandes=("> 6.0",),
                            cote_par_sport=(("soccer", ("1.0-1.8",)),)))
    par = {t["key"]: t["opportunities"] for t in r["by_sport"]}
    assert par == {"soccer": 1, "tennis": 1}       # foot 1.5, tennis 7.0


def test_bandes_et_bornes_libres_se_CUMULENT(tmp_path):
    """Même convention que l'EV : les bandes disent « dans quelles tranches »,
    les bornes libres « et pas au-delà de ». Les deux en ET."""
    p = _cotes(tmp_path)
    r = analyser(p, Filtres(cote_bandes=("1.8-2.3", "2.3-3.0"), cote_min=2.2))
    assert r["summary"]["opportunities"] == 2      # seule la cote 2.60 survit


def test_la_borne_basse_est_INCLUSE_et_la_haute_EXCLUE(tmp_path):
    """Une cote exactement à 2.30 appartient à « 2.3-3.0 », jamais aux deux —
    sinon elle compterait deux fois dans une somme de tranches."""
    p = monter(tmp_path, [
        Opp(id=1, home="A", away="B", sport="soccer", book="unibet_be",
            odd=2.30, ev=10.0, jour="2026-08-10", cloture=1.9, gagnant="home")])
    assert analyser(p, Filtres(cote_bandes=("1.8-2.3",)))["summary"]["opportunities"] == 0
    assert analyser(p, Filtres(cote_bandes=("2.3-3.0",)))["summary"]["opportunities"] == 1


def test_les_bandes_viennent_de_la_MEME_table_que_la_matrice(tmp_path):
    """Une seconde table ferait qu'une tranche lisible dans la matrice
    n'existerait pas comme filtre — ou l'inverse."""
    from src.analytics.perimetre import bornes_cote, ordre_cote
    from scripts.pnl_detections import BANDES_COTE
    assert ordre_cote() == tuple(lab for lab, _, _ in BANDES_COTE)
    assert set(bornes_cote()) == {lab for lab, _, _ in BANDES_COTE}
    v = valeurs_disponibles(_cotes(tmp_path))
    assert [b["key"] for b in v["odds_bands"]] == list(ordre_cote())
