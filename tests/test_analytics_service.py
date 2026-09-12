"""Le service de bout en bout : filtres, découpes, matrice, pagination.

TROIS RÈGLES QUE CE FICHIER FAIT RESPECTER
------------------------------------------
1. **Le book vient de `value_bets`, jamais de `played_bets`.** 76,6 % des
   lignes de `played_bets.book` en production portent un LIBELLÉ d'affichage
   (« StarCasino », « Unibet / 711 / Bingoal / Scooore ») et non une valeur
   d'énumération. La fabrique de test y écrit volontairement le libellé : si
   la couche lisait cette colonne, les tests le verraient.

2. **La source temporelle est `value_bets.detected_at`, jamais
   `bet_features.detected_at`.** Les deux colonnes portent le même nom et
   deux significations : `insert_bet_features` fait un INSERT OR REPLACE avec
   l'objet en mémoire, donc la DERNIÈRE détection, tandis que `value_bets`
   garde la PREMIÈRE et ne la réécrit jamais. En production, juin et juillet
   n'ont d'ailleurs aucune feature.

3. **La pagination est côté serveur.** 44 498 lignes envoyées au navigateur
   pour en afficher cinquante, c'est ce que l'énoncé interdit.
"""
from __future__ import annotations

import sqlite3

import pytest

from src.analytics import Filtres, Population, analyser, detail
from src.analytics.filtres import FiltreInvalide
from src.analytics.service import valeurs_disponibles
from tests.analytics_base import Opp, ajouter_features, monter


def _n(chemin, **kw) -> int:
    return analyser(str(chemin), Filtres(**kw))["summary"]["opportunities"]


def _jeu(tmp_path):
    """Dix opportunités, réparties sur deux sports, trois books, deux mois."""
    return monter(tmp_path, [
        Opp(1, sport="soccer", book="unibet_be", odd=1.60, ev=6,
            jour="2026-08-05", cloture=1.55, gagnant="home", outcome="home"),
        Opp(2, sport="soccer", home="C", away="D", book="unibet_be",
            odd=2.20, ev=12, jour="2026-08-10", cloture=2.10,
            gagnant="away", outcome="home"),
        Opp(3, sport="soccer", home="E", away="F", book="ladbrokes_be",
            odd=2.80, ev=9, jour="2026-08-20", cloture=2.90,
            gagnant="home", outcome="home"),
        Opp(4, sport="soccer", home="G", away="H", book="ladbrokes_be",
            odd=3.50, ev=18, jour="2026-09-01", cloture=3.30, gagnant=None),
        Opp(5, sport="tennis", home="Sinner", away="Alcaraz", book="betano_be",
            odd=4.50, ev=25, jour="2026-09-02", cloture=4.80,
            gagnant="away", outcome="home"),
        Opp(6, sport="tennis", home="Djokovic", away="Zverev",
            book="betano_be", odd=7.00, ev=40, jour="2026-09-03",
            cloture=6.50, gagnant="home", outcome="home"),
        Opp(7, sport="soccer", home="I", away="J", book="unibet_be",
            odd=2.00, ev=10, jour="2026-09-04", cloture=None, gagnant=None),
        Opp(8, sport="soccer", home="K", away="L", book="unibet_be",
            odd=2.05, ev=10, jour="2026-09-05", cloture=2.00,
            gagnant="home", outcome="home", joue=True),
        Opp(9, sport="soccer", home="M", away="N", book="ladbrokes_be",
            odd=2.60, ev=14, jour="2026-09-06", cloture=2.50,
            gagnant="away", outcome="home", joue=True),
        Opp(10, sport="tennis", home="Medvedev", away="Rune",
            book="betano_be", odd=3.10, ev=16, jour="2026-09-07",
            cloture=3.00, gagnant="home", outcome="home"),
    ])


# ── Les filtres, un par un, exécutés côté base ──────────────────────

def test_filtre_par_sport(tmp_path):
    p = _jeu(tmp_path)
    assert _n(p, sports=("tennis",)) == 3
    assert _n(p, sports=("soccer",)) == 7
    assert _n(p, sports=("soccer", "tennis")) == 10


