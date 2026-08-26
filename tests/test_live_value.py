"""Le moteur LIVE : ce qu'il détecte, et surtout ce qu'il REFUSE de détecter.

Chaque garde-fou a ici un test qui échoue si on l'ôte. C'est la seule façon
de savoir qu'il sert encore : un garde-fou sans test qui le falsifie est une
intention, pas une protection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.live_value import (
    AGE_MAX_FAIR_SEC, AGE_MAX_PRENEUR_SEC, OVERROUND_PRENEUR_MAX,
    SEUIL_EV_PCT, Memoire, Statut, construire_fair, evaluer, resume)
from src.asianodds_live import LiveRow
from src.matcher import event_key
from src.models import Book, MarketType, OddQuote, Outcome
from src.storage import Storage

MAINTENANT = datetime(2026, 8, 26, 20, 0, 0, tzinfo=timezone.utc)
DEBUT = MAINTENANT - timedelta(minutes=30)
CLE = event_key("Orebro SK", "Varbergs BoIS", DEBUT)


def _db(tmp_path) -> Storage:
    return Storage(tmp_path / "t.db")


def _fair(storage, *, cotes=(3.00, 3.40, 2.60), market=MarketType.H2H,
          line=None, labels=("home", "draw", "away"), observed=None,
          feed_score="1:0", scores=None, source="1634601234",
          inverse=False, cle=CLE) -> None:
    """Écrire une ligne juste AsianOdds telle que le collecteur l'écrit.

    On passe par `LiveRow.as_upsert_row` et `upsert_live_state` — le VRAI
    chemin d'écriture. Fabriquer les tuples à la main ferait tester le moteur
    contre un format que la production n'écrit pas.
    """
    observed = observed or (MAINTENANT - timedelta(seconds=5))
    rows = []
    for i, (label, cote) in enumerate(zip(labels, cotes)):
        rows.append(LiveRow(
            event_key=cle, market=market, outcome_label=label, line=line,
            odd=cote, observed_at=observed, home_score=1, away_score=0,
            feed_score=(scores[i] if scores else feed_score), igm=55,
            league="Superettan", source_event_id=source,
            source_inverse=inverse, matched_at=observed,
        ).as_upsert_row(MAINTENANT))
    storage.upsert_live_state(rows)


def _cote(odd=4.00, *, market=MarketType.H2H, label="home", line=None,
          book=Book.UNIBET_BE, live=True, fetched=None, cle=CLE,
          source="9001") -> OddQuote:
    return OddQuote(
        event_key=cle, book=book, market=market,
        outcome=Outcome(label=label, line=line), decimal_odd=odd,
        fetched_at=fetched or (MAINTENANT - timedelta(seconds=1)),
        source_event_id=source, from_live_feed=live)


def _cote_pour_ev(cible_pct, storage, **kw):
    """La cote qui produit EXACTEMENT `cible_pct` d'EV sur la ligne juste.

    Calculée depuis la probabilité déviguée réelle, et non devinée : un
    nombre écrit à la main dériverait au premier changement de méthode de
    devig, et le test au seuil ne testerait plus le seuil.
    """
    g = construire_fair(storage, MAINTENANT)[0][(CLE, MarketType.H2H, None)]
    return (1.0 + cible_pct / 100.0) / g.probs["home"]


# ══ 1-3 : le seuil d'EV ═════════════════════════════════════════════════
@pytest.mark.parametrize("ev_cible,attendu", [
    (9.9, False),    # sous le seuil → aucune occasion
    (10.0, False),   # AU seuil → refusée, et c'est un choix explicite
    (10.1, True),    # au-dessus → occasion
])
def test_le_seuil_d_ev_est_strict(tmp_path, ev_cible, attendu):
    """Le comportement AU seuil n'est pas laissé au hasard d'un `>=`.

    10,00 % pile ne passe pas. Ce n'est pas plus juste que l'inverse, mais
    c'est décidé, écrit et figé — sinon un jour quelqu'un « nettoie » le
    comparateur et le seuil change sans que rien ne le dise.
    """
    db = _db(tmp_path)
    _fair(db)
    q = _cote(_cote_pour_ev(ev_cible, db))
    a = evaluer([q], db, MAINTENANT)
    assert bool(a.opportunites) is attendu
    if attendu:
        assert a.opportunites[0].ev_pct == pytest.approx(ev_cible, abs=1e-6)
    else:
        assert a.sous_seuil == 1


# ══ 4 : le handicap n'existe pas pour ce moteur ═════════════════════════
def test_un_handicap_rentable_reste_invisible(tmp_path):
    """La convention de ligne d'Unibet face à celle d'AsianOdds n'est pas
    vérifiée. Un handicap mal orienté produit une EV énorme et fausse : ce
    n'est pas une occasion ratée, c'est un pari perdu."""
    db = _db(tmp_path)
    _fair(db, market=MarketType.HANDICAP, cotes=(1.90, 1.90),
          labels=("home", "away"), line=-1.0)
    a = evaluer([_cote(9.99, market=MarketType.HANDICAP, line=-1.0)],
                db, MAINTENANT)
    assert a.opportunites == []
    assert a.quotes_analysees == 0
    assert "marché hors périmètre : handicap" in a.ecartees
    # Et la ligne juste elle-même n'a jamais été construite.
    assert construire_fair(db, MAINTENANT)[0] == {}


def test_les_mi_temps_sont_dehors_aussi(tmp_path):
    """`TOTALS_H1` porte les mêmes labels et les mêmes lignes que `TOTALS`.
    Rien ne garantit qu'Unibet et AsianOdds découpent la mi-temps pareil."""
    db = _db(tmp_path)
    _fair(db, market=MarketType.TOTALS_H1, cotes=(2.00, 1.90),
          labels=("over", "under"), line=1.5)
    assert construire_fair(db, MAINTENANT)[0] == {}


