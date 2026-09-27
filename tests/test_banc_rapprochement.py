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
    manquants, noms2 = br.charger_manquants(str(db), J.date(),
                                            datetime(2026, 9, 27, tzinfo=timezone.utc))
    return br.analyser(reglees, manquants, {**noms2, **noms}, joues, dossier)


def _codes(d) -> dict:
    return {k: len(v) for k, v in d.items() if v}


# ------------------------------------------------------------ à l'aveugle ---

def test_un_match_sans_sosie_ne_fait_aucune_erreur(tmp_path):
    b = _banc(tmp_path,
              reglees=[("k", "Arsenal", "Chelsea", "L", 2, 1, "home", "api-football")],
              fixtures=[_fx(1, "Arsenal", "Chelsea")])
    assert b.epreuves == 1 and b.hors_epreuve == 0
    assert _codes(b.erreurs) == {} and _codes(b.autres) == {}
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
    # Le vrai match présent, elles le choisissent bien : 23 points d'avance.
    assert _codes(b.autres) == {}
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
    """Le même match servi deux fois (le second non terminé) : caché avec le
    vrai, jamais compté comme une erreur."""
    b = _banc(tmp_path,
              reglees=[("k", "Arsenal", "Chelsea", "L", 2, 1, "home", "api-football")],
              fixtures=[_fx(1, "Arsenal", "Chelsea"),
                        _fx(2, "Arsenal", "Chelsea", statut="NS")])
    assert b.epreuves == 1 and _codes(b.erreurs) == {}


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
    assert "BANC D'ESSAI" in out and "1 matchs déjà réglés" in out


def test_la_sonde_est_en_lecture_seule(tmp_path, monkeypatch):
    db, dossier = _monde(tmp_path,
                         reglees=[("k", "Arsenal", "Chelsea", "L", 2, 1, "home",
                                   "api-football")],
                         fixtures=[_fx(1, "Arsenal", "Chelsea")])
    avant = db.read_bytes()
    monkeypatch.setenv("SCORES_INGEST_DIR", str(tmp_path))
    br.main(["--db", str(db), "--depuis", "2026-09-01"])
    assert db.read_bytes() == avant