def test_filtre_par_bookmakers_multiples(tmp_path):
    p = _jeu(tmp_path)
    assert _n(p, books=("unibet_be",)) == 4
    assert _n(p, books=("unibet_be", "ladbrokes_be")) == 7
    assert _n(p, books=("betano_be",)) == 3


def test_filtre_par_bande_de_cote(tmp_path):
    p = _jeu(tmp_path)
    assert _n(p, cote_min=2.0, cote_max=3.0) == 5


def test_les_bornes_de_cote_sont_INCLUSIVES(tmp_path):
    """Une cote exactement à la borne doit entrer : l'exclure retirerait des
    paris sans que rien ne le dise."""
    p = monter(tmp_path, [Opp(1, odd=2.00), Opp(2, home="C", away="D", odd=4.00)])
    assert _n(p, cote_min=2.00, cote_max=4.00) == 2


def test_filtre_par_bande_d_ev(tmp_path):
    p = _jeu(tmp_path)
    assert _n(p, ev_min=10, ev_max=20) == 6


def test_filtre_par_periode(tmp_path):
    p = _jeu(tmp_path)
    assert _n(p, date_from="2026-08-01", date_to="2026-08-31") == 3
    assert _n(p, date_from="2026-09-01") == 7


def test_la_borne_haute_contient_la_JOURNEE_ENTIERE(tmp_path):
    """Une détection à 23 h 59 le dernier jour demandé doit être DEDANS."""
    p = monter(tmp_path, [
        Opp(1, jour="2026-08-31", detecte="2026-08-31T23:59:00+00:00"),
        Opp(2, home="C", away="D", jour="2026-09-01",
            detecte="2026-09-01T00:00:01+00:00")])
    assert _n(p, date_to="2026-08-31") == 1


def test_filtre_par_marche(tmp_path):
    p = monter(tmp_path, [
        Opp(1, market="h2h"),
        Opp(2, home="C", away="D", market="totals", outcome="over 2.5", line=2.5)])
    assert _n(p, markets=("h2h",)) == 1
    assert _n(p, markets=("h2h", "totals")) == 2


def test_filtre_par_ligue(tmp_path):
    p = monter(tmp_path, [Opp(1, league="Jupiler Pro League"),
                          Opp(2, home="C", away="D", league="Ligue 1")])
    assert _n(p, leagues=("Ligue 1",)) == 1


def test_filtre_par_delai_avant_le_coup_d_envoi(tmp_path):
    """Le délai est CALCULÉ depuis `value_bets.detected_at` et
    `events.start_time` — il couvre donc tout l'historique, contrairement à
    `bet_features.delay_h` qui ne commence qu'en août."""
    p = monter(tmp_path, [
        Opp(1, detecte="2026-08-15T16:00:00+00:00"),   # 2 h avant
        Opp(2, home="C", away="D", detecte="2026-08-15T06:00:00+00:00"),  # 12 h
        Opp(3, home="E", away="F", detecte="2026-08-13T18:00:00+00:00")])  # 48 h
    assert _n(p, delai_max_h=6) == 1
    assert _n(p, delai_min_h=6, delai_max_h=24) == 1
    assert _n(p, delai_min_h=24) == 1


def test_les_filtres_se_COMBINENT(tmp_path):
    """Le critère de réussite de l'énoncé, en une ligne."""
    p = _jeu(tmp_path)
    assert _n(p, sports=("soccer",), books=("unibet_be", "ladbrokes_be"),
              cote_min=2.00, cote_max=4.00, ev_min=10, ev_max=20,
              date_from="2026-08-01", date_to="2026-09-12",
              population=Population.SETTLED) == 3


def test_un_filtre_invalide_remonte_une_FiltreInvalide(tmp_path):
    """Et pas une erreur SQL : c'est une faute de saisie, pas une panne."""
    p = _jeu(tmp_path)
    with pytest.raises(FiltreInvalide):
        analyser(str(p), Filtres(cote_min=4.0, cote_max=2.0))


# ── ⚠️ Le book vient de value_bets ───────────────────────────────────

