"""L'observatoire LIVE : il enregistre, il ne décide pas.

Ce module ne doit RIEN changer au moteur. Ce qu'on vérifie ici : qu'il écrit
dans sa propre base, jamais dans celle de production ; qu'il mesure le prix
figé ; qu'il suit la cote après l'alerte ; et qu'il n'introduit aucun filtre.
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

import scripts.live_observatoire as obs
from src.asianodds_live import LiveRow
from src.matcher import event_key
from src.models import MarketType
from src.storage import Storage

NOW = datetime.now(timezone.utc)
KO = NOW - timedelta(minutes=40)
CLE = event_key("Avro", "Leek Town", KO)


class _Faux:
    def __init__(self, suite):
        self._suite = list(suite)

    def fetch_listview(self, sport="soccer", path_suffix=""):
        c = self._suite.pop(0) if len(self._suite) > 1 else self._suite[0]
        issues = [{"type": "OT_OVER", "odds": 1300, "line": 6500}]
        if c is not None:
            issues.append({"type": "OT_UNDER", "odds": c, "line": 6500})
        return {"events": [{
            "event": {"id": 1001, "homeName": "Avro", "awayName": "Leek Town",
                      "start": KO.strftime("%Y-%m-%dT%H:%M:%SZ"), "group": "T"},
            "betOffers": [{"betOfferType": {"id": 6}, "outcomes": issues}]}]}

    def close(self):
        pass


@pytest.fixture
def monde(tmp_path, monkeypatch):
    prod, jour = str(tmp_path / "prod.db"), str(tmp_path / "obs.db")
    db = Storage(prod)
    db.upsert_events([(CLE, "soccer", "T", "avro", "leektown", KO.isoformat())])

    def ecrire(score, cotes):
        q = datetime.now(timezone.utc)
        db.upsert_live_state([LiveRow(
            event_key=CLE, market=MarketType.TOTALS, outcome_label=l,
            line=6.5, odd=c, observed_at=q, home_score=2, away_score=3,
            feed_score=score, igm=76, league="T",
            source_event_id="1634601234", source_inverse=False,
            matched_at=q).as_upsert_row(q)
            for l, c in zip(("over", "under"), cotes)])

    ecrire("2:3", (2.10, 1.70))
    monkeypatch.setattr(obs, "SUIVI_SEC", 2.0)
    monkeypatch.setattr(obs, "BALAYAGE_SEC", 0.0)
    import src.unibet_live as ul
    vrai = ul.UnibetLive
    monkeypatch.setattr(
        obs, "UnibetLive",
        lambda sport="soccer", **kw: vrai(sport, scraper=_Faux([2800])))
    return prod, jour, db, ecrire


def _lancer(prod, jour, extra=()):
    sys.argv = ["obs", "--heures", "0.0015", "--periode", "1",
                "--db", prod, "--journal", jour, *extra]
    return obs.main()


# ══ la base de production n'est JAMAIS écrite ══════════════════════════
def test_l_observatoire_n_ecrit_RIEN_dans_la_base_de_production(monde):
    """La contrainte de toute la phase LIVE : le moteur ne persiste rien.
    Enregistrer les observations ne doit pas ouvrir une porte dérobée."""
    prod, jour, db, _ = monde
    avant = [tuple(r) for r in db.market_state()]
    with db._conn() as c:
        quotes = c.execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
        bets = c.execute("SELECT COUNT(*) FROM value_bets").fetchone()[0]
    assert _lancer(prod, jour) == 0
    assert [tuple(r) for r in db.market_state()] == avant
    with db._conn() as c:
        assert c.execute("SELECT COUNT(*) FROM quotes").fetchone()[0] == quotes
        assert c.execute("SELECT COUNT(*) FROM value_bets").fetchone()[0] == bets


def test_le_journal_est_un_fichier_DISTINCT(monde):
    prod, jour, _, _ = monde
    _lancer(prod, jour)
    assert jour != prod
    cx = sqlite3.connect(jour)
    assert cx.execute("SELECT COUNT(*) FROM alertes").fetchone()[0] > 0


# ══ le prix figé ═══════════════════════════════════════════════════════
def test_un_score_qui_change_SANS_que_le_prix_bouge_est_compte(monde):
    """LE point de cette session. Le cas Avro — Leek Town du 26/08 : score
    2:3 → 2:4, cote inchangée, `observed_at` pourtant rafraîchi. Si ça se
    répète, notre contrôle de fraîcheur regarde la mauvaise chose."""
    prod, jour, db, ecrire = monde
    vrai = obs.evaluer
    n = {"i": 0}

    def espion(*args, **kw):
        n["i"] += 1
        if n["i"] == 2:
            ecrire("2:4", (2.10, 1.70))      # score neuf, PRIX IDENTIQUE
        return vrai(*args, **kw)

    obs.evaluer = espion
    try:
        _lancer(prod, jour)
    finally:
        obs.evaluer = vrai
    cx = sqlite3.connect(jour)
    cx.row_factory = sqlite3.Row
    lignes = cx.execute("SELECT * FROM score_prix").fetchall()
    assert lignes, "aucun changement de score enregistré"
    assert all(r["prix_fige"] == 1 for r in lignes)
    assert {r["score_avant"] for r in lignes} == {"2:3"}
    assert {r["score_apres"] for r in lignes} == {"2:4"}


def test_un_score_qui_change_AVEC_le_prix_n_est_pas_compte_comme_fige(monde):
    """Contre-épreuve : sans elle, un compteur bloqué à « toujours figé »
    passerait le test précédent."""
    prod, jour, db, ecrire = monde
    vrai = obs.evaluer
    n = {"i": 0}

    def espion(*args, **kw):
        n["i"] += 1
        if n["i"] == 2:
            ecrire("2:4", (3.40, 1.28))      # score neuf ET prix neuf
        return vrai(*args, **kw)

    obs.evaluer = espion
    try:
        _lancer(prod, jour)
    finally:
        obs.evaluer = vrai
    cx = sqlite3.connect(jour)
    lignes = cx.execute("SELECT prix_fige FROM score_prix").fetchall()
    assert lignes and all(r[0] == 0 for r in lignes)


# ══ le suivi après l'alerte ════════════════════════════════════════════
def test_la_cote_est_suivie_et_sa_DISPARITION_enregistree(monde, monkeypatch):
    """Une cote retirée juste après l'alerte est le signal le plus net que
    le book s'est repris. Elle doit être enregistrée comme NULL, pas
    disparaître du journal."""
    prod, jour, _, _ = monde
    import src.unibet_live as ul
    vrai = ul.UnibetLive
    monkeypatch.setattr(
        obs, "UnibetLive",
        lambda sport="soccer", **kw: vrai(
            sport, scraper=_Faux([2800, 1900, None])))
    _lancer(prod, jour)
    cx = sqlite3.connect(jour)
    cx.row_factory = sqlite3.Row
    suite = cx.execute("SELECT * FROM suivi WHERE alerte_id = 1"
                       " ORDER BY t_sec").fetchall()
    assert suite, "aucun suivi enregistré"
    assert suite[0]["t_sec"] == 0.0
    assert suite[0]["cote_unibet"] == pytest.approx(2.80), "le point de départ"

    # LA TRAJECTOIRE, pas seulement les deux bouts. Ma première version ne
    # vérifiait que le premier et le dernier point : désactiver
    # l'enregistrement des changements intermédiaires la laissait passer,
    # parce que l'expiration écrit de toute façon un point final. Or c'est
    # l'évolution qui dit si le book s'est repris — un endpoint ne le dit pas.
    valeurs = [r["cote_unibet"] for r in suite]
    assert pytest.approx(1.90) in [v for v in valeurs if v is not None], (
        f"la baisse intermédiaire n'a pas été enregistrée : {valeurs}")
    assert None in valeurs, "la disparition de la cote n'a pas été enregistrée"
    assert valeurs.index(None) > valeurs.index(pytest.approx(1.90)), (
        "l'ordre chronologique est perdu")


# ══ aucun filtre ajouté ════════════════════════════════════════════════
def test_une_EV_ENORME_est_enregistree_telle_quelle(monde, monkeypatch):
    """L'observatoire enregistre ce que le moteur lui donne, sans plafond.

    Ma première version de ce test lisait le source à la recherche de
    comparaisons sur `ev_pct` — fragile, et ça ne prouvait rien : un filtre
    peut s'écrire de cent façons. Vérifier le COMPORTEMENT est plus court et
    plus fort.
    """
    prod, jour, db, _ = monde
    import src.unibet_live as ul
    vrai = ul.UnibetLive
    # under 6.5 à 41.00 contre une fair AsianOdds de 1.70 : ~ +2300 % d'EV.
    monkeypatch.setattr(
        obs, "UnibetLive",
        lambda sport="soccer", **kw: vrai(sport, scraper=_Faux([41000])))
    _lancer(prod, jour)
    cx = sqlite3.connect(jour)
    haut = cx.execute("SELECT MAX(ev_pct) FROM alertes").fetchone()[0]
    assert haut is not None and haut > 1000.0, (
        f"une EV énorme a été perdue en route (max enregistré : {haut})")


def test_l_observatoire_n_importe_pas_le_moteur_PREMATCH():
    """Isolation : rien du prématch ne doit entrer dans ce chemin."""
    import ast
    import pathlib
    arbre = ast.parse(pathlib.Path("scripts/live_observatoire.py").read_text())
    modules = {n.module or "" for n in ast.walk(arbre)
               if isinstance(n, ast.ImportFrom)}
    interdits = {"src.detection", "src.main", "src.late_markets",
                 "src.orchestration"}
    assert not (modules & interdits), modules & interdits
