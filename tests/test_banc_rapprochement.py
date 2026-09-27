"""Le banc d'essai des règles de rapprochement assouplies.

Chaque cas construit une base et un fichier du pont, puis vérifie les trois
mesures : l'erreur à l'aveugle (le vrai match caché), l'autre choix (le vrai
match présent), et ce qu'une règle récupère sur les matchs sans résultat.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from scripts import banc_rapprochement as br

J = datetime(2026, 9, 20, tzinfo=timezone.utc)
Q = "2026-09-20T15:00:00+00:00"


def _fx(id_, dom, ext, quand=Q, hs=2, as_=1, statut="FT", ligue="L", pays=""):
    return {"fixture": {"id": id_, "date": quand, "status": {"short": statut}},
            "league": {"name": ligue, "country": pays},
            "teams": {"home": {"name": dom}, "away": {"name": ext}},
            "score": {"fulltime": {"home": hs, "away": as_},
                      "extratime": {"home": None, "away": None}},
            "goals": {"home": hs, "away": as_}}


def _monde(tmp_path, reglees=(), manquants=(), fixtures=(), joues=()):
    """`reglees` : (clé, dom, ext, ligue, score dom, score ext, gagnant,
    source) ; `manquants` : (clé, dom, ext, ligue[, heure])."""
    from src.storage import Storage
    db = tmp_path / "v.db"
    Storage(str(db))
    c = sqlite3.connect(str(db))

    def match(k, h, a, lg, quand=Q):
        c.execute("INSERT INTO events (event_key, sport, league, home, away, start_time)"
                  " VALUES (?,?,?,?,?,?)", (k, "soccer", lg, h, a, quand))
        c.execute("INSERT INTO value_bets (event_key, book, market, outcome_label,"
                  " odd_taken, fair_prob, fair_odd, ev_pct, kelly_pct, detected_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (k, "u", "h2h", "home", 2.1, .5, 2.0, 5, 1, Q))
    for k, h, a, lg, hs, as_, w, src in reglees:
        match(k, h, a, lg)
        c.execute("INSERT INTO results VALUES (?,?,?,?,?,?)",
                  (k, w, hs, as_, src, "2026-09-21T10:00:00+00:00"))
    for m in manquants:
        match(*m)
    for k in joues:
        c.execute("INSERT INTO played_bets (dedup_key, event_key, market, outcome_label,"
                  " odd_taken, stake, played_at) VALUES (?,?,?,?,?,?,?)",
                  (f"d-{k}", k, "h2h", "home", 2.1, 10, Q))
    c.commit()
    c.close()
    dossier = tmp_path / "soccer"
    dossier.mkdir()
    (dossier / "2026-09-20.json").write_text(json.dumps({"response": list(fixtures)}))
    return db, dossier


def _banc(tmp_path, **kw):
    db, dossier = _monde(tmp_path, **kw)
    reglees, noms, joues = br.charger_regles(str(db), J.date())
    manquants, noms2, jours = br.charger_manquants(
        str(db), J.date(), datetime(2026, 9, 27, tzinfo=timezone.utc))
    return br.analyser(reglees, manquants, {**noms2, **noms}, joues, dossier,
                       jours=jours)


def _codes(d) -> dict:
    return {k: len(v) for k, v in d.items() if v}


# ------------------------------------------------------------ à l'aveugle ---

def test_un_match_sans_sosie_ne_fait_aucune_erreur(tmp_path):
    b = _banc(tmp_path,
              reglees=[("k", "Arsenal", "Chelsea", "L", 2, 1, "home", "api-football")],
              fixtures=[_fx(1, "Arsenal", "Chelsea")])
    assert b.epreuves == 1 and b.hors_epreuve == 0
    assert _codes(b.erreurs) == {}
    assert [g.code for g in br.sures(b)] == [g.code for g in br.REGLES]


def test_un_sosie_au_meme_horaire_trahit_les_regles_trop_souples(tmp_path):
    """« Sporting Gijon v Racing Santander » caché : « Sporting Braga v Racing
    Ferrol », au même horaire, vaut 76,7. La production (85) et N80 le
    refusent ; N75 et N70 le prendraient — deux erreurs qu'elles écriraient."""
    b = _banc(tmp_path,
              reglees=[("k", "Sporting Gijon", "Racing Santander", "L", 2, 1, "home",
                        "api-football")],
              fixtures=[_fx(1, "Sporting Gijon", "Racing Santander"),
                        _fx(2, "Sporting Braga", "Racing Ferrol", hs=0, as_=0)])
    assert b.epreuves == 1
    assert _codes(b.erreurs) == {"N75": 1, "N70": 1}
    assert b.epreuves_regles == 1
    assert "N75" not in {g.code for g in br.sures(b)}
    assert "N80" in {g.code for g in br.sures(b)}


