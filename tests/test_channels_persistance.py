"""Les canaux en base, et leur conversion vers le modèle de décision.

Trois exigences se croisent ici :

  * la persistance ne doit RIEN perdre — une base ancienne gagne les trois
    tables à la réouverture, sans toucher à ses lignes ;
  * la conversion doit rendre des objets que `routing.canaux_pour` accepte
    tels quels, sinon les deux moitiés du système divergeraient sans que
    rien ne le dise ;
  * rien de tout cela ne doit encore router quoi que ce soit.

Deux garde-fous portent le vrai risque :

  * `test_supprimer_un_canal_supprime_ses_regles` — `PRAGMA foreign_keys`
    est désactivé dans ce projet, donc aucun ON DELETE CASCADE ne se
    déclenche. Des règles orphelines seraient réattribuées au prochain
    canal recevant le même id ;
  * `test_un_canal_invalide_est_saute_et_signale` — ce chargement tournera
    dans le cycle du daemon. Une ligne écrite à la main ne doit pas
    pouvoir arrêter toutes les alertes.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import pytest

from src.channels import charger
from src.routing import Borne, Canal, Critere, Regle, canaux_pour
from src.storage import Storage

TABLES = ("channels", "channel_rules", "channel_rule_values")


@dataclass(frozen=True)
class _Pari:
    ev_pct: float = 10.0
    odd_taken: float = 2.0
    book: str = "unibet_be"
    market: str = "h2h"


@pytest.fixture
def st(tmp_path) -> Storage:
    return Storage(str(tmp_path / "t.db"))


def _tables(chemin) -> set[str]:
    with sqlite3.connect(str(chemin)) as c:
        return {r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}


# ══ 1-4 : les tables et la migration ═══════════════════════════════════
def test_les_trois_tables_sont_creees(st):
    assert set(TABLES) <= _tables(st.path)


def test_migration_sur_une_base_existante(tmp_path):
    """Une base d'avant ce commit doit gagner les tables à la réouverture."""
    chemin = tmp_path / "ancienne.db"
    Storage(str(chemin))
    with sqlite3.connect(str(chemin)) as c:
        for t in TABLES:
            c.execute(f"DROP TABLE {t}")
    assert not (set(TABLES) & _tables(chemin))

    Storage(str(chemin))
    assert set(TABLES) <= _tables(chemin)


def test_la_migration_est_idempotente(tmp_path):
    """Trois ouvertures successives : aucune erreur, aucune perte."""
    chemin = tmp_path / "t.db"
    st = Storage(str(chemin))
    cid = st.create_channel("CHAT", "TENNIS")
    st.add_channel_rule(cid, ev_min=10.0)
    for _ in range(3):
        st = Storage(str(chemin))
    assert len(st.load_channel_rows()[0]) == 1
    assert len(st.load_channel_rows()[1]) == 1


def test_les_donnees_existantes_survivent_a_la_reouverture(tmp_path):
    """Aucune suppression implicite : ni les canaux, ni les autres tables."""
    chemin = tmp_path / "t.db"
    st = Storage(str(chemin))
    st.upsert_event("202609011800::a__vs__b", "soccer", "Pro League", "a", "b",
                    __import__("datetime").datetime(2026, 9, 1, 18, 0))
    cid = st.create_channel("CHAT", "TENNIS", priorite=7, exclusif=True)
    rid = st.add_channel_rule(cid, ev_min=10.0, odd_max=4.0, phase="prematch")
    st.add_rule_value(rid, "sport", "tennis")

    st2 = Storage(str(chemin))
    canaux, regles, valeurs = st2.load_channel_rows()
    assert len(canaux) == 1 and len(regles) == 1 and len(valeurs) == 1
    with sqlite3.connect(str(chemin)) as c:
        assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1


# ══ 5-7 : creation, regles, dimensions ═════════════════════════════════
def test_creation_d_un_canal(st):
    cid = st.create_channel("-100123", "TENNIS", priorite=10)
    ligne = st.find_channel_by_name("TENNIS")
    assert ligne["id"] == cid
    assert ligne["chat_id"] == "-100123"
    assert (ligne["actif"], ligne["priorite"], ligne["exclusif"]) == (1, 10, 0)


def test_le_nom_d_un_canal_est_unique(st):
    st.create_channel("A", "TENNIS")
    with pytest.raises(sqlite3.IntegrityError):
        st.create_channel("B", "TENNIS")


