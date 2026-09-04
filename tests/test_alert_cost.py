"""La sonde `alert_cost` accuse-t-elle le bon coupable — et sait-elle se taire ?

Une sonde qui ne peut que confirmer ne prouve rien. Ces tests plantent les
DEUX journaux : celui où `dRest` suit le nombre d'alertes (l'hypothèse est
vraie) et celui où `dRest` est gros alors qu'aucune alerte n'est partie
(l'hypothèse est fausse). Le verdict doit basculer.

C'est la leçon de `closing_gap` le 04/09 : les branches du verdict y étaient
inversées, et la sonde aurait annoncé « hypothèse écartée » exactement quand
elle était confirmée. Rien dans le code ne le disait — seul un journal planté
dont on connaît la réponse pouvait le montrer.
"""
from __future__ import annotations

import io

import pytest
from rich.console import Console

from scripts.alert_cost import (RE_BETS, RE_ENVOI, RE_PAIRE, RE_PHASES,
                                _pente_origine, _pearson, lire, main)
from src.orchestration import Chrono, ligne_phases


def _rendu(markup: str) -> str:
    """Tel que ça atterrit dans valuebet.log : `rich` hors terminal, 80 col."""
    buf = io.StringIO()
    Console(file=buf, width=80).print(markup)
    return buf.getvalue().rstrip("\n")


# ── Le lien avec la production ───────────────────────────────────────

def test_la_ligne_de_phases_de_production_est_lue():
    ch = Chrono()
    ch.par_phase.update({"fetch": 20.9, "detc_reste": 118.6, "base": 1.2,
                         "fair": 1.0})
    rendu = _rendu(ligne_phases("soccer", ch))
    m = RE_PHASES.match(rendu.strip())
    assert m, f"la regex ne matche pas : {rendu!r}"
    ph = {k: float(v) for k, v in RE_PAIRE.findall(m.group(2))}
    assert ph["dRest"] == pytest.approx(118.6)


def test_dRest_n_est_pas_lu_comme_est():
    """La classe `[a-z]` seule capturait « est 118.6 » — la phase changeait de
    nom en silence et disparaissait de tout tableau qui la cherchait."""
    ph = dict(RE_PAIRE.findall("fetch 20.9 dRest 118.6 base 1.2"))
    assert "dRest" in ph
    assert "est" not in ph


def test_insVB_n_est_pas_perdu():
    """`[a-z]+ [\\d.]+` ne trouvait AUCUNE paire dans « insVB 2.3 » : la phase
    n'apparaissait nulle part, ni comme fausse valeur ni comme absence."""
    ph = dict(RE_PAIRE.findall("insVB 2.3 seed 0.4"))
    assert ph["insVB"] == "2.3"


def test_la_ligne_d_envoi_de_production_est_lue():
    rendu = _rendu("[dim]\\[soccer]   → 37 value bet alert(s) sent[/dim]")
    m = RE_ENVOI.match(rendu.strip())
    assert m and m.group(1) == "soccer" and int(m.group(2)) == 37


def test_la_ligne_de_paris_de_production_est_lue():
    rendu = _rendu("\\[soccer]   value bets: 74 total")
    m = RE_BETS.match(rendu.strip())
    assert m and int(m.group(2)) == 74


# ── L'arithmétique ───────────────────────────────────────────────────

def test_pente_par_l_origine():
    # y = 3,2 x exactement.
    assert _pente_origine([(1.0, 3.2), (10.0, 32.0)]) == pytest.approx(3.2)


def test_pente_sans_donnee_est_none_pas_zero():
    """Zéro serait un chiffre ; None dit qu'on ne sait pas. Le projet ne rend
    jamais zéro à la place d'une valeur inconnue."""
    assert _pente_origine([(0.0, 5.0)]) is None


def test_pearson_parfait():
    assert _pearson([(1.0, 3.2), (2.0, 6.4), (3.0, 9.6)]) == pytest.approx(1.0)


def test_pearson_refuse_moins_de_trois_points():
    assert _pearson([(1.0, 3.2), (2.0, 6.4)]) is None


# ── Les journaux plantés ─────────────────────────────────────────────

