"""La sonde du seuil des `under` (§21.9 pt 4).

Ce qui est vérifié ici n'est pas le formatage mais la conclusion : sur une
population dont on connaît la pente à l'avance, la sonde doit annoncer la
bonne. Une sonde qui se trompe de verdict enverrait le projet couper du
volume pour rien — c'est la panne silencieuse du §11, appliquée à une mesure.
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone

from scripts import under_threshold
from src.models import Book, MarketType, Outcome, ValueBet
from src.storage import Storage


def _base(tmp_path, unders, overs=(), h2h=()):
    """Construit une base avec des paris clôturés dont la CLV est imposée.

    `clv_pct = odd_taken / closing_fair_odd - 1`, donc pour viser une CLV c on
    fixe la cote prise et on en déduit la clôture juste.
    """
    db = str(tmp_path / "t.db")
    st = Storage(db)
    t0 = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
    n = 0
    for marche, cote, lot in ((MarketType.TOTALS, "under", unders),
                              (MarketType.TOTALS, "over", overs),
                              (MarketType.H2H, "home", h2h)):
        for ev, ligne, clv in lot:
            n += 1
            # event_key distinct par pari : la dédup d'insert_value_bet porte
            # sur (event, book, marché, côté, ligne) et fusionnerait les lots.
            ek = f"2026080112{n:04d}::a__vs__b"
            st.upsert_event(ek, "soccer", "L", "A", "B", t0 + timedelta(hours=3))
            odd = 2.0
            vid = st.insert_value_bet(ValueBet(
                event_key=ek, book=Book.UNIBET_BE, market=marche,
                outcome=Outcome(label=cote, line=ligne), odd_taken=odd,
                fair_prob=0.5, fair_odd=1.9, ev_pct=ev, kelly_stake_pct=1.0,
                detected_at=t0 + timedelta(minutes=n),
            ))
            st.insert_clv_snapshot(
                value_bet_id=vid, pinnacle_odd=odd, pinnacle_prob=0.5,
                snapshot_at=t0 + timedelta(hours=2), closing=True,
                fair_odd=odd / (1.0 + clv), fair_prob=0.5, overround=1.02,
            )
    return db


def _sortie(capsys, db, *args):
    argv = sys.argv
    sys.argv = ["under_threshold", "--db", db, *args]
    try:
        under_threshold.main()
    finally:
        sys.argv = argv
    return capsys.readouterr().out


def test_pente_plate_deconseille_le_seuil(tmp_path, capsys):
    """CLV identique à toutes les EV : la sonde doit dire que le seuil ne trie rien."""
    unders = [(ev, 3.5, 0.04) for ev in (2.5, 3.5, 5.0, 7.0, 10.0, 15.0) for _ in range(30)]
    out = _sortie(capsys, _base(tmp_path, unders))
    assert "INDISCERNABLE DE ZÉRO" in out
    assert "règle de LIGNE" in out


def test_pente_plate_mais_bruitee_reste_plate(tmp_path, capsys):
    """Le cas réaliste, et la panne que cette sonde a réellement produite.

    EV et CLV tirées indépendamment : la vraie pente est nulle. Sur des données
    bruitées, une régression sans barre d'erreur sort spontanément une pente de
    l'ordre de +0,1 et conclut « la CLV monte avec l'EV ». Conseiller de relever
    le seuil sur ce bruit ferait couper du volume pour rien — la panne
    silencieuse du §11 déguisée en mesure.
    """
    rnd = random.Random(7)
    unders = [(rnd.uniform(2.0, 15.0), 3.5, rnd.gauss(0.04, 0.35)) for _ in range(437)]
    out = _sortie(capsys, _base(tmp_path, unders))
    assert "INDISCERNABLE DE ZÉRO" in out
    assert "monte avec l'EV" not in out


def test_pente_montante_valide_le_seuil(tmp_path, capsys):
    """CLV croissante avec l'EV : la sonde doit valider le principe du seuil."""
    unders = [(ev, 3.5, ev / 100.0) for ev in (2.5, 3.5, 5.0, 7.0, 10.0, 15.0) for _ in range(30)]
    out = _sortie(capsys, _base(tmp_path, unders))
    assert "monte avec l'EV" in out
    assert "INDISCERNABLE" not in out