def test_la_production_elle_meme_est_mesuree(tmp_path):
    """« Deportivo Cuenca v Macara » caché : « Deportivo Quito v Macara » vaut
    90,7 — au-dessus du seuil de production. C'est ce qu'elle écrirait si le
    vrai match manquait à la source : le banc le compte."""
    b = _banc(tmp_path,
              reglees=[("k", "Deportivo Cuenca", "Macara", "L", 2, 1, "home",
                        "api-football")],
              fixtures=[_fx(1, "Deportivo Cuenca", "Macara"),
                        _fx(2, "Deportivo Quito", "Macara", hs=0, as_=3)])
    assert b.epreuves == 1
    assert _codes(b.erreurs) == {"production": 1}
    assert b.erreurs["production"][0]["source"] == "Deportivo Quito - Macara"


def test_les_regles_ne_sont_pas_eprouvees_ou_la_production_a_deja_pris(tmp_path):
    """Là où la production prend un sosie, une règle assouplie — qui ne
    tourne qu'après elle — n'est jamais consultée : cette épreuve ne compte
    pas dans son dénominateur."""
    b = _banc(tmp_path,
              reglees=[("k", "Deportivo Cuenca", "Macara", "L", 2, 1, "home",
                        "api-football"),
                       ("k2", "Arsenal", "Chelsea", "L", 2, 1, "home", "api-football")],
              fixtures=[_fx(1, "Deportivo Cuenca", "Macara"),
                        _fx(2, "Deportivo Quito", "Macara", hs=0, as_=3),
                        _fx(3, "Arsenal", "Chelsea")])
    assert (b.epreuves, b.epreuves_regles) == (2, 1)


def test_un_match_sous_deux_cles_n_est_eprouve_qu_une_fois(tmp_path):
    """Horaire révisé : deux clés, un seul match de la source. Deux épreuves
    sur les mêmes candidats ne sont pas indépendantes."""
    b = _banc(tmp_path,
              reglees=[("k", "Arsenal", "Chelsea", "L", 2, 1, "home", "api-football"),
                       ("k2", "Arsenal FC", "Chelsea", "L", 2, 1, "home",
                        "api-football")],
              fixtures=[_fx(1, "Arsenal", "Chelsea")])
    assert (b.epreuves, b.cles_en_double) == (1, 1)


def test_un_resultat_saisi_n_entre_pas_dans_l_epreuve(tmp_path):
    b = _banc(tmp_path,
              reglees=[("k", "Arsenal", "Chelsea", "L", 2, 1, "home", "manual")],
              fixtures=[_fx(1, "Arsenal", "Chelsea")])
    assert b.epreuves == 0 and b.hors_epreuve == 1


def test_un_resultat_introuvable_n_entre_pas_dans_l_epreuve(tmp_path):
    b = _banc(tmp_path,
              reglees=[("k", "Arsenal", "Chelsea", "L", 2, 1, "home", "api-football")],
              fixtures=[_fx(1, "Kontu", "LPS")])
    assert b.epreuves == 0 and b.hors_epreuve == 1