def _journal(lignes: list[str], tmp_path, nom="valuebet.log"):
    p = tmp_path / nom
    p.write_text("\n".join(lignes) + "\n", encoding="utf-8")
    return p


def _cycle(num: int, drest: float, alertes: int, paris: int, tot: float) -> list[str]:
    l = [f"══ CYCLE {num} — 2026-09-04 12:00:00",
         f"[soccer]   ⏱ dRest {drest:.1f} fetch 20.9 tot {tot:.1f}s",
         f"[soccer]   value bets: {paris} total"]
    if alertes:
        l.append(f"[soccer]   → {alertes} value bet alert(s) sent")
    return l


def test_journal_ou_l_hypothese_est_vraie(tmp_path, capsys, monkeypatch):
    """dRest = 3,2 s × alertes, et zéro quand rien n'est envoyé."""
    lignes = []
    for i, n in enumerate([0, 5, 12, 37, 20, 0, 8], start=1):
        lignes += _cycle(i, 3.2 * n, n, n + 4, 25.0 + 3.2 * n)
    p = _journal(lignes, tmp_path)
    monkeypatch.setattr("sys.argv", ["alert_cost", "--log", str(p)])
    main()
    out = capsys.readouterr().out
    assert "HYPOTHÈSE CONFIRMÉE" in out
    assert "HYPOTHÈSE ÉCARTÉE" not in out


def test_journal_ou_le_temoin_dit_non(tmp_path, capsys, monkeypatch):
    """Des cycles SANS aucune alerte portent 100 s de dRest : quoi que dise la
    corrélation, le temps ne vient pas de l'envoi."""
    lignes = []
    for i, n in enumerate([0, 0, 0, 5, 12, 0, 8], start=1):
        lignes += _cycle(i, 100.0 + 3.2 * n, n, n + 4, 125.0)
    p = _journal(lignes, tmp_path)
    monkeypatch.setattr("sys.argv", ["alert_cost", "--log", str(p)])
    main()
    out = capsys.readouterr().out
    assert "HYPOTHÈSE ÉCARTÉE" in out
    assert "HYPOTHÈSE CONFIRMÉE" not in out
    assert "portent quand même" in out


def test_une_pente_sous_l_intervalle_reste_confirmee(tmp_path, capsys,
                                                     monkeypatch):
    """LE CAS RÉEL DU 04/09, ET L'ERREUR DE LA PREMIÈRE VERSION.

    118,6 s pour 74 paris = 1,60 s par pari, moitié moins que l'intervalle de
    3,2 s. La sonde exigeait alors une pente d'au moins 0,8 × l'intervalle et
    aurait rejeté l'hypothèse sur les données mêmes qui l'ont fait naître.

    Le modèle est un MAXIMUM PAR CHAT : une pente sous l'intervalle dit
    seulement qu'un pari sur deux est dédoublonné sur le canal le plus chargé.
    C'est normal, et ça doit rester confirmable."""
    lignes = []
    for i, n in enumerate([0, 10, 40, 74, 30, 0, 20], start=1):
        # La moitié des paris passe le dédoublonnage sur le canal le plus
        # chargé : dRest = 3,2 × n/2 = 1,6 n.
        lignes += _cycle(i, 1.6 * n, n, n + 4, 28.5 + 1.6 * n)
    p = _journal(lignes, tmp_path)
    monkeypatch.setattr("sys.argv", ["alert_cost", "--log", str(p)])
    main()
    out = capsys.readouterr().out
    assert "HYPOTHÈSE CONFIRMÉE" in out, out


def test_journal_ou_la_borne_est_franchie(tmp_path, capsys, monkeypatch):
    """Témoin muet, corrélation parfaite — mais 10 s par pari, soit trois
    messages par pari sur UN canal. Un canal reçoit au plus un message par
    pari : les pauses ne peuvent pas produire ce temps."""
    lignes = []
    for i, n in enumerate([0, 5, 12, 37, 20, 0, 8], start=1):
        lignes += _cycle(i, 10.0 * n, n, n + 4, 25.0 + 10.0 * n)
    p = _journal(lignes, tmp_path)
    monkeypatch.setattr("sys.argv", ["alert_cost", "--log", str(p)])
    main()
    out = capsys.readouterr().out
    assert "HYPOTHÈSE ÉCARTÉE" in out, out
    assert "plus de messages" in out


