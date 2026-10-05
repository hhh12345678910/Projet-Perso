"""La validation hors échantillon : une méthode ne voit JAMAIS les paris sur
lesquels elle est jugée."""
from __future__ import annotations

import pytest

from scripts import valider_mises as vm
from src.alerter import _round_stake


def _p(ev, gagne, cote=2.0, clv=5.0):
    return {"ev": ev, "cote": cote, "statut": "won" if gagne else "lost", "clv": clv}


def test_les_bandes():
    assert [vm.bande(e) for e in (5, 7.9, 8, 14.9, 15, 34.9, 35, 90)] == [0, 0, 1, 1, 2, 2, 3, 3]


def test_l_optimisation_choisit_ce_qui_a_paye_a_capital_egal():
    # Les grosses EV gagnent toutes, les petites perdent toutes : l'optimum
    # met le maximum sur 15 %+ et le minimum dessous, à moyenne ≈ 35 €.
    passe = [_p(6, False)] * 50 + [_p(10, False)] * 50 + [_p(20, True)] * 30 + [_p(40, True)] * 10
    g = vm.regle_optimisee(passe, 35.0)
    assert g[0] <= g[1] <= g[2] <= g[3] and g[2] > g[1]
    n = [50, 50, 30, 10]
    assert abs(sum(s * c for s, c in zip(g, n)) / 140 - 35) <= vm.TOLERANCE_MOY


def test_la_regle_d_un_bloc_ne_depend_que_du_passe():
    """Changer les résultats du DERNIER bloc ne change aucune règle choisie
    pour lui : elle a été fixée sur les blocs précédents."""
    base = ([_p(6, i % 2 == 0) for i in range(40)] + [_p(10, i % 3 == 0) for i in range(40)]
            + [_p(20, i % 2 == 1) for i in range(40)] + [_p(40, i % 4 == 0) for i in range(40)])
    import random
    random.Random(1).shuffle(base)
    autre = base[:120] + [dict(p, statut="won") for p in base[120:]]
    a = vm.valider(base, 35.0, _round_stake, k=4)
    b = vm.valider(autre, 35.0, _round_stake, k=4)
    for nom in a:
        assert a[nom]["regles"] == b[nom]["regles"], nom
    # Mais les gains jugés, eux, changent.
    assert a["fixe"]["gains"] != b["fixe"]["gains"]


def test_chaque_methode_est_jugee_a_capital_egal():
    paris = [_p(e, i % 3 == 0) for i, e in enumerate([6, 10, 20, 40] * 40)]
    res = vm.valider(paris, 35.0, _round_stake, k=4)
    capital = sum(res["fixe"]["mises"])
    for nom, r in res.items():
        assert sum(r["mises"]) == pytest.approx(capital), nom


def test_trop_peu_de_paris():
    with pytest.raises(ValueError):
        vm.valider([_p(10, True)] * 5, 35.0, _round_stake, k=4)


def test_le_script_affiche_la_validation(tmp_path, capsys):
    from scripts import staking_curves as sc
    from tests.test_staking_curves import _base
    with pytest.raises(ValueError):          # 5 paris : trop peu pour 4 blocs
        sc.main(["--db", str(_base(tmp_path)), "--valider"])
    capsys.readouterr()
    assert sc.main(["--db", str(_base(tmp_path / "b")), "--valider", "2"]) == 0
    assert "VALIDATION HORS ÉCHANTILLON" in capsys.readouterr().out
