"""La déduplication — le seul défaut qui puisse compter un match 43 fois.

CE QUE CE FICHIER PROTÈGE
-------------------------
`event_key` contient l'HEURE du coup d'envoi (`matcher.event_key`). Une
révision d'horaire crée donc une clé neuve pour le MÊME match. Mesuré sur la
base de production le 12/09 :

    tennis      17 380 matchs → 53 600 clés  (3,084 par match, jusqu'à 43)
    soccer      30 138 matchs → 31 023 clés

Sans déduplication, un match de tennis pèse jusqu'à quarante-trois fois son
poids réel dans une moyenne. La clé analytique est donc (§17.8) :

    équipes + jour + marché + pari + ligne        — jamais `event_key`

⚠️ ET LE PIÈGE QUI A MORDU TROIS FOIS. La dédup garde la MEILLEURE COTE. Toute
propriété qui vit en dehors de `value_bets` — jouée, notifiée — doit être
agrégée sur le GROUPE avant qu'on choisisse le représentant. La lire sur le
représentant remplirait le lot « non joué » exactement des paris les mieux
tarifés, et la comparaison dirait le contraire de la vérité.
"""
from __future__ import annotations

from src.analytics import Filtres, Population, analyser, detail
from tests.analytics_base import Opp, monter


def _n(chemin, **kw) -> int:
    return analyser(str(chemin), Filtres(**kw))["summary"]["opportunities"]


# ── Le cas tennis : plusieurs clés, un seul match ────────────────────

def test_cinq_event_key_du_meme_match_ne_comptent_qu_une_fois(tmp_path):
    """Le coup d'envoi glisse de 20 minutes à chaque republication du book.
    Cinq clés, un seul match, une seule opportunité."""
    lignes = [Opp(i, sport="tennis", home="Sinner", away="Alcaraz",
                  heure=f"1{i}:00", odd=2.00 + i / 100)
              for i in range(1, 6)]
    p = monter(tmp_path, lignes)
    assert _n(p) == 1


def test_le_pire_cas_mesure_en_production_tient(tmp_path):
    """43 clés pour un match — le maximum relevé sur la base réelle."""
    lignes = [Opp(i, sport="tennis", home="Sinner", away="Alcaraz",
                  event_key=f"2026081512{i:02d}::sinner__vs__alcaraz",
                  odd=2.00 + i / 1000)
              for i in range(1, 44)]
    p = monter(tmp_path, lignes)
    assert _n(p) == 1, "43 clés du même match doivent peser une seule fois"


def test_deux_matchs_distincts_restent_distincts(tmp_path):
    """La dédup ne doit pas fusionner ce qui n'est pas le même match."""
    p = monter(tmp_path, [Opp(1, home="Sinner", away="Alcaraz"),
                          Opp(2, home="Djokovic", away="Zverev")])
    assert _n(p) == 2


def test_le_meme_match_un_AUTRE_JOUR_reste_distinct(tmp_path):
    """Le jour fait partie de la clé : deux confrontations à des dates
    différentes sont deux opportunités."""
    p = monter(tmp_path, [Opp(1, jour="2026-08-15"),
                          Opp(2, jour="2026-08-22")])
    assert _n(p) == 2


def test_deux_MARCHES_du_meme_match_restent_distincts(tmp_path):
    p = monter(tmp_path, [Opp(1, market="h2h", outcome="home"),
                          Opp(2, market="totals", outcome="over 2.5", line=2.5)])
    assert _n(p) == 2


def test_deux_PARIS_du_meme_marche_restent_distincts(tmp_path):
    p = monter(tmp_path, [Opp(1, outcome="home"), Opp(2, outcome="away")])
    assert _n(p) == 2


def test_deux_LIGNES_du_meme_total_restent_distinctes(tmp_path):
    p = monter(tmp_path, [Opp(1, market="totals", outcome="over 2.5", line=2.5),
                          Opp(2, market="totals", outcome="over 2.5", line=3.5)])
    assert _n(p) == 2


def test_une_ligne_NULLE_et_une_ligne_chiffree_ne_se_confondent_pas(tmp_path):
    """SQLite considère deux NULL comme distincts dans une comparaison : la clé
    les remplace par un sentinel pour que le groupement soit exact."""
    p = monter(tmp_path, [Opp(1, market="h2h", outcome="home", line=None),
                          Opp(2, market="h2h", outcome="home", line=0.0)])
    assert _n(p) == 2


# ── Le représentant : la meilleure cote ──────────────────────────────

def test_le_representant_porte_la_MEILLEURE_COTE(tmp_path):
    """C'est elle qui paie : sur un pari disponible à 2,10 et à 2,30, le prix
    retenu doit être 2,30."""
    p = monter(tmp_path, [Opp(1, book="unibet_be", odd=2.10),
                          Opp(2, book="ladbrokes_be", odd=2.30)])
    items = detail(str(p), Filtres())["items"]
    assert len(items) == 1
    assert items[0]["odds"] == 2.30
    assert items[0]["bookmaker"] == "ladbrokes_be"


def test_le_representant_est_stable_a_cote_egale(tmp_path):
    """Deux exécutions doivent rendre le même représentant : sans départage,
    l'ordre de SQLite pourrait varier et les chiffres bouger sans raison."""
    p = monter(tmp_path, [Opp(1, book="unibet_be", odd=2.20),
                          Opp(2, book="ladbrokes_be", odd=2.20)])
    vus = {detail(str(p), Filtres())["items"][0]["id"] for _ in range(5)}
    assert len(vus) == 1


