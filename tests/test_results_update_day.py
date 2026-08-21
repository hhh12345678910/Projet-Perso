"""`results-update --day` — mesurer une source sans se mentir.

Le 21/08, un dry-run a rendu 32 % et ce chiffre ne mesurait PAS la couverture
de la source : la fenêtre de `--days` va jusqu'à `maintenant - 2 h`, donc elle
contient toujours la journée en cours, dont le pont n'a pas encore déposé le
fichier. Deux journées manquaient, et le taux mélangeait « la source n'a pas ce
match » avec « ce jour n'a jamais été demandé ».

Le compteur `journee_non_pontee` ne pouvait donc jamais retomber à zéro, ce qui
rendait la sonde inutilisable pour ce à quoi elle sert : décider de payer un
abonnement. `--day` borne sur une journée UTC révolue, et le taux qui en sort
ne parle plus que de la source.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from typer.testing import CliRunner

from src.main import app
from src.models import Book, MarketType, Outcome, ValueBet
from src.storage import Storage

runner = CliRunner()


def _db_path(tmp_path):
    """`ScanConfig.db_path` est un chemin RELATIF au répertoire courant, et
    n'est pas réglable par l'environnement. On place donc la base là où la
    commande ira la chercher, et `_run` se déplace dedans."""
    (tmp_path / "data").mkdir(exist_ok=True)
    return str(tmp_path / "data" / "valuebet.db")


def _base(tmp_path, jours):
    """Une détection football par journée demandée, à midi UTC."""
    db = _db_path(tmp_path)
    st = Storage(db)
    for i, d in enumerate(jours):
        ek = f"ev{i}::a__vs__b"
        st.upsert_event(ek, "soccer", "Test League", "A", "B",
                        datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc))
        st.insert_value_bet(ValueBet(
            event_key=ek, book=Book.UNIBET_BE, market=MarketType.H2H,
            outcome=Outcome(label="home"), odd_taken=2.0, fair_prob=0.5,
            fair_odd=1.9, ev_pct=10.0, kelly_stake_pct=1.0,
            detected_at=datetime(d.year, d.month, d.day, 9, tzinfo=timezone.utc)))
    return db


def _run(monkeypatch, tmp_path, db, *args):
    # Le pont, et un répertoire de scores VIDE : on veut que la source soit
    # réclamée et manquante, c'est justement ce que compte
    # `journee_non_pontee`. Aucun appel réseau n'est fait.
    monkeypatch.setenv("SCORES_FOOTBALL_BRIDGE", "1")
    monkeypatch.setenv("SCORES_INGEST_DIR", str(tmp_path / "scores"))
    monkeypatch.chdir(tmp_path)
    return runner.invoke(app, ["results-update", "--dry-run", "--sport", "soccer", *args])


@pytest.fixture(autouse=True)
def _registre_vierge():
    """⚠️ `teams._DISPLAY` est un cache GLOBAL au processus, et `init()` ne le
    vide pas — vérifié. Sans ce nettoyage, un test hérite des noms enregistrés
    par le précédent et passe pour la mauvaise raison."""
    import src.teams as teams
    teams._DISPLAY.clear()
    yield
    teams._DISPLAY.clear()


@pytest.fixture()
def jours():
    aujourdhui = datetime.now(timezone.utc).date()
    return {"hier": aujourdhui - timedelta(days=1),
            "avant_hier": aujourdhui - timedelta(days=2),
            "aujourdhui": aujourdhui}


def test_day_ne_juge_que_la_journee_demandee(monkeypatch, tmp_path, jours):
    """Trois journées en base, une seule demandée : la sonde ne doit en voir
    qu'une. Sinon le taux porte sur des jours qu'on n'a pas voulu mesurer."""
    db = _base(tmp_path, [jours["avant_hier"], jours["hier"], jours["aujourdhui"]])
    r = _run(monkeypatch, tmp_path, db, "--day", jours["hier"].isoformat())

    assert r.exit_code == 0, r.output
    # Une seule journée pontée manquante, donc un seul match à noter.
    assert "journee_non_pontee=1" in r.output
    assert f"journée {jours['hier'].isoformat()}" in r.output