def test_un_doublon_exact_n_est_pas_un_sosie(tmp_path):
    """Le même match servi deux fois, terminé les deux fois : nous à 15 h, le
    vrai à 15 h 08, son doublon à 15 h 15 (hors de NOTRE créneau, dans celui
    du vrai). Caché avec le vrai : sans `_doublons`, les règles H le
    comptaient comme une erreur (revue du 27/09 : ce test ne prouvait rien
    avec un doublon non terminé)."""
    b = _banc(tmp_path,
              reglees=[("k", "Arsenal", "Chelsea", "L", 2, 1, "home", "api-football")],
              fixtures=[_fx(1, "Arsenal", "Chelsea", quand="2026-09-20T15:08:00+00:00"),
                        _fx(2, "Arsenal", "Chelsea", quand="2026-09-20T15:15:00+00:00")])
    assert b.epreuves == 1 and _codes(b.erreurs) == {}


def test_la_production_qui_prend_toujours_un_sosie_ne_fait_pas_planter(tmp_path, capsys):
    """Une seule épreuve, et la production y prend le sosie : p = 1."""
    b = _banc(tmp_path,
              reglees=[("k", "Deportivo Cuenca", "Macara", "L", 2, 1, "home",
                        "api-football")],
              fixtures=[_fx(1, "Deportivo Cuenca", "Macara"),
                        _fx(2, "Deportivo Quito", "Macara", hs=0, as_=3)])
    br.imprimer(b, J.date())
    assert "tous les pour au plus" in capsys.readouterr().out


# ------------------------------------------------------------- récupère ---

def test_des_abreviations_se_recuperent_par_les_noms(tmp_path):
    """« Atletico Tucuman v Deportivo Maipu » contre « Atl. Tucuman v Dep.
    Maipu » : 82,1, sous le seuil de production — N80 le règle."""
    b = _banc(tmp_path,
              manquants=[("m", "Atletico Tucuman", "Deportivo Maipu", "L")],
              fixtures=[_fx(1, "Atl. Tucuman", "Dep. Maipu", hs=1, as_=0)],
              joues=["m"])
    assert b.manquants == 1 and b.par_production == 0
    assert _codes(b.recup) == {"N80": 1, "N75": 1, "N70": 1}
    ex, joues = b.recup["N80"][0]
    assert ex["source"] == "Atl. Tucuman - Dep. Maipu" and joues == 1


def test_le_pays_garde_les_regles_p(tmp_path):
    for pays, attendu in (("Argentina", True), ("Chile", False)):
        d = tmp_path / pays
        d.mkdir()
        b = _banc(d,
                  manquants=[("m", "Atletico Tucuman", "Deportivo Maipu",
                              "Argentina - Primera Nacional")],
                  fixtures=[_fx(1, "Atl. Tucuman", "Dep. Maipu", pays=pays)])
        assert ("N75P" in _codes(b.recup)) is attendu, pays


def test_ce_que_la_production_lie_deja_n_est_pas_une_recuperation(tmp_path):
    b = _banc(tmp_path,
              manquants=[("m", "Arsenal", "Chelsea", "L")],
              fixtures=[_fx(1, "Arsenal", "Chelsea")])
    assert b.par_production == 1 and _codes(b.recup) == {}


def test_un_horaire_decale_se_recupere_a_noms_identiques(tmp_path):
    b = _banc(tmp_path,
              manquants=[("m", "Kalmar FF", "Varbergs BoIS", "L")],
              fixtures=[_fx(1, "Kalmar FF", "Varbergs BoIS",
                            quand="2026-09-20T16:30:00+00:00")])
    assert _codes(b.recup) == {"H3": 1, "H6": 1, "H3L": 1}


def test_des_noms_seulement_proches_ne_passent_que_la_variante_large(tmp_path):
    """« Kalmar » contre « Kalmar FF » : 80 en entier, pas 90."""
    b = _banc(tmp_path,
              manquants=[("m", "Kalmar", "Varbergs BoIS", "L")],
              fixtures=[_fx(1, "Kalmar FF", "Varbergs BoIS",
                            quand="2026-09-20T16:30:00+00:00")])
    assert _codes(b.recup) == {"H3L": 1}