# ── ⚠️ L'AGRÉGATION DES PROPRIÉTÉS DU GROUPE ─────────────────────────

def test_joue_est_agrege_sur_le_GROUPE_pas_sur_le_representant(tmp_path):
    """⚠️ LE PIÈGE PRINCIPAL. Le pari est cliqué chez Unibet à 2,10 ; Ladbrokes
    proposait 2,30, donc la dédup garde la ligne Ladbrokes — qui n'est PAS
    marquée jouée. Lire le drapeau sur le représentant classerait cette
    opportunité en « non jouée »."""
    p = monter(tmp_path, [Opp(1, book="unibet_be", odd=2.10, joue=True),
                          Opp(2, book="ladbrokes_be", odd=2.30, joue=False)])
    assert _n(p, joue="oui") == 1, "l'opportunité jouée a disparu"
    assert _n(p, joue="non") == 0, "elle est comptée deux fois"


def test_envoye_est_agrege_sur_le_GROUPE(tmp_path):
    """Même piège pour la notification : l'alerte part sur UN book, la dédup
    garde celui à la meilleure cote. Quand ce n'est pas le même, le groupe
    reste néanmoins ENVOYÉ."""
    p = monter(tmp_path, [
        Opp(1, book="unibet_be", odd=2.10, alerte="2026-08-15T10:05:00+00:00"),
        Opp(2, book="ladbrokes_be", odd=2.30, alerte=None)])
    assert _n(p, population=Population.SENT) == 1


def test_la_PREMIERE_alerte_du_groupe_est_retenue(tmp_path):
    """Une sélection ré-alertée porte plusieurs instants : c'est le premier qui
    décrit quand l'information est arrivée."""
    p = monter(tmp_path, [
        Opp(1, book="unibet_be", odd=2.10, alerte="2026-08-15T12:00:00+00:00"),
        Opp(2, book="ladbrokes_be", odd=2.30, alerte="2026-08-15T10:05:00+00:00")])
    assert detail(str(p), Filtres())["items"][0]["notified_at"] \
        == "2026-08-15T10:05:00+00:00"


def test_les_cinq_cles_d_un_match_tennis_partagent_le_drapeau_joue(tmp_path):
    """Le scénario de l'énoncé : cinq `event_key`, une seule jouée → le groupe
    est joué, et une seule opportunité en sort."""
    lignes = [Opp(i, sport="tennis", home="Sinner", away="Alcaraz",
                  heure=f"1{i}:00", odd=2.00 + i / 100, joue=(i == 2))
              for i in range(1, 6)]
    p = monter(tmp_path, lignes)
    assert _n(p, joue="oui") == 1
    assert _n(p, joue="non") == 0


def test_les_deux_lots_PARTITIONNENT_le_total(tmp_path):
    """joués + non joués = tout. Un pari dans les deux, ou dans aucun,
    fausserait toute comparaison entre les deux lots."""
    lignes = [Opp(i, home=f"A{i}", away=f"B{i}", joue=(i <= 4))
              for i in range(1, 11)]
    p = monter(tmp_path, lignes)
    tous, oui, non = _n(p), _n(p, joue="oui"), _n(p, joue="non")
    assert tous == 10 and oui == 4 and non == 6
    assert oui + non == tous


# ── Le filtre s'applique AVANT la dédup ──────────────────────────────

def test_un_filtre_de_book_change_le_representant(tmp_path):
    """« Le meilleur prix PARMI LES BOOKS QUE JE JOUE », et non le meilleur
    prix du marché : filtrer après la dédup rendrait un prix indisponible."""
    p = monter(tmp_path, [Opp(1, book="unibet_be", odd=2.10),
                          Opp(2, book="betano_be", odd=2.90)])
    items = detail(str(p), Filtres(books=("unibet_be",)))["items"]
    assert len(items) == 1 and items[0]["odds"] == 2.10


def test_une_opportunite_dont_un_exemplaire_est_dans_la_bande_survit(tmp_path):
    """⚠️ Le filtre de cote s'applique AVANT la dédup. Garder l'exemplaire à
    2,90 puis le jeter ferait disparaître une opportunité dont un exemplaire à
    2,10 était pourtant dans la bande demandée."""
    p = monter(tmp_path, [Opp(1, book="unibet_be", odd=2.10),
                          Opp(2, book="betano_be", odd=2.90)])
    items = detail(str(p), Filtres(cote_min=2.0, cote_max=2.5))["items"]
    assert len(items) == 1 and items[0]["odds"] == 2.10


def test_un_evenement_sans_equipes_ne_fusionne_pas_avec_un_autre(tmp_path):
    """Sans repli sur `event_key`, toutes les lignes sans équipes tomberaient
    dans un même groupe et se fondraient en une seule opportunité."""
    import sqlite3
    p = monter(tmp_path, [Opp(1, home="A", away="B"), Opp(2, home="C", away="D")])
    con = sqlite3.connect(str(p))
    con.execute("DELETE FROM events")       # les deux perdent leurs équipes
    con.commit(); con.close()
    assert _n(p) == 2, "deux matchs sans équipes se sont confondus"