# ══ 5 : un autre bookmaker ═════════════════════════════════════════════
def test_la_cote_d_un_autre_book_est_rejetee(tmp_path):
    """Betano est volontairement hors périmètre. Une cote qui arriverait
    d'ailleurs ne doit pas être prise pour de l'Unibet LIVE."""
    db = _db(tmp_path)
    _fair(db)
    a = evaluer([_cote(4.00, book=Book.BETANO_BE)], db, MAINTENANT)
    assert a.opportunites == []
    assert a.quotes_analysees == 0
    assert "book hors périmètre : betano_be" in a.ecartees


def test_une_cote_UNIBET_PREMATCH_n_est_jamais_prise_pour_du_live(tmp_path):
    """LE piège du commit : le prématch et le LIVE portent le MÊME book.

    `Book.UNIBET_BE` ne distingue rien. Seul `from_live_feed`, posé par
    `unibet_live.apparier`, sépare une cote sondée à la seconde d'une cote
    de cycle vieille d'une minute. Sans ce contrôle, une cote prématch
    servirait de preneur face à une fair line LIVE — et l'EV mesurerait le
    retard de notre propre collecte, pas une opportunité.
    """
    db = _db(tmp_path)
    _fair(db)
    a = evaluer([_cote(4.00, live=False)], db, MAINTENANT)
    assert a.opportunites == []
    assert "cote prématch (from_live_feed faux)" in a.ecartees


# ══ 6-7 : fraîcheur ════════════════════════════════════════════════════
def test_une_fair_line_trop_ancienne_est_rejetee(tmp_path):
    db = _db(tmp_path)
    _fair(db, observed=MAINTENANT - timedelta(seconds=AGE_MAX_FAIR_SEC + 1))
    a = evaluer([_cote(4.00)], db, MAINTENANT)
    o = a.opportunites[0]
    assert o.statut is Statut.REJET_FAIR_PERIMEE
    assert not o.exploitable
    assert o.age_fair_sec == pytest.approx(AGE_MAX_FAIR_SEC + 1)


def test_la_fraicheur_fair_se_juge_sur_la_jambe_la_plus_VIEILLE(tmp_path):
    """Le devig mêle les trois issues : sa fraîcheur est celle de la plus
    ancienne. Prendre la plus fraîche ferait passer pour neuf un prix juste
    calculé sur une jambe figée depuis dix minutes."""
    db = _db(tmp_path)
    _fair(db, labels=("home",), cotes=(3.00,),
          observed=MAINTENANT - timedelta(seconds=600))
    _fair(db, labels=("draw", "away"), cotes=(3.40, 2.60),
          observed=MAINTENANT - timedelta(seconds=1))
    o = evaluer([_cote(4.00)], db, MAINTENANT).opportunites[0]
    assert o.age_fair_sec == pytest.approx(600, abs=1)
    assert o.statut is Statut.REJET_FAIR_PERIMEE


def test_une_cote_unibet_trop_ancienne_est_rejetee(tmp_path):
    """L'instantané Unibet est CONSERVÉ quand un sondage échoue — c'est voulu,
    il porte son âge. C'est ici qu'on refuse de s'en servir."""
    db = _db(tmp_path)
    _fair(db)
    vieille = _cote(4.00,
                    fetched=MAINTENANT - timedelta(seconds=AGE_MAX_PRENEUR_SEC + 1))
    o = evaluer([vieille], db, MAINTENANT).opportunites[0]
    assert o.statut is Statut.REJET_COTE_PERIMEE
    assert not o.exploitable
    assert o.age_preneur_sec == pytest.approx(AGE_MAX_PRENEUR_SEC + 1)


# ══ 8 : le score ═══════════════════════════════════════════════════════
def test_un_score_devenu_incoherent_est_rejete(tmp_path):
    """« Je préfère perdre une opportunité plutôt que générer une fausse
    alerte après un but. » AsianOdds dit 1:0, Unibet a coté à 0:0 : l'un des
    deux prix précède le but. L'EV qui les compare n'a jamais existé."""
    db = _db(tmp_path)
    _fair(db, feed_score="1:0")
    a = evaluer([_cote(4.00)], db, MAINTENANT,
                scores_preneur={"9001": "0:0"})
    o = a.opportunites[0]
    assert o.statut is Statut.REJET_SCORE_INCOHERENT
    assert not o.exploitable
    assert o.feed_score == "1:0" and o.score_preneur == "0:0"


def test_scores_concordants_donc_exploitable(tmp_path):
    """La contre-épreuve du test précédent : sans elle, un moteur qui rejette
    TOUT passerait le test du rejet et ne détecterait jamais rien."""
    db = _db(tmp_path)
    _fair(db, feed_score="1:0")
    o = evaluer([_cote(4.00)], db, MAINTENANT,
                scores_preneur={"9001": "1:0"}).opportunites[0]
    assert o.statut is Statut.RETENUE
    assert o.exploitable


def test_score_preneur_inconnu_detecte_mais_NON_exploitable(tmp_path):
    """L'état réel de ce commit : le collecteur Unibet n'expose pas le score.

    On ne suppose donc pas qu'il concorde. L'occasion est vue et affichée,
    mais elle ne franchira aucune porte — c'est le comportement demandé tant
    que la logique n'est pas démontrée sûre.
    """
    db = _db(tmp_path)
    _fair(db)
    o = evaluer([_cote(4.00)], db, MAINTENANT).opportunites[0]
    assert o.statut is Statut.OBSERVEE_SCORE_INCONNU
    assert not o.exploitable
    assert o.score_preneur is None


def test_un_groupe_fair_aux_scores_DISCORDANTS_est_ecarte(tmp_path):
    """Deux jambes du même 1X2 estampillées de deux scores : le devig
    porterait sur deux états du match à la fois. Ce contrôle-là ne dépend
    d'aucune donnée qu'on n'aurait pas — il est actif dès maintenant."""
    db = _db(tmp_path)
    _fair(db, scores=("1:0", "0:0", "1:0"))
    groupes, motifs = construire_fair(db, MAINTENANT)
    assert groupes == {}
    assert motifs["score incohérent dans le groupe fair"] == 1
    assert evaluer([_cote(4.00)], db, MAINTENANT).opportunites == []