def test_un_canal_avec_plusieurs_regles(st):
    cid = st.create_channel("CHAT", "CRITIQUE")
    st.add_channel_rule(cid, ev_min=35.0)
    st.add_channel_rule(cid, ev_min=20.0, odd_min=4.0, odd_min_strict=True)
    canal = charger(st)[0]
    assert len(canal.regles) == 2


def test_une_regle_avec_plusieurs_valeurs_par_dimension(st):
    cid = st.create_channel("CHAT", "MULTI")
    rid = st.add_channel_rule(cid)
    for sport in ("tennis", "soccer"):
        st.add_rule_value(rid, "sport", sport)
    for book in ("unibet_be", "starcasino_sport"):
        st.add_rule_value(rid, "book", book)
    st.add_rule_value(rid, "market", "h2h")
    st.add_rule_value(rid, "league", "Belgique - Pro League")

    regle = charger(st)[0].regles[0]
    par_dim = {c.dimension: c for c in regle.criteres}
    assert par_dim["sport"].valeurs == frozenset({"tennis", "soccer"})
    assert par_dim["book"].valeurs == frozenset({"unibet_be", "starcasino_sport"})
    assert par_dim["market"].valeurs == frozenset({"h2h"})
    assert par_dim["league"].valeurs == frozenset({"belgique - pro league"})


def test_une_dimension_inconnue_est_refusee_a_l_ecriture(st):
    """Validé à la source : une ligne invalide en base ne se manifesterait
    qu'au chargement, dans le cycle, loin de la commande fautive."""
    cid = st.create_channel("CHAT", "C")
    rid = st.add_channel_rule(cid)
    with pytest.raises(ValueError, match="dimension inconnue"):
        st.add_rule_value(rid, "competition", "x")


def test_une_phase_inconnue_est_refusee_a_l_ecriture(st):
    cid = st.create_channel("CHAT", "C")
    with pytest.raises(ValueError, match="phase inconnue"):
        st.add_channel_rule(cid, phase="mi-temps")


def test_reecrire_une_valeur_ne_cree_pas_de_doublon(st):
    cid = st.create_channel("CHAT", "C")
    rid = st.add_channel_rule(cid)
    st.add_rule_value(rid, "sport", "tennis", inclut=True)
    st.add_rule_value(rid, "sport", "tennis", inclut=False)
    valeurs = st.load_channel_rows()[2]
    assert len(valeurs) == 1 and valeurs[0]["inclut"] == 0


# ══ 8-10 : inclusion, exclusion, bornes, phase ═════════════════════════
def test_inclusion_et_exclusion_sont_conservees(st):
    cid = st.create_channel("CHAT", "C")
    rid = st.add_channel_rule(cid)
    st.add_rule_value(rid, "sport", "tennis", inclut=True)
    st.add_rule_value(rid, "book", "elitesports", inclut=False)
    par_dim = {c.dimension: c for c in charger(st)[0].regles[0].criteres}
    assert par_dim["sport"].inclut is True
    assert par_dim["book"].inclut is False


def test_les_bornes_et_leur_strictesse_sont_conservees(st):
    cid = st.create_channel("CHAT", "C")
    st.add_channel_rule(cid, ev_min=5.0, ev_max=8.0, ev_max_strict=True,
                        odd_min=4.0, odd_min_strict=True, odd_max=6.0)
    r = charger(st)[0].regles[0]
    assert r.ev_min == Borne(5.0, stricte=False)
    assert r.ev_max == Borne(8.0, stricte=True)
    assert r.odd_min == Borne(4.0, stricte=True)
    assert r.odd_max == Borne(6.0, stricte=False)


def test_une_borne_absente_reste_absente(st):
    cid = st.create_channel("CHAT", "C")
    st.add_channel_rule(cid, ev_min=10.0)
    r = charger(st)[0].regles[0]
    assert r.ev_min == Borne(10.0)
    assert (r.ev_max, r.odd_min, r.odd_max) == (None, None, None)


def test_une_borne_a_zero_n_est_pas_confondue_avec_une_absence(st):
    """0.0 est faux en Python. Un test sur la fausseté au lieu de None
    effacerait une borne parfaitement légitime."""
    cid = st.create_channel("CHAT", "C")
    st.add_channel_rule(cid, ev_min=0.0)
    assert charger(st)[0].regles[0].ev_min == Borne(0.0)


def test_la_phase_est_conservee(st):
    cid = st.create_channel("CHAT", "C")
    st.add_channel_rule(cid, phase="live")
    st.add_channel_rule(cid, phase="prematch")
    st.add_channel_rule(cid)
    phases = [r.phase for r in charger(st)[0].regles]
    assert phases == ["live", "prematch", None]


