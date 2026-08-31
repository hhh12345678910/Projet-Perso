"""La sonde d'impact ne doit pas fabriquer de surplus qui n'existent pas.

Elle traite les opportunites groupe par groupe et vide sa table temporaire
aux frontieres, pour rester lineaire (27 min -> 7,5 min sur 36 000 lignes).
Le risque est unique et precis : purger AU MILIEU d'un groupe effacerait
l'historique dont le dedoublonnage a besoin, et chaque re-detection
paraitrait alors etre un surplus.

`test_purger_a_chaque_groupe_ne_change_rien` est le garde-fou : il force la
purge au maximum et exige un resultat identique. Si la purge migrait hors
des frontieres de groupe, il casserait.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import scripts.impact_dedoublonnage as imp
from src.alerter import TelegramConfig
from src.storage import Storage

T0 = datetime(2026, 8, 1, 10, 0, tzinfo=timezone.utc)


def _cfg(**kw) -> TelegramConfig:
    base = dict(bot_token="t", chat_id="PRINCIPAL", premium_chat_id="PREMIUM",
                critical_chat_id="CRITIQUE", min_minutes_to_kickoff=15)
    base.update(kw)
    return TelegramConfig(**base)


def _base(tmp_path, lignes) -> str:
    """`lignes` : (cle, ev, cote, minutes_apres_T0, last_ev, last_odd)."""
    chemin = str(tmp_path / "d.db")
    Storage(chemin)
    c = sqlite3.connect(chemin)
    for cle, ev, cote, apres, last_ev, last_odd in lignes:
        det = T0 + timedelta(minutes=apres)
        depart = det + timedelta(hours=6)
        c.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?)",
                  (cle, "soccer", "Pro League", "a", "b", depart.isoformat()))
        c.execute(
            "INSERT INTO value_bets(event_key, book, market, outcome_label, line,"
            " odd_taken, fair_prob, fair_odd, ev_pct, kelly_pct, detected_at,"
            " last_seen_at, last_odd, last_ev) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cle, "unibet_be", "h2h", "home", None, cote, 0.5, 2.0, ev, 1.0,
             det.isoformat(),
             (det + timedelta(hours=1)).isoformat() if last_ev is not None else None,
             last_odd, last_ev))
    c.commit()
    c.close()
    return chemin


# Trois detections du MEME match a des minutes differentes : `_event_key_like`
# les regroupe (meme date, memes equipes). C'est le motif observe sur la VM.
_MEME_MATCH = [
    ("202608011800::pedrovives__vs__bernardomunk", 7.5, 3.05, 0, None, None),
    ("202608011746::pedrovives__vs__bernardomunk", 7.5, 3.05, 5, None, None),
    ("202608011722::pedrovives__vs__bernardomunk", 5.3, 3.05, 10, None, None),
]


def test_un_pari_immobile_ne_produit_aucun_surplus(tmp_path):
    """Le cas le plus courant, et celui qu'il serait le plus grave de rater :
    une re-detection a EV inchangee ne doit apparaitre nulle part."""
    db = _base(tmp_path, [("202608011800::a__vs__b", 6.0, 2.00, 0, 6.1, 2.00)])
    r = imp.analyser(db, _cfg())
    assert r["surplus"] == []
    assert dict(r["ancien"]) == dict(r["nouveau"])


def test_une_traversee_de_bande_produit_un_surplus(tmp_path):
    """7,9 -> 8,5 : le premium n'a jamais vu ce pari, il l'envoie."""
    db = _base(tmp_path, [("202608011800::a__vs__b", 7.9, 2.10, 0, 8.5, 2.10)])
    r = imp.analyser(db, _cfg())
    assert len(r["surplus"]) == 1
    s = r["surplus"][0]
    assert s["canal"] == "PREMIUM"
    assert s["bande_changee"] is True
    assert r["nouveau"]["PREMIUM"] - r["ancien"]["PREMIUM"] == 1


def test_un_changement_de_tranche_de_cote_a_EV_constante(tmp_path):
    """Cote 3,90 -> 4,50 : deux tranches, mais la meme bande premium et une
    EV immobile. Aucun surplus — la sonde ne doit pas compter un changement
    de tranche qui ne change pas de destination."""
    db = _base(tmp_path, [("202608011800::a__vs__b", 22.0, 3.90, 0, 22.0, 4.50)])
    r = imp.analyser(db, _cfg())
    assert r["surplus"] == []


def test_le_regroupement_par_date_et_equipes_est_respecte(tmp_path):
    """Trois lignes du meme match : le dedoublonnage les traite comme UNE
    opportunite, exactement comme en production."""
    r = imp.analyser(_base(tmp_path, _MEME_MATCH), _cfg())
    assert r["lignes"] == 3
    # Une seule alerte cote ancien : les deux suivantes sont dedoublonnees
    # (EV identique, puis ecart de 2,2 points > le delta de 2,0).
    assert sum(r["ancien"].values()) == 2


def test_purger_a_chaque_groupe_ne_change_rien(tmp_path, monkeypatch):
    """LE garde-fou de l'optimisation. La purge n'est sure qu'aux frontieres
    de groupe ; si elle migrait a l'interieur, chaque re-detection paraitrait
    un surplus. Forcer la purge au maximum doit etre invisible."""
    lignes = _MEME_MATCH + [
        ("202608021800::c__vs__d", 7.9, 2.10, 0, 8.5, 2.10),
        ("202608031800::e__vs__f", 40.0, 12.0, 0, None, None),
        ("202608041800::g__vs__h", 6.0, 2.00, 0, 6.1, 2.00),
    ]
    db = _base(tmp_path, lignes)
    normal = imp.analyser(db, _cfg())
    monkeypatch.setattr(imp, "PURGE_AU_DELA", 0)   # purge a chaque groupe
    force = imp.analyser(db, _cfg())

    assert dict(normal["ancien"]) == dict(force["ancien"])
    assert dict(normal["nouveau"]) == dict(force["nouveau"])
    assert len(normal["surplus"]) == len(force["surplus"])
    assert dict(normal["motifs"]) == dict(force["motifs"])


def test_la_sonde_n_ecrit_rien_dans_la_base_lue(tmp_path):
    """LECTURE SEULE, verifie et non pas affirme."""
    db = _base(tmp_path, _MEME_MATCH)
    with sqlite3.connect(db) as c:
        avant = c.execute("SELECT COUNT(*) FROM value_bets").fetchone()[0]
        marques_avant = c.execute(
            "SELECT COUNT(*) FROM notified_value_bets").fetchone()[0]
    imp.analyser(db, _cfg())
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT COUNT(*) FROM value_bets").fetchone()[0] == avant
        assert c.execute(
            "SELECT COUNT(*) FROM notified_value_bets").fetchone()[0] == marques_avant
        assert c.execute("SELECT COUNT(*) FROM channels").fetchone()[0] == 0


def test_une_base_sans_detection_ne_plante_pas(tmp_path):
    r = imp.analyser(_base(tmp_path, []), _cfg())
    assert r["lignes"] == 0 and r["surplus"] == [] and r["jours"] == 0.0


def test_le_tri_par_groupe_protege_les_groupes_entrelaces(tmp_path, monkeypatch):
    """Sans le tri, les lignes d'un meme match ne sont pas adjacentes et une
    purge peut tomber ENTRE elles — la seconde detection verrait alors une
    table vide et paraitrait un surplus.

    Ecrit apres avoir constate que la premiere version de ce fichier ne
    detectait PAS la suppression du tri : ses fixtures avaient leurs groupes
    deja groupes par ordre d'insertion. Ici les deux matchs alternent, ce que
    la production produit naturellement (deux matchs suivis en parallele)."""
    lignes = []
    for tour in range(3):
        for match, (jour, eq) in enumerate((("01", "aaa__vs__bbb"),
                                            ("02", "ccc__vs__ddd"))):
            lignes.append((f"202608{jour}18{10 + tour * 7:02d}::{eq}",
                           6.0, 2.00, tour * 5 + match, None, None))
    db = _base(tmp_path, lignes)
    monkeypatch.setattr(imp, "PURGE_AU_DELA", 0)   # purge a chaque frontiere
    r = imp.analyser(db, _cfg())

    # Deux matchs, trois detections chacun a EV identique : le dedoublonnage
    # n'en laisse passer qu'une par match, ancien comme nouveau.
    assert sum(r["ancien"].values()) == 2, dict(r["ancien"])
    assert r["surplus"] == [], (
        "une purge est tombee au milieu d'un groupe — le tri ne protege plus")