# ══ 9 : orientation ════════════════════════════════════════════════════
def test_l_orientation_home_away_est_respectee(tmp_path):
    """Le sens compte : la cote « home » doit être jugée contre la
    probabilité « home », jamais contre « away ». Une permutation ici
    passerait inaperçue sur un match équilibré et exploserait sur un
    déséquilibré — exactement le défaut corrigé côté AsianOdds."""
    db = _db(tmp_path)
    # Déséquilibre franc : home très probable, away très improbable.
    _fair(db, cotes=(1.20, 6.00, 15.00))
    g = construire_fair(db, MAINTENANT)[0][(CLE, MarketType.H2H, None)]
    assert g.probs["home"] > g.probs["away"]

    # Une cote de 2.00 sur le favori est une énorme value ; la même cote sur
    # l'outsider n'en est pas une du tout.
    sur_home = evaluer([_cote(2.00, label="home")], db, MAINTENANT)
    sur_away = evaluer([_cote(2.00, label="away")], db, MAINTENANT)
    assert sur_home.opportunites and sur_home.opportunites[0].outcome == "home"
    assert sur_away.opportunites == []
    assert sur_away.sous_seuil == 1


def test_l_inversion_annoncee_par_asianodds_est_tracee(tmp_path):
    db = _db(tmp_path)
    _fair(db, inverse=True)
    o = evaluer([_cote(4.00)], db, MAINTENANT).opportunites[0]
    assert o.fair_inverse is True
    assert o.source_event_id_fair == "1634601234"
    assert o.source_event_id_preneur == "9001"


# ══ 10 : les totaux ════════════════════════════════════════════════════
def test_un_total_est_associe_a_SA_ligne_et_pas_a_une_autre(tmp_path):
    """Over 2.5 et over 3.5 ne se comparent pas. Sans la ligne dans la clé,
    une cote « over 3.5 » se ferait juger contre la juste de 2.5 — et
    paraîtrait toujours généreuse."""
    db = _db(tmp_path)
    _fair(db, market=MarketType.TOTALS, cotes=(1.95, 1.85),
          labels=("over", "under"), line=2.5)
    q_bonne = _cote(2.40, market=MarketType.TOTALS, label="over", line=2.5)
    q_autre = _cote(2.40, market=MarketType.TOTALS, label="over", line=3.5)

    a = evaluer([q_bonne], db, MAINTENANT)
    assert a.opportunites and a.opportunites[0].line == 2.5

    b = evaluer([q_autre], db, MAINTENANT)
    assert b.opportunites == []
    assert "aucune ligne juste AsianOdds" in b.ecartees


def test_un_total_ne_depend_pas_de_l_orientation(tmp_path):
    """« Plus de 2,5 buts » ne dépend pas de qui reçoit. L'invariance est
    établie côté AsianOdds ; ce test interdit de la casser ici."""
    db = _db(tmp_path)
    _fair(db, market=MarketType.TOTALS, cotes=(1.95, 1.85),
          labels=("over", "under"), line=2.5, inverse=True)
    o = evaluer([_cote(2.40, market=MarketType.TOTALS, label="over",
                       line=2.5)], db, MAINTENANT).opportunites[0]
    assert o.outcome == "over" and o.line == 2.5


# ══ 11 : groupe incomplet / overround ══════════════════════════════════
def test_un_groupe_fair_incomplet_est_ecarte(tmp_path):
    """Le devig normalise à 100 % quoi qu'on lui donne : un 1X2 amputé du nul
    rendrait des probabilités d'apparence normale et une EV inventée."""
    db = _db(tmp_path)
    _fair(db, cotes=(3.00, 3.40), labels=("home", "draw"))
    groupes, motifs = construire_fair(db, MAINTENANT)
    assert groupes == {}
    assert motifs["groupe incomplet"] == 1


def test_un_overround_aberrant_est_ecarte(tmp_path):
    """Le contrôle de marge de la référence prématch, réutilisé tel quel :
    au-delà, ce n'est plus un prix de marché."""
    db = _db(tmp_path)
    _fair(db, cotes=(1.05, 1.05, 1.05))
    groupes, motifs = construire_fair(db, MAINTENANT)
    assert groupes == {}
    assert motifs["overround invalide"] == 1


def test_un_groupe_fair_SAIN_produit_bien_une_ligne(tmp_path):
    """Contre-épreuve des deux précédents."""
    db = _db(tmp_path)
    _fair(db)
    groupes, motifs = construire_fair(db, MAINTENANT)
    assert motifs == {}
    g = groupes[(CLE, MarketType.H2H, None)]
    assert sum(g.probs.values()) == pytest.approx(1.0)
    assert set(g.probs) == {"home", "draw", "away"}


# ══ 12 : déduplication ═════════════════════════════════════════════════
def test_la_meme_occasion_n_est_pas_resignalee_a_chaque_sondage(tmp_path):
    db = _db(tmp_path)
    _fair(db)
    m = Memoire()
    q = _cote(4.00)
    assert evaluer([q], db, MAINTENANT, memoire=m).nouvelles
    for _ in range(5):
        a = evaluer([q], db, MAINTENANT, memoire=m)
        assert a.nouvelles == []
        assert a.opportunites[0].statut is Statut.DOUBLON


def test_une_EV_qui_bouge_assez_est_resignalee(tmp_path):
    db = _db(tmp_path)
    _fair(db)
    m = Memoire()
    evaluer([_cote(_cote_pour_ev(11.0, db))], db, MAINTENANT, memoire=m)
    # +1,5 point : sous le seuil de ré-émission, on se tait.
    assert evaluer([_cote(_cote_pour_ev(12.5, db))], db, MAINTENANT,
                   memoire=m).nouvelles == []
    # +4 points depuis la dernière SIGNALÉE : on reparle.
    assert evaluer([_cote(_cote_pour_ev(15.0, db))], db, MAINTENANT,
                   memoire=m).nouvelles