def test_le_book_vient_de_value_bets_et_JAMAIS_de_played_bets(tmp_path):
    """La fabrique écrit « Unibet / 711 / Bingoal / Scooore » dans
    `played_bets.book` — le libellé composite réel, qui recouvre quatre books.
    Si la couche le lisait, il apparaîtrait ici."""
    p = monter(tmp_path, [Opp(1, book="ladbrokes_be", joue=True)])
    res = analyser(str(p), Filtres())
    libelles = {b["key"] for b in res["by_book"]}
    assert libelles == {"ladbrokes_be"}
    assert detail(str(p), Filtres())["items"][0]["bookmaker"] == "ladbrokes_be"


def test_un_filtre_de_book_ne_rapproche_pas_le_libelle(tmp_path):
    """Filtrer sur « ladbrokes_be » doit trouver le pari même si
    `played_bets` l'appelle autrement."""
    p = monter(tmp_path, [Opp(1, book="ladbrokes_be", joue=True)])
    assert _n(p, books=("ladbrokes_be",), joue="oui") == 1


def test_les_valeurs_disponibles_ne_proposent_que_des_enums(tmp_path):
    """Proposer « StarCasino » et « starcasino_sport » comme deux choix
    distincts dans une liste déroulante serait un piège."""
    p = monter(tmp_path, [Opp(1, book="unibet_be", joue=True)])
    assert valeurs_disponibles(str(p))["bookmakers"] == ["unibet_be"]


# ── ⚠️ La source temporelle est value_bets ───────────────────────────

def test_bet_features_detected_at_n_influence_RIEN(tmp_path):
    """⚠️ Les deux colonnes portent le même nom et deux significations.
    `bet_features.detected_at` est réécrit à chaque re-détection ; ici on lui
    donne une date d'un autre mois, et le filtre de période ne doit pas
    bouger d'un pari."""
    p = monter(tmp_path, [Opp(1, jour="2026-08-05",
                              detecte="2026-08-05T10:00:00+00:00")])
    avant = _n(p, date_from="2026-08-01", date_to="2026-08-31")
    ajouter_features(p, 1, "2026-09-30T10:00:00+00:00")
    apres = _n(p, date_from="2026-08-01", date_to="2026-08-31")
    assert avant == apres == 1
    assert _n(p, date_from="2026-09-01") == 0


def test_une_opportunite_sans_features_reste_visible(tmp_path):
    """Juin et juillet 2026 n'ont AUCUNE feature en production : une couche
    qui joindrait cette table perdrait 39 % de l'historique."""
    p = monter(tmp_path, [Opp(1, jour="2026-07-15",
                              detecte="2026-07-15T10:00:00+00:00")])
    assert _n(p, date_from="2026-07-01", date_to="2026-07-31") == 1


# ── Les découpes ─────────────────────────────────────────────────────

def test_les_decoupes_somment_au_total(tmp_path):
    """⚠️ Une tranche qui disparaît retirerait ses paris d'un total qu'on
    croit complet — le mode de panne du §11."""
    p = _jeu(tmp_path)
    res = analyser(str(p), Filtres())
    total = res["summary"]["opportunities"]
    for axe in ("by_book", "by_sport", "by_odds", "by_ev", "by_time"):
        assert sum(t["opportunities"] for t in res[axe]) == total, axe


def test_chaque_tranche_porte_ses_PROPRES_effectifs(tmp_path):
    p = _jeu(tmp_path)
    for t in analyser(str(p), Filtres())["by_sport"]:
        assert "clv_coverage" in t and "settlement_rate" in t


def test_l_axe_temporel_accepte_trois_granularites(tmp_path):
    p = _jeu(tmp_path)
    for g, attendu in (("jour", 10), ("mois", 2)):
        res = analyser(str(p), Filtres(), granularite=g)
        assert len(res["by_time"]) == attendu, g
    assert len(analyser(str(p), Filtres(), granularite="semaine")["by_time"]) >= 2


def test_une_granularite_inconnue_est_refusee(tmp_path):
    p = _jeu(tmp_path)
    with pytest.raises(FiltreInvalide):
        analyser(str(p), Filtres(), granularite="trimestre")


def test_l_axe_temporel_est_trie_chronologiquement(tmp_path):
    p = _jeu(tmp_path)
    cles = [t["key"] for t in analyser(str(p), Filtres(),
                                       granularite="jour")["by_time"]]
    assert cles == sorted(cles)


# ── La matrice COTE × EV ─────────────────────────────────────────────