def test_un_match_retour_bloque_la_regle_horaire(tmp_path):
    """Les deux mêmes clubs rejouent le lendemain : rien ne dit lequel des
    deux matchs décalés est le nôtre."""
    b = _banc(tmp_path,
              manquants=[("m", "Kalmar FF", "Varbergs BoIS", "L")],
              fixtures=[_fx(1, "Kalmar FF", "Varbergs BoIS",
                            quand="2026-09-20T16:30:00+00:00"),
                        _fx(2, "Varbergs BoIS", "Kalmar FF", statut="NS",
                            quand="2026-09-21T13:00:00+00:00")])
    assert _codes(b.recup) == {}


def test_la_classe_se_leve_seulement_sans_jumeau(tmp_path):
    """Notre match sans ligue, la source ne l'a qu'en féminin : C le règle.
    Si le match des hommes est AUSSI là au même horaire, C refuse."""
    feminin = _fx(1, "Hammarby", "Djurgarden", ligue="Allsvenskan Women")
    b = _banc(tmp_path, manquants=[("m", "Hammarby", "Djurgarden", "")],
              fixtures=[feminin])
    assert _codes(b.recup) == {"C": 1}
    d = tmp_path / "jumeau"
    d.mkdir()
    b = _banc(d, manquants=[("m", "Hammarby", "Djurgarden", "")],
              fixtures=[feminin, _fx(2, "Hammarby", "Djurgarden", statut="NS")])
    assert _codes(b.recup) == {}


def test_deux_regles_en_desaccord_refusent_ensemble(tmp_path, capsys):
    """Le cas de la revue : N80 prend « Atl. Tucuman - Dep. Maipu » à 15 h
    (1-0), H3 le même duel écrit en entier à 17 h (0-2). Chacune seule est
    « sûre » ; ensemble, l'une écrirait un score faux — refusé, et dit."""
    b = _banc(tmp_path,
              reglees=[("k", "Arsenal", "Chelsea", "L", 2, 1, "home", "api-football")],
              manquants=[("m", "Atletico Tucuman", "Deportivo Maipu", "L")],
              fixtures=[_fx(1, "Atl. Tucuman", "Dep. Maipu", hs=1, as_=0),
                        _fx(2, "Atletico Tucuman", "Deportivo Maipu", hs=0, as_=2,
                            quand="2026-09-20T17:00:00+00:00"),
                        _fx(3, "Arsenal", "Chelsea")])
    assert {"N80", "H3"} <= set(_codes(b.recup))
    regles = [g for g in br.REGLES if g.code in ("N80", "H3")]
    pris, desaccords = br.ensemble(b, regles)
    assert pris == [] and len(desaccords) == 1
    pris, desaccords = br.ensemble(b, [g for g in br.REGLES if g.code == "N80"])
    assert pris == ["m"] and desaccords == []
    # Dans le tableau, chacune porte son désaccord : aucune n'est « SÛRE » tout court.
    assert br.desaccords_par_regle(b, regles) == {"N80": 1, "H3": 1}
    br.imprimer(b, J.date())
    lignes = {ligne.split()[0]: ligne for ligne in capsys.readouterr().out.splitlines()
              if ligne.strip() and ligne.split()[0] in {"N80", "H3"}}
    assert "désaccord" in lignes["N80"] and "désaccord" in lignes["H3"]


def test_une_journee_sans_fichier_ne_compte_pas_dans_les_faux_attendus(tmp_path):
    db, dossier = _monde(tmp_path, manquants=[
        ("m", "Arsenal", "Chelsea", "L"),
        ("m2", "Everton", "Fulham", "L", "2026-09-22T15:00:00+00:00")],
        fixtures=[_fx(1, "Kontu", "LPS")])
    manquants, noms, jours = br.charger_manquants(
        str(db), J.date(), datetime(2026, 9, 27, tzinfo=timezone.utc))
    b = br.analyser([], manquants, noms, {}, dossier, jours=jours)
    assert (b.manquants, b.sans_fichier, b.tournent) == (2, 1, 1)