def test_une_occasion_qui_disparait_puis_revient_est_resignalee(tmp_path):
    """Sans l'oubli des clés absentes, une occasion vue une fois ne
    reparlerait plus jamais tant que son EV ne saute pas de deux points —
    même après avoir disparu une demi-heure."""
    db = _db(tmp_path)
    _fair(db)
    m = Memoire()
    q = _cote(4.00)
    assert evaluer([q], db, MAINTENANT, memoire=m).nouvelles
    assert evaluer([q], db, MAINTENANT, memoire=m).nouvelles == []
    assert evaluer([], db, MAINTENANT, memoire=m).opportunites == []   # disparue
    assert evaluer([q], db, MAINTENANT, memoire=m).nouvelles           # revenue


def test_la_cle_de_dedup_distingue_ligne_issue_et_book(tmp_path):
    """Deux sélections différentes ne doivent pas s'éclipser l'une l'autre."""
    db = _db(tmp_path)
    _fair(db, market=MarketType.TOTALS, cotes=(1.95, 1.85),
          labels=("over", "under"), line=2.5)
    _fair(db, market=MarketType.TOTALS, cotes=(2.60, 1.50),
          labels=("over", "under"), line=3.5)
    m = Memoire()
    a = evaluer([_cote(2.40, market=MarketType.TOTALS, label="over", line=2.5),
                 _cote(2.40, market=MarketType.TOTALS, label="under", line=2.5),
                 _cote(3.60, market=MarketType.TOTALS, label="over", line=3.5)],
                db, MAINTENANT, memoire=m)
    cles = {o.cle for o in a.nouvelles}
    assert len(cles) == len(a.nouvelles) >= 2


# ══ 13 : horodatages manquants ═════════════════════════════════════════
def test_un_observed_at_absent_donne_N_A_et_ne_plante_pas(tmp_path):
    """Une colonne vide dégrade la détection ; elle n'arrête pas le moteur.

    Et un âge INCONNU n'est pas un âge NUL : ne pouvant pas certifier la
    fraîcheur, on rejette — sans jamais afficher « 0.0 s », qui se lirait
    comme frais.
    """
    db = _db(tmp_path)
    _fair(db)
    with db._conn() as c:
        c.execute("UPDATE market_state SET observed_at = NULL")
    o = evaluer([_cote(4.00)], db, MAINTENANT).opportunites[0]
    assert o.age_fair_sec is None
    assert o.statut is Statut.REJET_FAIR_PERIMEE
    assert "asianodds_age=N/As" in o.ligne()
    assert o.motif == "observed_at absent"


def test_un_observed_at_ILLISIBLE_ne_plante_pas(tmp_path):
    db = _db(tmp_path)
    _fair(db)
    with db._conn() as c:
        c.execute("UPDATE market_state SET observed_at = 'pas une date'")
    o = evaluer([_cote(4.00)], db, MAINTENANT).opportunites[0]
    assert o.age_fair_sec is None
    assert o.statut is Statut.REJET_FAIR_PERIMEE


def test_l_intervalle_de_maj_est_N_A_tant_qu_on_n_a_pas_deux_observations(tmp_path):
    """Entre deux passages qui relisent le MÊME `observed_at`, la source n'a
    rien dit. Renvoyer 0 laisserait croire à une mise à jour à l'instant."""
    db = _db(tmp_path)
    _fair(db, observed=MAINTENANT - timedelta(seconds=5))
    m = Memoire()
    o = evaluer([_cote(4.00)], db, MAINTENANT, memoire=m).opportunites[0]
    assert o.intervalle_maj_sec is None
    assert "maj_precedente=N/As" in o.ligne()

    o = evaluer([_cote(4.00)], db, MAINTENANT, memoire=m).opportunites[0]
    assert o.intervalle_maj_sec is None, "même observation, aucune mise à jour"

    _fair(db, observed=MAINTENANT - timedelta(seconds=2))
    m.vues.clear()
    o = evaluer([_cote(4.00)], db, MAINTENANT, memoire=m).opportunites[0]
    assert o.intervalle_maj_sec == pytest.approx(3.0)


def test_les_cinq_mesures_de_fraicheur_sont_toutes_portees(tmp_path):
    """Les cinq champs demandés existent et valent ce qu'ils annoncent."""
    db = _db(tmp_path)
    _fair(db, observed=MAINTENANT - timedelta(seconds=7))
    o = evaluer([_cote(4.00, fetched=MAINTENANT - timedelta(seconds=2))],
                db, MAINTENANT,
                preneur_pris_a=MAINTENANT - timedelta(seconds=1.5)).opportunites[0]
    assert o.age_fair_sec == pytest.approx(7.0)
    assert o.age_preneur_sec == pytest.approx(2.0)
    assert o.delai_calcul_sec == pytest.approx(1.5)
    assert o.intervalle_maj_sec is None
    assert o.detecte_a == MAINTENANT


# ══ 14 : aucune écriture ═══════════════════════════════════════════════
def test_le_moteur_n_ecrit_RIEN(tmp_path):
    """Contrainte explicite : lecture seule. On compare l'état AVANT et
    APRÈS, ligne par ligne, plutôt que de faire confiance au code lu."""
    db = _db(tmp_path)
    _fair(db)
    avant = [tuple(r) for r in db.market_state()]
    with db._conn() as c:
        quotes_avant = c.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        bets_avant = c.execute("SELECT COUNT(*) FROM value_bets").fetchone()[0]

    m = Memoire()
    for _ in range(3):
        evaluer([_cote(4.00), _cote(2.00, label="away")], db, MAINTENANT,
                memoire=m)

    assert [tuple(r) for r in db.market_state()] == avant
    with db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == quotes_avant
        assert c.execute("SELECT COUNT(*) FROM value_bets").fetchone()[0] == bets_avant