# ══ 11-13 : actif, priorite, exclusivite ═══════════════════════════════
def test_actif_et_inactif_sont_conserves(st):
    a = st.create_channel("A", "ACTIF")
    i = st.create_channel("B", "INACTIF", actif=False)
    st.add_channel_rule(a)
    st.add_channel_rule(i)
    par_nom = {c.nom: c for c in charger(st)}
    assert par_nom["ACTIF"].actif is True
    assert par_nom["INACTIF"].actif is False


def test_un_canal_inactif_est_charge_mais_ignore_par_le_routage(st):
    """Chargé, donc éditable et visible ; ignoré, donc muet."""
    i = st.create_channel("B", "INACTIF", actif=False)
    st.add_channel_rule(i)
    canaux = charger(st)
    assert len(canaux) == 1
    assert canaux_pour(_Pari(), canaux=canaux) == []

    st.set_channel_active(i, True)
    assert [c.nom for c in canaux_pour(_Pari(), canaux=charger(st))] == ["INACTIF"]


def test_la_priorite_est_conservee_et_ordonne(st):
    for nom, prio in (("TARD", 90), ("TOT", 10)):
        cid = st.create_channel(nom, nom, priorite=prio)
        st.add_channel_rule(cid)
    assert [c.nom for c in canaux_pour(_Pari(), canaux=charger(st))] == ["TOT", "TARD"]


def test_l_exclusivite_est_conservee_et_agit(st):
    p = st.create_channel("P", "PREMIUM", priorite=10, exclusif=True)
    c = st.create_channel("C", "CRITIQUE", priorite=20)
    st.add_channel_rule(p)
    st.add_channel_rule(c)
    canaux = charger(st)
    assert {x.nom for x in canaux if x.exclusif} == {"PREMIUM"}
    assert [x.nom for x in canaux_pour(_Pari(), canaux=canaux)] == ["PREMIUM"]


def test_profile_id_est_conserve_sans_effet(st):
    a = st.create_channel("A", "MOI")
    b = st.create_channel("B", "AUTRE", profile_id=42)
    st.add_channel_rule(a)
    st.add_channel_rule(b)
    par_nom = {c.nom: c for c in charger(st)}
    assert par_nom["MOI"].profile_id is None
    assert par_nom["AUTRE"].profile_id == 42
    # Aucun filtrage par profil en V1 : les deux passent.
    assert len(canaux_pour(_Pari(), canaux=charger(st))) == 2


# ══ 14 : le chargement complet ═════════════════════════════════════════
def test_chargement_complet_vers_canaux_pour(st):
    """De bout en bout : trois canaux en base, un pari, trois destinations
    décidées par le module pur."""
    t = st.create_channel("T", "TENNIS", priorite=10)
    rt = st.add_channel_rule(t, ev_min=10.0, odd_max=4.0)
    st.add_rule_value(rt, "sport", "tennis")

    g = st.create_channel("G", "GROSSES_EV", priorite=20)
    st.add_channel_rule(g, ev_min=20.0)

    u = st.create_channel("U", "UNIBET", priorite=30)
    ru = st.add_channel_rule(u, ev_min=10.0)
    st.add_rule_value(ru, "book", "unibet_be")

    canaux = charger(st)
    assert all(isinstance(c, Canal) for c in canaux)

    pari = _Pari(ev_pct=25.0, odd_taken=3.0, book="unibet_be")
    assert [c.nom for c in canaux_pour(pari, sport="tennis", canaux=canaux)] == [
        "TENNIS", "GROSSES_EV", "UNIBET"]
    # Le meme pari en soccer : le canal tennis ne le prend plus.
    assert [c.nom for c in canaux_pour(pari, sport="soccer", canaux=canaux)] == [
        "GROSSES_EV", "UNIBET"]


def test_un_canal_sans_regle_est_charge_mais_ne_prend_rien(st):
    st.create_channel("CHAT", "NEUF")
    canaux = charger(st)
    assert len(canaux) == 1 and canaux[0].regles == ()
    assert canaux_pour(_Pari(), canaux=canaux) == []


def test_une_base_vide_rend_une_liste_vide(st):
    assert charger(st) == []