def test_la_matrice_couvre_toutes_les_cases(tmp_path):
    """Les cases vides ne sont PAS omises : une case absente se lirait comme
    une case à zéro, alors qu'elle veut dire « rien ici »."""
    p = _jeu(tmp_path)
    m = analyser(str(p), Filtres())["matrix"]
    assert len(m["cells"]) == len(m["rows"]) * len(m["cols"])


def test_chaque_cellule_porte_N_CLV_et_ROI(tmp_path):
    p = _jeu(tmp_path)
    for c in analyser(str(p), Filtres())["matrix"]["cells"]:
        assert {"odds", "ev", "opportunities", "clv", "roi"} <= set(c)


def test_les_cellules_de_la_matrice_somment_au_total(tmp_path):
    p = _jeu(tmp_path)
    res = analyser(str(p), Filtres())
    assert sum(c["opportunities"] for c in res["matrix"]["cells"]) \
        == res["summary"]["opportunities"]


def test_une_cellule_vide_rend_None_et_non_zero(tmp_path):
    p = monter(tmp_path, [Opp(1, odd=2.00, ev=10, cloture=1.9)])
    vides = [c for c in analyser(str(p), Filtres())["matrix"]["cells"]
             if c["opportunities"] == 0]
    assert vides and all(c["clv"] is None and c["roi"] is None for c in vides)


# ── La pagination ────────────────────────────────────────────────────

def test_la_pagination_borne_ce_qui_sort(tmp_path):
    p = _jeu(tmp_path)
    d = detail(str(p), Filtres(), page=1, par_page=3)
    assert len(d["items"]) == 3 and d["total"] == 10 and d["pages"] == 4


def test_les_pages_couvrent_tout_sans_doublon(tmp_path):
    p = _jeu(tmp_path)
    vus = []
    for page in range(1, 5):
        vus += [i["id"] for i in detail(str(p), Filtres(), page, 3)["items"]]
    assert len(vus) == 10 and len(set(vus)) == 10


def test_une_page_au_dela_de_la_fin_est_vide_sans_lever(tmp_path):
    p = _jeu(tmp_path)
    assert detail(str(p), Filtres(), page=99, par_page=10)["items"] == []


def test_la_taille_de_page_est_PLAFONNEE(tmp_path):
    """Sans plafond, `par_page=100000` renverrait toute la base au navigateur
    — exactement ce que l'énoncé interdit."""
    p = _jeu(tmp_path)
    assert detail(str(p), Filtres(), 1, 100000)["per_page"] == 500


def test_une_page_zero_ou_negative_retombe_sur_la_premiere(tmp_path):
    p = _jeu(tmp_path)
    assert detail(str(p), Filtres(), page=0)["page"] == 1
    assert detail(str(p), Filtres(), page=-3)["page"] == 1


# ── Le tri du détail ─────────────────────────────────────────────────

@pytest.mark.parametrize("colonne", ["detected_at", "odd_taken", "ev_pct",
                                     "clv", "pnl", "sport", "book"])
def test_chaque_colonne_triable_repond(tmp_path, colonne):
    p = _jeu(tmp_path)
    assert len(detail(str(p), Filtres(), tri=colonne)["items"]) == 10


def test_le_tri_par_cote_est_correct_dans_les_deux_sens(tmp_path):
    p = _jeu(tmp_path)
    desc = [i["odds"] for i in detail(str(p), Filtres(), tri="odd_taken",
                                      ordre="desc")["items"]]
    asc = [i["odds"] for i in detail(str(p), Filtres(), tri="odd_taken",
                                     ordre="asc")["items"]]
    assert desc == sorted(desc, reverse=True) and asc == sorted(asc)


def test_les_valeurs_manquantes_vont_en_FIN_de_liste(tmp_path):
    """⚠️ Un `None` en tête de tri ferait croire à un extrême."""
    p = _jeu(tmp_path)
    for ordre in ("desc", "asc"):
        clvs = [i["clv_pct"] for i in
                detail(str(p), Filtres(), tri="clv", ordre=ordre)["items"]]
        assert clvs[-1] is None, f"un None a remonté en {ordre}"
        assert clvs[0] is not None, f"un None est en tête en {ordre}"