def test_un_match_reporte_ne_prend_pas_le_score_de_son_match_rejoue(tmp_path):
    """Revue du 27/09 : le match 777, reporté (PST) le 20 à 15 h, rejoué le 21
    à 13 h (FT 0-3), garde son identifiant. Notre match du 20 ne doit pas
    recevoir ce score, affiché « écart 0 min » : le pari a pu être annulé."""
    db, dossier = _monde(tmp_path, manquants=[("m", "Kalmar FF", "Varbergs BoIS", "L")],
                         fixtures=[_fx(777, "Kalmar FF", "Varbergs BoIS", statut="PST")])
    (dossier / "2026-09-21.json").write_text(json.dumps({"response": [
        _fx(777, "Kalmar FF", "Varbergs BoIS", hs=0, as_=3,
            quand="2026-09-21T13:00:00+00:00")]}))
    manquants, noms, jours = br.charger_manquants(
        str(db), J.date(), datetime(2026, 9, 27, tzinfo=timezone.utc))
    b = br.analyser([], manquants, noms, {}, dossier, jours=jours)
    assert _codes(b.recup) == {}


def test_la_veille_et_le_lendemain_ne_comptent_que_charges(tmp_path):
    """Un match à 23 h 58 dont le résultat (00 h 05) est dans le fichier du
    lendemain : results-update ne charge ce fichier que si le lendemain a un
    match en attente. Sinon, la production ne le réglerait PAS."""
    db, dossier = _monde(tmp_path, manquants=[
        ("m", "Arsenal", "Chelsea", "L", "2026-09-20T23:58:00+00:00")],
        fixtures=[_fx(1, "Kontu", "LPS")])
    (dossier / "2026-09-21.json").write_text(json.dumps({"response": [
        _fx(2, "Arsenal", "Chelsea", quand="2026-09-21T00:05:00+00:00")]}))
    manquants, noms, jours = br.charger_manquants(
        str(db), J.date(), datetime(2026, 9, 27, tzinfo=timezone.utc))
    assert jours == {J.date()}
    b = br.analyser([], manquants, noms, {}, dossier, jours=jours)
    assert b.par_production == 0
    b = br.analyser([], manquants, noms, {}, dossier, jours=None)
    assert b.par_production == 1


def test_sans_fichier_du_jour_le_voisin_charge_suffit(tmp_path):
    """Pas de fichier pour la journée du match (le 21), mais son résultat est
    dans le fichier de la veille, chargé : la production le règle — ce match
    n'est pas « sans fichier »."""
    db, dossier = _monde(tmp_path, manquants=[
        ("m", "Arsenal", "Chelsea", "L", "2026-09-21T00:03:00+00:00"),
        ("m2", "Everton", "Fulham", "L")],
        fixtures=[_fx(1, "Arsenal", "Chelsea", quand="2026-09-21T00:00:00+00:00")])
    manquants, noms, jours = br.charger_manquants(
        str(db), J.date(), datetime(2026, 9, 27, tzinfo=timezone.utc))
    b = br.analyser([], manquants, noms, {}, dossier, jours=jours)
    assert (b.par_production, b.sans_fichier) == (1, 0)


def test_un_fichier_illisible_est_signale(tmp_path, capsys):
    db, dossier = _monde(tmp_path,
                         reglees=[("k", "Arsenal", "Chelsea", "L", 2, 1, "home",
                                   "api-football")],
                         fixtures=[_fx(1, "Arsenal", "Chelsea")])
    (dossier / "2026-09-21.json").write_text('{"response": [')
    reglees, noms, joues = br.charger_regles(str(db), J.date())
    b = br.analyser(reglees, [], noms, joues, dossier)
    assert b.illisibles == [datetime(2026, 9, 21).date()]
    br.imprimer(b, J.date())
    assert "ILLISIBLE" in capsys.readouterr().out


def test_ce_que_la_production_regle_deja_vient_avec_sa_commande(tmp_path, capsys):
    b = _banc(tmp_path,
              reglees=[("k", "Everton", "Fulham", "L", 2, 1, "home", "api-football")],
              manquants=[("m", "Arsenal", "Chelsea", "L")],
              fixtures=[_fx(1, "Arsenal", "Chelsea"), _fx(2, "Everton", "Fulham")])
    b.maintenant = datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
    br.imprimer(b, J.date())
    assert "results-update --days 8 --sport soccer" in capsys.readouterr().out