def test_pente_montante_sous_bruit_reste_detectee(tmp_path, capsys):
    """Une vraie pente noyée dans du bruit doit quand même ressortir.

    Contrepartie du test précédent : une barre d'erreur trop large rendrait la
    sonde aveugle à tout, ce qui la ferait toujours répondre « ne rien faire ».
    """
    rnd = random.Random(11)
    unders = [(ev, 3.5, rnd.gauss(0.01 * ev, 0.20))
              for _ in range(600) for ev in [rnd.uniform(2.0, 15.0)]]
    out = _sortie(capsys, _base(tmp_path, unders))
    assert "monte avec l'EV" in out


def test_balayage_compte_garde_et_jete(tmp_path, capsys):
    """Le tableau 4 doit répartir exactement la population de part et d'autre."""
    unders = [(2.5, 3.5, 0.02)] * 40 + [(9.0, 3.5, 0.09)] * 60
    out = _sortie(capsys, _base(tmp_path, unders))
    ligne = [l for l in out.splitlines() if l.strip().startswith("8.0 %")][0]
    # seuil 8 % : les 60 à EV 9 sont gardés, les 40 à EV 2,5 sont jetés.
    assert "    60" in ligne and "    40" in ligne


def test_bandes_de_lignes_separent_les_hautes(tmp_path, capsys):
    """Le tableau 2 doit isoler 4.5 des lignes basses."""
    unders = ([(5.0, 2.5, 0.08)] * 30 + [(5.0, 4.5, 0.01)] * 30)
    out = _sortie(capsys, _base(tmp_path, unders))
    b25 = [l for l in out.splitlines() if l.startswith("under ≤ 2.5")][0]
    b45 = [l for l in out.splitlines() if l.startswith("under 4.5")][0]
    assert "+8.00 %" in b25 and "+1.00 %" in b45


def test_sans_under_ne_plante_pas(tmp_path, capsys):
    """Base sans `under` clôturé : message clair, pas une trace."""
    out = _sortie(capsys, _base(tmp_path, [], overs=[(5.0, 2.5, 0.06)] * 5))
    assert "Aucun `under` clôturé" in out


def test_pente_globale_est_signalee_comme_telle(tmp_path, capsys):
    """Même pente sur h2h, over et under : la sonde doit refuser d'y voir un
    défaut propre aux `under`.

    C'est la confusion qui ferait relever le seuil du seul marché regardé,
    en prenant pour une faiblesse locale une propriété de tout le flux.
    """
    rnd = random.Random(3)
    lot = lambda k: [(ev, 2.5, rnd.gauss(0.01 * ev, 0.10))
                     for _ in range(k) for ev in [rnd.uniform(5.0, 15.0)]]
    out = _sortie(capsys, _base(tmp_path, lot(400), overs=lot(400), h2h=lot(400)))
    assert "MÊME pente sur les autres marchés" in out
    assert "seuil GLOBAL" in out


def test_pente_proche_de_un_alerte_sur_la_clv_qui_repete_l_ev(tmp_path, capsys):
    """CLV = EV exactement : la ligne juste de clôture n'a pas bougé.

    La CLV ne mesure alors plus rien d'indépendant — elle réénonce l'EV. La
    sonde doit le dire avant qu'on règle un seuil sur ces chiffres (§20.4).
    """
    rnd = random.Random(5)
    unders = [(ev, 2.5, ev / 100.0) for _ in range(300) for ev in [rnd.uniform(5.0, 15.0)]]
    out = _sortie(capsys, _base(tmp_path, unders))
    assert "répéter" in out and "§20.4" in out


def test_pente_franchement_sous_un_ne_declenche_pas_l_alerte(tmp_path, capsys):
    """Contrepartie : une CLV qui régresse vers zéro est le cas SAIN.

    La ligne de clôture s'écarte de celle de la détection, donc la CLV apporte
    une information que l'EV ne contenait pas. Pas d'alerte attendue.
    """
    rnd = random.Random(9)
    unders = [(ev, 2.5, rnd.gauss(0.003 * ev, 0.05))
              for _ in range(600) for ev in [rnd.uniform(5.0, 15.0)]]
    out = _sortie(capsys, _base(tmp_path, unders))
    assert "répéter" not in out