def test_l_absence_de_ligne_d_envoi_vaut_zero_pas_inconnu(tmp_path):
    """`→ N alert(s) sent` n'est imprimée que si N > 0 : son absence est un
    zéro mesuré, pas une mesure manquante."""
    p = _journal(_cycle(1, 0.0, 0, 3, 25.0), tmp_path)
    lots, _, _ = lire(p, None)
    assert lots == [((1, 1), "soccer", 0.0, 25.0, 0, 3)]


def test_les_series_ne_se_melangent_pas(tmp_path):
    """Un redémarrage remet le compteur à 1 ; le cycle 1 d'après ne doit pas
    écraser le cycle 1 d'avant."""
    lignes = _cycle(1, 10.0, 3, 5, 30.0) + _cycle(2, 20.0, 6, 9, 40.0) \
        + _cycle(1, 99.0, 30, 40, 130.0)
    p = _journal(lignes, tmp_path)
    lots, ordre, _ = lire(p, None)
    assert len(lots) == 3
    assert {l[0] for l in lots} == {(1, 1), (1, 2), (2, 1)}
    assert ordre == [(1, 1), (1, 2), (2, 1)]


def test_derniers_prend_la_serie_recente_pas_la_plus_longue(tmp_path, capsys,
                                                            monkeypatch):
    """Le piège de `book_latency` : trier par numéro rendait les cycles
    d'AVANT le redémarrage quand on demandait les derniers."""
    lignes = []
    for i in range(1, 11):                      # ancienne série, chère
        lignes += _cycle(i, 100.0, 30, 40, 130.0)
    for i in range(1, 3):                       # nouvelle série, bon marché
        lignes += _cycle(i, 3.2, 1, 4, 25.0)
    p = _journal(lignes, tmp_path)
    monkeypatch.setattr("sys.argv", ["alert_cost", "--log", str(p), "--derniers", "2"])
    main()
    out = capsys.readouterr().out
    assert "2 lots" in out
    assert "100.0" not in out


def test_le_filtre_sport(tmp_path):
    lignes = ["══ CYCLE 1 — 2026-09-04 12:00:00",
              "[soccer]   ⏱ dRest 118.6 tot 147.1s",
              "[soccer]   value bets: 74 total",
              "[soccer]   → 37 value bet alert(s) sent",
              "[tennis]   ⏱ dRest 0.1 tot 20.1s",
              "[tennis]   value bets: 5 total"]
    p = _journal(lignes, tmp_path)
    lots, _, _ = lire(p, "tennis")
    assert [l[1] for l in lots] == ["tennis"]
    assert lots[0][2] == pytest.approx(0.1)


def test_les_incidents_telegram_sont_comptes(tmp_path):
    lignes = _cycle(1, 10.0, 3, 5, 30.0) + [
        "Telegram 429 [chat=-100123] — pause 47s avant le prochain envoi",
        "Telegram cooldown 12s restant [chat=-100123] — message reporté",
        "Telegram non-200 (400) [chat=-100123]: Bad Request",
    ]
    p = _journal(lignes, tmp_path)
    _, _, inc = lire(p, None)
    assert inc == {"429": 1, "cooldown": 1, "non200": 1, "pause_max": 47}


def test_un_journal_sans_ligne_de_phases_le_dit(tmp_path, monkeypatch):
    p = _journal(["══ CYCLE 1 — 2026-09-04 12:00:00",
                  "[soccer]   value bets: 74 total"], tmp_path)
    monkeypatch.setattr("sys.argv", ["alert_cost", "--log", str(p)])
    with pytest.raises(SystemExit) as e:
        main()
    assert "Aucune ligne de phases" in str(e.value)


def test_un_journal_absent_le_dit(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.argv", ["alert_cost", "--log",
                                     str(tmp_path / "rien.log")])
    with pytest.raises(SystemExit) as e:
        main()
    assert "introuvable" in str(e.value)