# ---------------------------------------------------- les gardes des règles ---
#
# Revue du 27/09 : retirer la marge, le plancher par camp ou le garde de sens
# ne faisait tomber AUCUN test. Ce sont pourtant eux qui rendent une règle
# activable.

def test_la_classe_ne_se_lit_pas_a_l_envers(tmp_path):
    """« Union Santa Fe v Colon » (sans ligue) contre le seul match féminin
    « Union W 2-1 Colon Santa Fe W » : avec la classe de chaque côté,
    l'appariement valait 0 et seuls les noms entiers jugeaient — C écrivait
    1-2. Sans la classe, les deux avis se contredisent : rien n'est écrit."""
    b = _banc(tmp_path, manquants=[("m", "Union Santa Fe", "Colon", "")],
              fixtures=[_fx(1, "Union", "Colon Santa Fe", hs=2, as_=1,
                            ligue="Liga Profesional Women")])
    assert _codes(b.recup) == {}


def test_le_sens_se_lit_sans_la_classe_des_deux_cotes():
    """`_sens` lui-même : avec la classe d'un seul côté, `_orientation` rend
    « inverse » sur « Union Santa Fe v Colon » / « Union W v Colon Santa Fe W »
    (appariement à 0, garde de contradiction muet) ; sans la classe, les deux
    avis se contredisent : None."""
    from src.scores import MatchResult, OurEvent, _orientation
    t = datetime(2026, 9, 20, 15, tzinfo=timezone.utc)
    c = {"evm": OurEvent("m", "Union Santa Fe", "Colon", t),
         "res": MatchResult("soccer", "Union W", "Colon Santa Fe W", t, "home", 2, 1, "t")}
    assert _orientation(c["evm"], c["res"]) == "inverse"
    assert br._sens(c) is None


def test_hors_de_la_zone_calibree_le_sens_doit_etre_net(tmp_path):
    """« Sheffield United v Sheff Wed » contre « Sheff Utd 2-1 Sheffield
    Wednesday » : 74,9 dans l'ordre, 81,4 croisé. N80 ne l'admettait que
    croisé et l'écrivait 1-2."""
    b = _banc(tmp_path,
              manquants=[("m", "Sheffield United", "Sheff Wed", "England - Championship")],
              fixtures=[_fx(1, "Sheff Utd", "Sheffield Wednesday", hs=2, as_=1)])
    assert b.par_production == 0 and _codes(b.recup) == {}


def test_un_rival_a_moins_de_dix_points_bloque_les_regles_n(tmp_path):
    """« Atl. Tucuman - Dep. Maipu » (82,1) et, au même horaire, « Atl.
    Tucuman - Dep. Moron » (77,8), non terminé : moins de 10 points d'écart,
    N refuse — sans la marge, elle prenait le premier."""
    b = _banc(tmp_path,
              manquants=[("m", "Atletico Tucuman", "Deportivo Maipu", "L")],
              fixtures=[_fx(1, "Atl. Tucuman", "Dep. Maipu", hs=1, as_=0),
                        _fx(2, "Atl. Tucuman", "Dep. Moron", statut="NS")])
    assert not {"N80", "N75", "N70"} & set(_codes(b.recup))


def test_un_camp_sous_60_bloque_les_regles_n():
    """Un camp à 100, l'autre à 50 : la moyenne passe 75, pas le plancher."""
    c = {"dt": 0.0, "g": 75.0, "g_cotes": (100.0, 50.0), "u": 75.0, "entier": 50.0,
         "res": None, "x": {}, "ident": ("id", "1")}
    regle = next(g for g in br.REGLES if g.code == "N75")
    assert br.choisir(regle, [c]) == (None, "rien")


def test_un_sens_indecidable_n_est_jamais_ecrit(tmp_path):
    """« River v River Plate » contre « River Plate v River Plate Montevideo » :
    la production l'apparie et ne sait pas le lire ; aucune règle ne le
    règle à sa place."""
    b = _banc(tmp_path, manquants=[("m", "River", "River Plate", "L")],
              fixtures=[_fx(1, "River Plate", "River Plate Montevideo", hs=2, as_=1)])
    assert b.par_production == 0 and _codes(b.recup) == {}