def test_le_moteur_n_importe_pas_telegram():
    """Aucune alerte à cette étape, et pas seulement « on n'appelle pas » :
    le module d'alerte n'entre même pas dans le graphe d'imports."""
    import ast
    import pathlib
    src = pathlib.Path("src/live_value.py").read_text()
    importes = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.ImportFrom):
            importes.add(n.module or "")
        elif isinstance(n, ast.Import):
            importes.update(a.name for a in n.names)
    interdits = {"alerter", "main", "orchestration", "telegram"}
    assert not {i.split(".")[-1] for i in importes} & interdits, importes


# ══ affichage ══════════════════════════════════════════════════════════
def test_la_ligne_d_observation_porte_tous_les_champs_demandes(tmp_path):
    db = _db(tmp_path)
    _fair(db)
    o = evaluer([_cote(4.00)], db, MAINTENANT).opportunites[0]
    ligne = o.ligne()
    for champ in ("match=", "market=", "outcome=", "unibet=", "fair=", "ev=",
                  "asianodds_age=", "unibet_age=", "score=", "status="):
        assert champ in ligne, f"{champ} absent de : {ligne}"


def test_le_resume_publie_les_rejets_et_pas_seulement_les_prises(tmp_path):
    """Ce que la prudence coûte doit rester visible. Un compte rendu qui
    n'affiche que les prises laisserait croire que le moteur ne trouve rien,
    alors qu'il trouve et refuse."""
    db = _db(tmp_path)
    _fair(db, observed=MAINTENANT - timedelta(seconds=600))
    _fair(db, cle=event_key("A", "B", DEBUT), cotes=(1.05, 1.05, 1.05))
    r = resume(evaluer([_cote(4.00)], db, MAINTENANT))
    assert "fair périmée            1" in r
    assert "overround invalide ×1" in r


def test_une_jambe_sans_prix_reel_rend_le_groupe_incomplet(tmp_path):
    """Une cote à 1.00 est une LIGNE présente et une ISSUE absente.

    Compter les lignes lues plutôt que les prix retenus donnerait un groupe
    « complet » de trois lignes dont le devig n'en verrait que deux, et il
    normaliserait ces deux-là à 100 % — un 1X2 devigué comme un pile ou face.
    """
    db = _db(tmp_path)
    _fair(db, cotes=(3.00, 1.00, 2.60))
    groupes, motifs = construire_fair(db, MAINTENANT)
    assert groupes == {}
    assert motifs["groupe incomplet"] == 1


# ══ bout en bout : du payload Kambi a l'occasion ═══════════════════════
#
# Les tests ci-dessus appellent `evaluer` avec des cotes fabriquees a la main.
# Utile, mais ils ne prouvent RIEN sur le chemin reel : entre le payload et le
# moteur il y a `parse_listview`, `candidats_en_cours`, `evaluer_appariement`
# et `_permuter_h2h`. C'est la que l'orientation se joue, et c'est donc la
# qu'il faut la verifier.

KO = DEBUT
_PAYLOAD_KEYS = dict(start=KO.strftime("%Y-%m-%dT%H:%M:%SZ"), group="Sweden")


def _payload(home, away, *, sid=1001, h=2500, d=3200, a=2800,
             totals=(2500, 1900, 1950)):
    """Le format que `parse_listview` lit VRAIMENT : cotes et lignes au
    millieme, `betOfferType.id` 2 pour le 1X2 et 6 pour les totaux, 11 pour
    le handicap — qui doit disparaitre en chemin."""
    ligne, over, under = totals
    return {"events": [{
        "event": {"id": sid, "homeName": home, "awayName": away,
                  **_PAYLOAD_KEYS},
        "betOffers": [
            {"betOfferType": {"id": 2},
             "outcomes": [{"type": "OT_ONE", "odds": h},
                          {"type": "OT_CROSS", "odds": d},
                          {"type": "OT_TWO", "odds": a}]},
            {"betOfferType": {"id": 6},
             "outcomes": [{"type": "OT_OVER", "odds": over, "line": ligne},
                          {"type": "OT_UNDER", "odds": under, "line": ligne}]},
            {"betOfferType": {"id": 11},
             "outcomes": [{"type": "OT_ONE", "odds": 9000, "line": -1000},
                          {"type": "OT_TWO", "odds": 9000, "line": -1000}]},
        ]}]}


class _Faux:
    def __init__(self, payload):
        self._p = payload

    def fetch_listview(self, sport="soccer", path_suffix=""):
        return self._p

    def close(self):
        pass


def _chaine(tmp_path, payload, *, fair_cotes=(1.20, 6.00, 15.00), **kw):
    """La chaine complete : sondage → appariement → moteur."""
    from src.unibet_live import UnibetLive, apparier
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = _db(tmp_path)
    db.upsert_events([(CLE, "soccer", "Superettan", "orebrosk",
                       "varbergsbois", DEBUT.isoformat())])
    _fair(db, cotes=fair_cotes)
    _fair(db, market=MarketType.TOTALS, cotes=(1.95, 1.85),
          labels=("over", "under"), line=2.5)
    live = UnibetLive("soccer", scraper=_Faux(payload),
                      horloge=lambda: MAINTENANT)
    live.sonder()
    app = apparier(live.instantane, db, MAINTENANT)
    return app, evaluer(app.quotes, db, MAINTENANT,
                        preneur_pris_a=live.instantane.pris_a, **kw)