def test_sans_day_la_fenetre_avale_la_journee_en_cours(monkeypatch, tmp_path, jours):
    """Le défaut d'origine, gardé sous test pour qu'on sache qu'il est
    STRUCTUREL et non un accident : `--days` inclut toujours aujourd'hui."""
    db = _base(tmp_path, [jours["hier"], jours["aujourdhui"]])
    r = _run(monkeypatch, tmp_path, db, "--days", "2")

    assert r.exit_code == 0, r.output
    # Deux journées réclamées, aucune pontée : le compteur le dit.
    assert "journee_non_pontee=2" in r.output


def test_une_journee_non_revolue_est_refusee(monkeypatch, tmp_path, jours):
    """Demander demain ne peut rien rendre. Mieux vaut le dire que d'afficher
    un 0 % qui se lirait comme « la source ne couvre rien »."""
    db = _base(tmp_path, [jours["hier"]])
    demain = (jours["aujourdhui"] + timedelta(days=1)).isoformat()
    r = _run(monkeypatch, tmp_path, db, "--day", demain)

    assert r.exit_code != 0
    assert "révolue" in r.output


def test_une_date_illisible_est_refusee_clairement(monkeypatch, tmp_path, jours):
    """`--day hier` doit dire ce qu'il attend, pas lever une ValueError nue."""
    db = _base(tmp_path, [jours["hier"]])
    r = _run(monkeypatch, tmp_path, db, "--day", "hier")

    assert r.exit_code != 0
    assert "AAAA-MM-JJ" in r.output


def test_day_couvre_toute_la_journee_utc(monkeypatch, tmp_path, jours):
    """Un match de 23 h UTC — l'Amérique du Sud, 7 % du flux — doit être dans
    la journée demandée. Rogner `until` de deux heures les perdrait tous."""
    db = _db_path(tmp_path)
    st = Storage(db)
    tard = datetime(jours["hier"].year, jours["hier"].month, jours["hier"].day,
                    23, 30, tzinfo=timezone.utc)
    st.upsert_event("tard::a__vs__b", "soccer", "Brazil - Serie C", "A", "B", tard)
    st.insert_value_bet(ValueBet(
        event_key="tard::a__vs__b", book=Book.UNIBET_BE, market=MarketType.H2H,
        outcome=Outcome(label="home"), odd_taken=2.0, fair_prob=0.5,
        fair_odd=1.9, ev_pct=10.0, kelly_stake_pct=1.0, detected_at=tard))

    r = _run(monkeypatch, tmp_path, db, "--day", jours["hier"].isoformat())
    assert r.exit_code == 0, r.output
    # Le match est bien pris en compte : la source est réclamée pour ce jour.
    assert "journee_non_pontee=1" in r.output
    assert "Aucun match en attente" not in r.output


def _fichier_pont(tmp_path, jour, matchs):
    """Un fichier de pont à la forme d'API-Football. `matchs` = [(dom, ext)]."""
    import json
    d = tmp_path / "scores" / "soccer"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{jour.isoformat()}.json").write_text(json.dumps({"response": [
        {"fixture": {"id": i, "date": f"{jour.isoformat()}T12:00:00+00:00",
                     "status": {"short": "FT"}},
         "teams": {"home": {"name": h}, "away": {"name": a}},
         "score": {"fulltime": {"home": 1, "away": 0}}}
        for i, (h, a) in enumerate(matchs)]}, ensure_ascii=False), encoding="utf-8")