def test_un_tri_inconnu_retombe_sur_un_defaut_sans_lever(tmp_path):
    p = _jeu(tmp_path)
    assert len(detail(str(p), Filtres(), tri="; DROP TABLE value_bets")["items"]) == 10


# ── Le détail porte les colonnes demandées ──────────────────────────

def test_le_detail_porte_toutes_les_colonnes_attendues(tmp_path):
    p = _jeu(tmp_path)
    attendu = {"detected_at", "sport", "league", "event", "market",
               "selection", "bookmaker", "odds", "ev_pct", "stake", "result",
               "closing_fair_odd", "clv_pct", "pnl"}
    assert attendu <= set(detail(str(p), Filtres())["items"][0])


# ── Lecture seule et sécurité ────────────────────────────────────────

def test_la_base_n_est_JAMAIS_modifiee(tmp_path):
    """La garantie vient de `mode=ro`, pas de la discipline de celui qui code.
    On compare l'empreinte du fichier avant et après une analyse complète."""
    import hashlib
    p = _jeu(tmp_path)
    empreinte = lambda: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
    avant = empreinte()
    analyser(str(p), Filtres())
    detail(str(p), Filtres())
    valeurs_disponibles(str(p))
    assert empreinte() == avant, "la base a été modifiée"


def test_une_base_inexistante_leve_au_lieu_d_en_creer_une(tmp_path):
    """Une base vide créée à côté de la vraie rendrait « 0 opportunité » — un
    chiffre parfaitement normal, et faux."""
    with pytest.raises(FileNotFoundError):
        analyser(str(tmp_path / "absente.db"), Filtres())


@pytest.mark.parametrize("mechant", [
    "'; DROP TABLE value_bets; --",
    "unibet_be' OR '1'='1",
    "1); DELETE FROM results; --",
])
def test_une_valeur_hostile_ne_peut_pas_atteindre_le_sql(tmp_path, mechant):
    """Elle doit être REFUSÉE comme book inconnu — jamais interprétée. Le
    filtre ne concatène rien : seul le NOMBRE de marqueurs varie."""
    p = _jeu(tmp_path)
    with pytest.raises(FiltreInvalide):
        analyser(str(p), Filtres(books=(mechant,)))
    # La table est intacte.
    con = sqlite3.connect(str(p))
    assert con.execute("SELECT COUNT(*) FROM value_bets").fetchone()[0] == 10
    con.close()


def test_un_sport_hostile_ne_casse_rien(tmp_path):
    """Les sports sont un ensemble ouvert : la valeur passe en PARAMÈTRE et ne
    rapproche simplement rien."""
    p = _jeu(tmp_path)
    assert _n(p, sports=("'; DROP TABLE value_bets; --",)) == 0
    con = sqlite3.connect(str(p))
    assert con.execute("SELECT COUNT(*) FROM value_bets").fetchone()[0] == 10
    con.close()


# ── Les valeurs disponibles ──────────────────────────────────────────

def test_valeurs_disponibles_peuple_les_listes(tmp_path):
    p = _jeu(tmp_path)
    v = valeurs_disponibles(str(p))
    assert set(v["sports"]) == {"soccer", "tennis"}
    assert set(v["bookmakers"]) == {"unibet_be", "ladbrokes_be", "betano_be"}
    assert v["date_min"] == "2026-08-05" and v["date_max"] == "2026-09-07"
    assert len(v["populations"]) == 6


def test_chaque_population_proposee_porte_son_explication(tmp_path):
    p = _jeu(tmp_path)
    for bloc in valeurs_disponibles(str(p))["populations"]:
        assert bloc["explication"] and "limites" in bloc


# ── L'analyse rend les filtres qu'elle a APPLIQUÉS ──────────────────

def test_l_analyse_renvoie_les_filtres_normalises(tmp_path):
    """Un filtre rendu différent de celui demandé (alias déplié, casse
    corrigée) doit être visible : c'est ce qui permet de rejouer l'analyse."""
    p = _jeu(tmp_path)
    rendus = analyser(str(p), Filtres(books=("kambi",)))["filters"]
    assert len(rendus["bookmakers"]) == 4
    import json
    assert json.loads(json.dumps(rendus)) == rendus