def test_bout_en_bout_le_payload_kambi_produit_une_occasion(tmp_path):
    """Unibet cote le domicile a 2.50 la ou la juste AsianOdds le donne
    grandissime favori : c'est une value, et elle doit traverser toute la
    chaine sans se perdre."""
    app, a = _chaine(tmp_path, _payload("Örebro SK", "Varbergs BoIS"))
    assert app.matchs_apparies == 1
    assert a.matchs_analyses == 1
    home = [o for o in a.opportunites if o.outcome == "home"]
    assert home and home[0].ev_pct > SEUIL_EV_PCT
    assert home[0].cote_preneur == pytest.approx(2.50)


def test_bout_en_bout_le_handicap_du_payload_ne_ressort_jamais(tmp_path):
    """Le payload contient un handicap a 9.00 — irresistible et interdit.
    Deux filtres devraient l'arreter ; le test dit que le resultat est bon,
    pas lequel des deux a servi."""
    _, a = _chaine(tmp_path, _payload("Örebro SK", "Varbergs BoIS"))
    assert all(o.market is not MarketType.HANDICAP for o in a.opportunites)
    assert all(o.market in (MarketType.H2H, MarketType.TOTALS)
               for o in a.opportunites)


def test_bout_en_bout_l_orientation_DEPLACE_la_value_de_home_vers_away(tmp_path):
    """LE test qui compte pour l'orientation, et le seul qui discrimine.

    Ma premiere version ne prouvait rien : avec une juste ou le domicile ecrase
    l'exterieur, la value tombe sur `home` DANS LES DEUX SENS, et le test
    passait aussi bien avec la permutation que sans. Il fallait la construire
    pour que le verdict CHANGE.

    Memes cotes Unibet des deux cotes — 6.00 sur le premier nomme, 2.00 sur le
    second — et une juste equilibree (2.00 / 3.60 / 4.00). La value est sur la
    cote de 6.00, donc sur l'equipe PREMIERE NOMMEE PAR UNIBET, quelle qu'elle
    soit. Quand Unibet annonce le match a l'envers, cette equipe est notre
    EXTERIEUR : la value doit passer de `home` a `away`.

    Sans la permutation d'`apparier`, la value resterait sur `home` dans les
    deux cas — c'est-a-dire qu'on jugerait la cote de l'exterieur contre la
    probabilite du domicile. Sur un match desequilibre, ce defaut ne produit
    pas une petite erreur : il produit une value enorme et entierement fausse.
    """
    JUSTE = (2.00, 3.60, 4.00)
    COTES = dict(h=6000, d=3000, a=2000)

    app, endroit = _chaine(tmp_path / "a",
                           _payload("Örebro SK", "Varbergs BoIS", **COTES),
                           fair_cotes=JUSTE)
    assert app.inversions == 0
    app, envers = _chaine(tmp_path / "b",
                          _payload("Varbergs BoIS", "Örebro SK", **COTES),
                          fair_cotes=JUSTE)
    assert app.matchs_apparies == 1 and app.inversions == 1

    h2h = lambda a: {o.outcome for o in a.opportunites
                     if o.market is MarketType.H2H}
    assert h2h(endroit) == {"home"}, "la cote de 6.00 est celle du domicile"
    assert h2h(envers) == {"away"}, (
        "match annonce a l'envers : la cote de 6.00 est celle de l'exterieur")

    # Et c'est bien LA MEME cote qui est jugee, des deux cotes.
    prise = lambda a: next(o.cote_preneur for o in a.opportunites
                           if o.market is MarketType.H2H)
    assert prise(endroit) == prise(envers) == pytest.approx(6.00)


def test_bout_en_bout_les_totaux_survivent_a_l_inversion(tmp_path):
    """« Plus de 2,5 buts » ne depend pas de qui recoit : la meme cote doit
    donner la meme EV dans les deux sens."""
    _, endroit = _chaine(tmp_path, _payload("Örebro SK", "Varbergs BoIS"),
                         seuil_ev=-100.0)
    _, envers = _chaine(tmp_path, _payload("Varbergs BoIS", "Örebro SK"),
                        seuil_ev=-100.0)
    tot = lambda a: {(o.outcome, o.line): round(o.ev_pct, 9)
                     for o in a.opportunites if o.market is MarketType.TOTALS}
    assert tot(endroit) == tot(envers) != {}


def test_bout_en_bout_le_moteur_n_ecrit_toujours_rien(tmp_path):
    """La chaine complete, y compris `apparier` qui lit `events`."""
    from src.unibet_live import UnibetLive, apparier
    db = _db(tmp_path)
    db.upsert_events([(CLE, "soccer", "Superettan", "orebrosk",
                       "varbergsbois", DEBUT.isoformat())])
    _fair(db)
    avant = [tuple(r) for r in db.market_state()]
    live = UnibetLive("soccer",
                      scraper=_Faux(_payload("Örebro SK", "Varbergs BoIS")),
                      horloge=lambda: MAINTENANT)
    live.sonder()
    app = apparier(live.instantane, db, MAINTENANT)
    evaluer(app.quotes, db, MAINTENANT)
    assert [tuple(r) for r in db.market_state()] == avant


# ══ horloges ═══════════════════════════════════════════════════════════
def test_un_age_negatif_franc_est_traite_comme_INCONNU(tmp_path):
    """Un prix ne peut pas venir du futur. S'il en vient, les deux horloges
    divergent — et alors son âge réel est inconnu, pas nul. Le laisser passer
    ferait franchir le contrôle de fraîcheur à n'importe quelle cote, aussi
    vieille soit-elle, du moment que l'horloge de la source avance."""
    db = _db(tmp_path)
    _fair(db, observed=MAINTENANT + timedelta(seconds=30))
    o = evaluer([_cote(4.00)], db, MAINTENANT).opportunites[0]
    assert o.age_fair_sec is None
    assert o.statut is Statut.REJET_FAIR_PERIMEE