def _base_deux_ligues(tmp_path, jour):
    """Deux ligues de deux matchs, aux noms d'équipe distincts."""
    db = _db_path(tmp_path)
    st = Storage(db)
    quand = datetime(jour.year, jour.month, jour.day, 12, tzinfo=timezone.utc)
    for ligue, equipes in (("Spain - La Liga", [("Alpha FC", "Beta FC"),
                                                ("Gamma FC", "Delta FC")]),
                           ("Club Friendlies", [("Epsilon FC", "Zeta FC"),
                                                ("Eta FC", "Theta FC")])):
        for h, a in equipes:
            ek = f"{h}::{a}".lower().replace(" ", "")
            st.upsert_event(ek, "soccer", ligue, h, a, quand)
            st.insert_value_bet(ValueBet(
                event_key=ek, book=Book.UNIBET_BE, market=MarketType.H2H,
                outcome=Outcome(label="home"), odd_taken=2.0, fair_prob=0.5,
                fair_odd=1.9, ev_pct=10.0, kelly_stake_pct=1.0, detected_at=quand))
    return db


def test_une_ligue_entierement_ratee_est_nommee(monkeypatch, tmp_path, jours):
    """LE diagnostic qui manquait. Un taux global de 50 % ne dit pas si la
    source rate un peu partout ou ignore une compétition entière — et seule la
    seconde situation biaise le P&L (§21.9)."""
    jour = jours["hier"]
    db = _base_deux_ligues(tmp_path, jour)
    # La source ne connaît que La Liga. Les amicaux sont absents du catalogue.
    _fichier_pont(tmp_path, jour, [("Alpha FC", "Beta FC"), ("Gamma FC", "Delta FC")])

    r = _run(monkeypatch, tmp_path, db, "--day", jour.isoformat())
    assert r.exit_code == 0, r.output
    assert "ne résout RIEN" in r.output
    assert "Club Friendlies" in r.output
    # La Liga est intégralement couverte : elle ne doit pas être signalée.
    assert "La Liga" not in r.output.split("ne résout RIEN")[1]
    # ⚠️ Et le message ne doit RIEN conclure : un zéro peut être un trou de
    # catalogue OU une convention de noms. La première version affirmait
    # « trou de catalogue » et se trompait sur le football féminin (§21.16).
    assert "Deux causes possibles" in r.output


def test_une_couverture_complete_ne_signale_rien(monkeypatch, tmp_path, jours):
    """Pas de faux positif : si tout est résolu, aucun tableau de manques."""
    jour = jours["hier"]
    db = _base_deux_ligues(tmp_path, jour)
    _fichier_pont(tmp_path, jour, [("Alpha FC", "Beta FC"), ("Gamma FC", "Delta FC"),
                                   ("Epsilon FC", "Zeta FC"), ("Eta FC", "Theta FC")])

    r = _run(monkeypatch, tmp_path, db, "--day", jour.isoformat())
    assert r.exit_code == 0, r.output
    assert "ne résout RIEN" not in r.output
    assert "où la source manque" not in r.output


def test_les_compteurs_dappariement_sont_affiches(monkeypatch, tmp_path, jours):
    """Le 21/08, `classe_posee` a été annoncé à l'utilisateur puis n'apparaissait
    nulle part : la colonne « Écartés source » ne montre que les compteurs de la
    SOURCE. Un compteur qu'on ne voit pas ne diagnostique rien — c'est ce qui a
    laissé croire une première fois qu'un correctif était appliqué."""
    jour = jours["hier"]
    db = _db_path(tmp_path)
    st = Storage(db)
    quand = datetime(jour.year, jour.month, jour.day, 12, tzinfo=timezone.utc)
    st.upsert_event("f::a__vs__b", "soccer", "USA - National Womens Soccer League",
                    "Houston Dash", "Chicago Red Stars", quand)
    st.insert_value_bet(ValueBet(
        event_key="f::a__vs__b", book=Book.UNIBET_BE, market=MarketType.H2H,
        outcome=Outcome(label="home"), odd_taken=2.0, fair_prob=0.5,
        fair_odd=1.9, ev_pct=10.0, kelly_stake_pct=1.0, detected_at=quand))
    _fichier_pont(tmp_path, jour, [("Houston Dash W", "Chicago Red Stars W")])

    r = _run(monkeypatch, tmp_path, db, "--day", jour.isoformat())
    assert r.exit_code == 0, r.output
    assert "classe_posee=1" in r.output
    assert "où la source manque" not in r.output


