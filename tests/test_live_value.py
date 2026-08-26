"""Le moteur LIVE : ce qu'il détecte, et surtout ce qu'il REFUSE de détecter.

Chaque garde-fou a ici un test qui échoue si on l'ôte. C'est la seule façon
de savoir qu'il sert encore : un garde-fou sans test qui le falsifie est une
intention, pas une protection.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.live_value import (
    AGE_MAX_FAIR_SEC, AGE_MAX_PRENEUR_SEC, SEUIL_EV_PCT, Memoire, Statut,
    construire_fair, evaluer, resume)
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