def test_le_jitter_sous_la_seconde_vaut_zero_et_pas_moins(tmp_path):
    """`parse_listview` estampille quelques microsecondes après l'instant de
    référence : c'est de l'ordonnancement, pas une anomalie. Afficher
    « -0.0 s » ferait douter d'une mesure juste."""
    db = _db(tmp_path)
    _fair(db)
    o = evaluer([_cote(4.00, fetched=MAINTENANT + timedelta(microseconds=800))],
                db, MAINTENANT).opportunites[0]
    assert o.age_preneur_sec == 0.0
    assert "unibet_age=0.0s" in o.ligne()


# ══ pas de plafond d'EV, jamais ════════════════════════════════════════
@pytest.mark.parametrize("ev_cible", [10.1, 25.0, 100.0, 500.0, 5000.0])
def test_aucune_EV_n_est_rejetee_pour_sa_TAILLE(tmp_path, ev_cible):
    """Règle explicite de la phase d'observation : +10 %, +100 %, +500 % ou
    davantage passent tous, si le calcul est valide. Une grosse EV n'est pas
    un défaut à corriger — c'est précisément l'objet de l'observation."""
    db = _db(tmp_path)
    _fair(db)
    o = evaluer([_cote(_cote_pour_ev(ev_cible, db))], db, MAINTENANT).opportunites[0]
    assert o.ev_pct == pytest.approx(ev_cible, rel=1e-9)
    assert o.statut is not Statut.DOUBLON
    assert "PLAFOND" not in o.motif.upper()


def test_kelly_est_calcule_et_ne_filtre_JAMAIS(tmp_path):
    """Kelly informe et trie. Il ne supprime pas.

    Le cas mesuré le 26/08 : une cote à 101 rendait +119 % d'EV pour un
    Kelly de 1,2 %, une cote à 3,25 rendait +15 % pour 6,8 %. Le classement
    par EV est l'inverse du classement par information. Les deux doivent
    sortir, avec les deux chiffres.
    """
    db = _db(tmp_path)
    _fair(db, cotes=(1.01, 21.41, 24.33))     # domicile écrasant, 2:0
    o = evaluer([_cote(101.00, label="away")], db, MAINTENANT).opportunites[0]
    assert o.ev_pct > 100.0, "EV énorme"
    assert 0.0 < o.kelly_pct < 3.0, "Kelly minuscule sur une cote à 101"
    assert o.statut is not Statut.DOUBLON, "le Kelly faible n'a rien supprimé"
    assert f"kelly={o.kelly_pct:.2f}%" in o.ligne()


# ══ marché preneur partiel ═════════════════════════════════════════════
def test_une_jambe_preneuse_MANQUANTE_ne_rejette_pas_l_occasion(tmp_path):
    """LE cas Petrojet du 26/08 : Unibet ne cotait que `draw` et `home`, sa
    jambe favorite absente, marge 0,032. L'EV de `draw` ne dépend pourtant
    que de la cote de `draw` et de sa probabilité juste — les autres jambes
    n'entrent pas dans le calcul. On signale, on ne rejette pas, et on
    n'invente aucune cote."""
    db = _db(tmp_path)
    _fair(db, cotes=(17.82, 6.26, 1.18))       # away écrasant
    lot = [_cote(41.00, label="draw"), _cote(131.00, label="home")]
    a = evaluer(lot, db, MAINTENANT)
    assert len(a.opportunites) == 2, "les deux cotes présentes sont jugées"
    for o in a.opportunites:
        assert o.partiel is True
        assert o.issues_manquantes == ("away",)
        assert o.statut is not Statut.REJET_MARCHE_PRENEUR
        assert "marché partiel" in o.ligne()
    assert a.partiels == 2


def test_une_cote_manquante_n_est_JAMAIS_inventee(tmp_path):
    """Aucune opportunité ne peut naître d'une issue que le preneur ne cote
    pas. L'absence reste l'absence."""
    db = _db(tmp_path)
    _fair(db)
    a = evaluer([_cote(4.00, label="home")], db, MAINTENANT)
    assert {o.outcome for o in a.opportunites} == {"home"}
    assert a.opportunites[0].issues_manquantes == ("away", "draw")


def test_un_marche_preneur_COMPLET_n_est_pas_marque_partiel(tmp_path):
    """Contre-épreuve : sans elle, marquer tout le monde « partiel » ferait
    passer le test précédent."""
    db = _db(tmp_path)
    _fair(db)
    a = evaluer([_cote(4.00, label="home"), _cote(4.00, label="draw"),
                 _cote(4.00, label="away")], db, MAINTENANT)
    assert a.partiels == 0
    assert all(not o.partiel and o.issues_manquantes == ()
               for o in a.opportunites)


# ══ overround du preneur ═══════════════════════════════════════════════
def test_une_marge_preneuse_BASSE_n_est_jamais_un_motif_de_rejet(tmp_path):
    """Une marge sous 100 % sur un marché complet est la signature d'un prix
    trop généreux — c'est-à-dire l'occasion elle-même. La couper reviendrait
    à supprimer les occasions pour cause d'occasion."""
    db = _db(tmp_path)
    _fair(db, cotes=(2.00, 3.60, 4.00))
    a = evaluer([_cote(1.90, label="home"), _cote(3.60, label="draw"),
                 _cote(8.00, label="away")], db, MAINTENANT)
    o = next(x for x in a.opportunites if x.outcome == "away")
    assert o.overround_preneur < 1.0, "marché sous 100 %"
    assert o.statut is not Statut.REJET_MARCHE_PRENEUR
    assert o.ev_pct > SEUIL_EV_PCT


def test_une_marge_preneuse_GROTESQUE_sur_marche_complet_est_rejetee(tmp_path):
    """Au-delà, ce n'est plus une offre. Le seuil est très haut à dessein :
    ce contrôle écarte ce qui n'est pas un marché, pas ce qui est cher."""
    db = _db(tmp_path)
    _fair(db, cotes=(2.00, 3.60, 4.00))
    a = evaluer([_cote(20.00, label="home"), _cote(1.10, label="draw"),
                 _cote(1.10, label="away")], db, MAINTENANT)
    o = next(x for x in a.opportunites if x.outcome == "home")
    assert o.overround_preneur > 1.50
    assert o.statut is Statut.REJET_MARCHE_PRENEUR