def test_le_nom_brut_est_repris_du_registre(monkeypatch, tmp_path, jours):
    """`events.home` contient la forme COMPACTÉE de la clé (« colonsantafe »),
    pas le nom brut — `build_event_rows` le dérive de `parse_event_key`.

    Mesuré le 21/08 : « colonsantafe » vs « Colón Res. » donne 80,0, sous le
    seuil de 85, donc match PERDU ; le nom brut « Colon Santa Fe » donne 87,5
    et passe. Le registre `teams` est clé sur la même forme compactée et rend
    le nom d'origine : on répare à la lecture.
    """
    jour = jours["hier"]
    db = _db_path(tmp_path)
    st = Storage(db)
    quand = datetime(jour.year, jour.month, jour.day, 12, tzinfo=timezone.utc)
    st.upsert_event("r::a__vs__b", "soccer", "Argentina - Liga Pro Reserves",
                    "platense", "colonsantafe", quand)
    st.insert_value_bet(ValueBet(
        event_key="r::a__vs__b", book=Book.UNIBET_BE, market=MarketType.H2H,
        outcome=Outcome(label="home"), odd_taken=2.0, fair_prob=0.5,
        fair_odd=1.9, ev_pct=10.0, kelly_stake_pct=1.0, detected_at=quand))
    # Ce qu'un scraper a réellement vu, enregistré comme en production.
    st.record_team("platense", "Platense")
    st.record_team("colonsantafe", "Colon Santa Fe")

    _fichier_pont(tmp_path, jour, [("Platense Res.", "Colón Res.")])
    r = _run(monkeypatch, tmp_path, db, "--day", jour.isoformat())

    assert r.exit_code == 0, r.output
    # ⚠️ Jamais `"100 %" in output` ni `"0 %" in output` : « 100 % » CONTIENT
    # « 0 % », et l'assertion inverse passe donc toujours. Le tableau des
    # manques, lui, n'apparaît que s'il manque quelque chose.
    assert "où la source manque" not in r.output, r.output


def test_le_compactage_erode_la_marge_sans_perdre_le_match():
    """⚠️ Ce que le registre `teams` fait, et ce qu'il NE fait PAS.

    Mesuré le 21/08 sur les paires réelles du 20/08 : le nom compacté coûte
    2 à 12 points de similarité, mais l'appariement décide sur la MOYENNE des
    deux côtés, et elle rattrape toujours un nom faible. Sur douze événements
    réels, **zéro** bascule sous le seuil de 85.

    Le registre est donc de la MARGE, pas un correctif — écrit ici pour qu'on
    ne le crédite jamais d'une récupération qu'il ne fait pas. La première
    version de ce test affirmait l'inverse et passait pour deux mauvaises
    raisons cumulées : `"0 %" in "100 %"` est vrai, et le cache global de
    `teams` fuyait d'un test à l'autre.
    """
    from src.matcher import team_similarity as sim

    def moyenne(nous, source):
        return (sim(nous[0], source[0]) + sim(nous[1], source[1])) / 2

    compacte = moyenne(("platense", "colonsantafe"), ("Platense Res.", "Colón Res."))
    brut = moyenne(("Platense", "Colon Santa Fe"), ("Platense Res.", "Colón Res."))

    assert compacte < brut, "le nom brut doit toujours être au moins aussi bon"
    assert compacte >= 85.0, "et pourtant le match n'est pas perdu — c'est le point"