# ══ suppression : rien d'implicite ═════════════════════════════════════
def test_supprimer_un_canal_supprime_ses_regles(st):
    """`PRAGMA foreign_keys` est désactivé : sans suppression explicite, les
    règles survivraient au canal et seraient réattribuées au prochain canal
    recevant le même id."""
    cid = st.create_channel("CHAT", "C")
    rid = st.add_channel_rule(cid, ev_min=10.0)
    st.add_rule_value(rid, "sport", "tennis")

    st.delete_channel(cid)
    canaux, regles, valeurs = st.load_channel_rows()
    assert (canaux, regles, valeurs) == ([], [], [])


def test_supprimer_un_canal_n_en_touche_aucun_autre(st):
    a = st.create_channel("A", "A")
    ra = st.add_channel_rule(a, ev_min=10.0)
    st.add_rule_value(ra, "sport", "tennis")
    b = st.create_channel("B", "B")
    rb = st.add_channel_rule(b, ev_min=20.0)
    st.add_rule_value(rb, "sport", "soccer")

    st.delete_channel(a)
    restants = charger(st)
    assert [c.nom for c in restants] == ["B"]
    assert restants[0].regles[0].criteres[0].valeurs == frozenset({"soccer"})


def test_supprimer_une_regle_supprime_ses_valeurs(st):
    cid = st.create_channel("CHAT", "C")
    r1 = st.add_channel_rule(cid, ev_min=10.0)
    st.add_rule_value(r1, "sport", "tennis")
    r2 = st.add_channel_rule(cid, ev_min=20.0)
    st.add_rule_value(r2, "sport", "soccer")

    st.delete_channel_rule(r1)
    canal = charger(st)[0]
    assert len(canal.regles) == 1
    assert canal.regles[0].criteres[0].valeurs == frozenset({"soccer"})


# ══ robustesse du chargement ═══════════════════════════════════════════
def test_un_canal_invalide_est_saute_et_signale(st):
    """Ce chargement tournera dans le cycle. Une ligne écrite à la main ne
    doit pas pouvoir arrêter toutes les alertes — mais elle doit se voir."""
    bon = st.create_channel("A", "BON")
    st.add_channel_rule(bon, ev_min=10.0)
    mauvais = st.create_channel("B", "MAUVAIS")
    rid = st.add_channel_rule(mauvais)
    with sqlite3.connect(str(st.path)) as c:   # contourne la validation
        c.execute("INSERT INTO channel_rule_values(rule_id, dimension, valeur,"
                  " inclut) VALUES (?,?,?,?)", (rid, "competition", "x", 1))

    messages: list[str] = []
    canaux = charger(st, print_fn=messages.append)
    assert [c.nom for c in canaux] == ["BON"]
    assert len(messages) == 1
    assert "MAUVAIS" in messages[0] and "dimension inconnue" in messages[0]


def test_deux_criteres_contradictoires_sautent_le_canal(st):
    """« sport inclut tennis » ET « sport exclut soccer » sous la même règle
    donneraient deux critères sur la même dimension, que `Regle` refuse."""
    cid = st.create_channel("A", "CONTRADICTOIRE")
    rid = st.add_channel_rule(cid)
    st.add_rule_value(rid, "sport", "tennis", inclut=True)
    st.add_rule_value(rid, "sport", "soccer", inclut=False)
    messages: list[str] = []
    assert charger(st, print_fn=messages.append) == []
    assert "même dimension" in messages[0]


def test_le_chargement_ne_fait_pas_de_N_plus_1(st):
    """Trois requêtes, quel que soit le nombre de canaux : ce chargement
    tournera sur le chemin le plus chaud du daemon."""
    for i in range(12):
        cid = st.create_channel(f"C{i}", f"CANAL_{i}")
        for _ in range(3):
            rid = st.add_channel_rule(cid, ev_min=10.0)
            st.add_rule_value(rid, "sport", "tennis")

    appels = {"n": 0}
    vrai = Storage.load_channel_rows

    class _Compte(Storage):
        def load_channel_rows(self):
            appels["n"] += 1
            return vrai(self)

    st2 = _Compte(str(st.path))
    canaux = charger(st2)
    assert len(canaux) == 12
    assert appels["n"] == 1


# ══ non-regression : rien n'est branche ════════════════════════════════
def test_le_daemon_ne_charge_encore_aucun_canal():
    """Le commit 3 ne branche rien : `channels` et `routing` ne sont importés
    par aucun module de production."""
    import pathlib
    racine = pathlib.Path(__file__).resolve().parent.parent
    for nom in ("src/main.py", "src/alerter.py", "bot_listener.py"):
        source = (racine / nom).read_text(encoding="utf-8")
        assert "src.channels" not in source and "src.routing" not in source, nom
        assert "load_channel_rows" not in source, nom