def test_un_marche_PARTIEL_n_est_jamais_juge_sur_sa_marge(tmp_path):
    """C'est LA distinction « marché incomplet » / « marché faux ».

    Ici deux jambes sur trois, chacune à 1.10 : la marge du lot vaut 1,82,
    au-dessus du seuil. Mais elle ne veut rien dire — il MANQUE une jambe, et
    la somme des probabilités d'un marché amputé n'a aucune raison d'être
    proche de 100 %. Juger un marché partiel sur sa marge, c'est rejeter une
    cote parfaitement valide au motif qu'une AUTRE cote est absente.

    Ma première version de ce test ne prouvait rien : elle n'avait qu'une
    seule jambe, la marge n'était donc même pas calculée, et le test passait
    aussi bien avec le garde-fou que sans.
    """
    db = _db(tmp_path)
    _fair(db, cotes=(1.20, 6.00, 15.00))
    a = evaluer([_cote(1.50, label="home"), _cote(1.15, label="draw")],
                db, MAINTENANT)
    o = next(x for x in a.opportunites if x.outcome == "home")
    assert o.partiel is True and o.issues_manquantes == ("away",)
    assert o.overround_preneur > OVERROUND_PRENEUR_MAX, "marge au-dessus du seuil"
    assert o.ev_pct > SEUIL_EV_PCT, "et une jambe réellement rentable"
    assert o.statut is not Statut.REJET_MARCHE_PRENEUR, (
        "un marché partiel a été jugé sur une marge qui ne veut rien dire")


# ══ match terminé ══════════════════════════════════════════════════════
def test_un_match_clairement_TERMINE_est_rejete(tmp_path):
    """Un match ne peut plus être en cours 3 heures après le coup d'envoi."""
    db = _db(tmp_path)
    vieux = event_key("Fini FC", "Termine SK", MAINTENANT - timedelta(hours=3))
    _fair(db, cle=vieux)
    o = evaluer([_cote(4.00, cle=vieux)], db, MAINTENANT).opportunites[0]
    assert o.statut is Statut.REJET_MATCH_TERMINE
    assert o.minute_ecoulee == pytest.approx(180, abs=1)


def test_un_match_a_la_88e_minute_N_EST_PAS_terminE(tmp_path):
    """LE test qui fige mon erreur du 26/08. J'avais déclaré Rapid Vienna
    terminé : coup d'envoi 16:45, observé à 18:25. J'avais OUBLIÉ LA
    MI-TEMPS — 16:45 + 45 + 15 + 45 = 18:30, le match était à la 88e.

    Une borne resserrée sur cette erreur aurait supprimé des matchs bel et
    bien en cours. 100 minutes d'horloge murale, c'est la fin de la seconde
    période, pas la fin du match.
    """
    db = _db(tmp_path)
    ko = MAINTENANT - timedelta(minutes=100)
    cle = event_key("Rapid Vienna", "Hearts", ko)
    _fair(db, cle=cle)
    o = evaluer([_cote(4.00, cle=cle)], db, MAINTENANT).opportunites[0]
    assert o.minute_ecoulee == pytest.approx(100, abs=1)
    assert o.statut is not Statut.REJET_MATCH_TERMINE


def test_une_prolongation_reste_en_cours(tmp_path):
    """90 + 15 de mi-temps + arrêts de jeu + 30 de prolongation et ses
    pauses : 140 minutes se jouent encore."""
    db = _db(tmp_path)
    ko = MAINTENANT - timedelta(minutes=140)
    cle = event_key("Prolong FC", "Tirs Au But SK", ko)
    _fair(db, cle=cle)
    o = evaluer([_cote(4.00, cle=cle)], db, MAINTENANT).opportunites[0]
    assert o.statut is not Statut.REJET_MATCH_TERMINE


# ══ déduplication : la règle en entier ═════════════════════════════════
def test_dedup_un_CHANGEMENT_DE_SCORE_rouvre_l_observation(tmp_path):
    """Même EV, même cote, mais un but est tombé : ce n'est plus la même
    situation de jeu. Sans cette règle, une occasion signalée à 0:0 resterait
    muette à 1:0 alors que tout a changé."""
    db = _db(tmp_path)
    _fair(db, feed_score="0:0")
    m = Memoire()
    q = _cote(4.00)
    assert evaluer([q], db, MAINTENANT, memoire=m).nouvelles
    assert evaluer([q], db, MAINTENANT, memoire=m).nouvelles == []

    _fair(db, feed_score="1:0")                       # but
    a = evaluer([q], db, MAINTENANT, memoire=m)
    assert a.nouvelles, "le changement de score n'a pas rouvert l'observation"
    assert "0:0 -> 1:0" in a.nouvelles[0].motif_reemission


def test_dedup_les_trois_declencheurs_et_RIEN_d_autre(tmp_path):
    """La règle complète, figée : EV qui bouge de 2 points, score qui change,
    ou disparition/réapparition. Un simple passage de plus ne suffit pas."""
    db = _db(tmp_path)
    _fair(db, feed_score="0:0")
    m = Memoire()
    base = _cote_pour_ev(20.0, db)

    assert evaluer([_cote(base)], db, MAINTENANT, memoire=m).nouvelles
    for _ in range(4):                                # rien ne change
        assert evaluer([_cote(base)], db, MAINTENANT, memoire=m).nouvelles == []
    # 1. EV
    a = evaluer([_cote(_cote_pour_ev(23.0, db))], db, MAINTENANT, memoire=m)
    assert a.nouvelles and "EV" in a.nouvelles[0].motif_reemission
    # 3. disparition puis retour
    evaluer([], db, MAINTENANT, memoire=m)
    assert evaluer([_cote(_cote_pour_ev(23.0, db))], db, MAINTENANT,
                   memoire=m).nouvelles