def test_l_exemple_dit_le_score_tel_qu_il_serait_ecrit(tmp_path, capsys):
    b = _banc(tmp_path,
              reglees=[("k", "Arsenal", "Chelsea", "L", 2, 1, "home", "api-football")],
              manquants=[("m", "Deportivo Maipu", "Atletico Tucuman", "L")],
              fixtures=[_fx(1, "Atl. Tucuman", "Dep. Maipu", hs=1, as_=0),
                        _fx(2, "Arsenal", "Chelsea")])
    br.imprimer(b, J.date())
    out = capsys.readouterr().out
    assert "écrit chez nous : 0-1 (source 1-0, sens inverse)" in out


def test_un_match_non_termine_ne_se_recupere_jamais(tmp_path):
    b = _banc(tmp_path,
              manquants=[("m", "Atletico Tucuman", "Deportivo Maipu", "L")],
              fixtures=[_fx(1, "Atl. Tucuman", "Dep. Maipu", statut="PST")])
    assert _codes(b.recup) == {}


# ---------------------------------------------------------------- pièces ---

@pytest.mark.parametrize("ligue, pays", [
    ("Scotland - Premiership", "scotland"),
    ("Costa Rica - Primera Division", "costa rica"),
    ("Premiership", ""), ("", ""),
])
def test_le_pays_de_notre_ligue(ligue, pays):
    assert br.pays_de_ligue(ligue) == pays


def test_le_pays_de_la_source_se_compare_sans_tiret():
    assert br._sans_accents("Costa-Rica") == br.pays_de_ligue("Costa Rica - Liga FPD")


# ---------------------------------------------------------------- sortie ---

def test_la_sortie_dit_quelle_regle_est_sure(tmp_path, capsys):
    b = _banc(tmp_path,
              reglees=[("k", "Sporting Gijon", "Racing Santander", "L", 2, 1, "home",
                        "api-football")],
              manquants=[("m", "Atletico Tucuman", "Deportivo Maipu", "L")],
              fixtures=[_fx(1, "Sporting Gijon", "Racing Santander"),
                        _fx(2, "Sporting Braga", "Racing Ferrol", hs=0, as_=0),
                        _fx(3, "Atl. Tucuman", "Dep. Maipu", hs=1, as_=0)])
    br.imprimer(b, J.date())
    out = capsys.readouterr().out
    lignes = {ligne.split()[0]: ligne for ligne in out.splitlines()
              if ligne.strip() and ligne.split()[0] in {g.code for g in br.REGLES}}
    assert lignes["N80"].rstrip().endswith("SÛRE")
    assert lignes["N75"].rstrip().endswith("à écarter")
    assert "ERREURS À L'AVEUGLE — noms ≥ 75, même horaire" in out
    assert "Sporting Braga - Racing Ferrol" in out
    assert "RÉCUPÉRÉS — N80" in out
    assert "Couverture football" in out


def test_la_sonde_complete_tourne_sur_une_base(tmp_path, capsys, monkeypatch):
    db, dossier = _monde(tmp_path,
                         reglees=[("k", "Arsenal", "Chelsea", "L", 2, 1, "home",
                                   "api-football")],
                         fixtures=[_fx(1, "Arsenal", "Chelsea")])
    monkeypatch.setenv("SCORES_INGEST_DIR", str(tmp_path))
    br.main(["--db", str(db), "--depuis", "2026-09-01"])
    out = capsys.readouterr().out
    assert "BANC D'ESSAI" in out and "1 matchs de la source déjà réglés" in out


def test_la_sonde_est_en_lecture_seule(tmp_path, monkeypatch):
    db, dossier = _monde(tmp_path,
                         reglees=[("k", "Arsenal", "Chelsea", "L", 2, 1, "home",
                                   "api-football")],
                         fixtures=[_fx(1, "Arsenal", "Chelsea")])
    avant = db.read_bytes()
    monkeypatch.setenv("SCORES_INGEST_DIR", str(tmp_path))
    br.main(["--db", str(db), "--depuis", "2026-09-01"])
    assert db.read_bytes() == avant
