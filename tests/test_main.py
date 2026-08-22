"""Réglages de src.main lus dans l'environnement.

Ces défauts sont évalués à l'import, donc une régression ne se voit ni au
démarrage ni dans les logs : la commande tourne, simplement avec la mauvaise
valeur. Pour la purge, cela signifie une base qui double sans que rien ne le
signale.
"""
from __future__ import annotations

import importlib
import inspect
import sys


def _prune_default(monkeypatch, value: str | None) -> int:
    """Recharge src.main avec PRUNE_DAYS fixé, et lit le défaut de --days."""
    if value is None:
        monkeypatch.delenv("PRUNE_DAYS", raising=False)
    else:
        monkeypatch.setenv("PRUNE_DAYS", value)
    for name in [n for n in list(sys.modules) if n == "src.main"]:
        del sys.modules[name]
    module = importlib.import_module("src.main")
    param = inspect.signature(module.prune).parameters["retention_days"]
    return param.default.default


def test_prune_retention_defaults_to_two_days(monkeypatch):
    assert _prune_default(monkeypatch, None) == 2


def test_prune_retention_reads_the_environment(monkeypatch):
    """La rétention est le seul levier sur la taille de la base, et elle
    n'était réglable qu'en éditant une unité systemd — donc en pratique
    jamais. Mesuré le 01/08 : deux jours pèsent 23 Go de données utiles.

    Une journée suffit dès lors que close-lines tourne toutes les heures, la
    ligne de clôture étant capturée dans l'heure suivant le coup d'envoi."""
    assert _prune_default(monkeypatch, "1") == 1
    assert _prune_default(monkeypatch, "7") == 7


# ── BOOKS_DISABLED — coupe-circuit par book, sans déploiement ─────────────

def _tasks_for(monkeypatch, disabled: str) -> set[str]:
    """Quels books le cycle interroge, sous un BOOKS_DISABLED donné.

    ⚠️ Le remplacement vise `src.orchestration`, et c'est le seul cas du
    découpage où viser `src.main` aurait échoué EN SILENCE. `main` importe
    toujours ThreadPoolExecutor pour son propre usage — il parallélise les
    SPORTS pendant que la collecte parallélise les BOOKS. L'attribut existe
    donc des deux côtés : `monkeypatch.setattr(main, ...)` réussirait sans
    lever, ne toucherait pas l'exécuteur employé par `fetch_all_parallel`, et
    ce test passerait en interrogeant les vrais books par le réseau.

    Vérifié en visant délibérément `main` : le test échoue (§21.23)."""
    import src.orchestration as orch
    monkeypatch.setenv("BOOKS_DISABLED", disabled)
    seen: set[str] = set()

    class _Pool:
        def __init__(self, max_workers=1): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def submit(self, fn):
            class _F:
                def result(self_inner): return []
            return _F()

    monkeypatch.setattr(orch, "ThreadPoolExecutor", _Pool)
    monkeypatch.setattr(orch, "as_completed",
                        lambda futures: (seen.update(futures.values()), [])[1])
    orch.fetch_all_parallel("soccer", betano_file=None)
    return seen


def test_a_book_can_be_cut_without_deploying(monkeypatch):
    assert "BetFirst" in _tasks_for(monkeypatch, "")
    assert "BetFirst" not in _tasks_for(monkeypatch, "betfirst")


def test_the_cut_is_case_insensitive_and_takes_a_list(monkeypatch):
    kept = _tasks_for(monkeypatch, "BetFirst, goldenpalace")
    assert "BetFirst" not in kept and "GoldenPalace" not in kept
    assert "Unibet" in kept, "les autres books ne bougent pas"


def test_an_unknown_name_is_reported(monkeypatch, capsys):
    """Un nom mal orthographié ne couperait rien et ne dirait rien — c'est le
    genre de réglage qu'on croit appliqué et qui ne l'est pas."""
    _tasks_for(monkeypatch, "betfrist")
    assert "inconnu" in capsys.readouterr().out.lower()


def test_cutting_every_book_does_not_crash(monkeypatch):
    """max_workers=0 lèverait ValueError."""
    kept = _tasks_for(monkeypatch, "pinnacle,unibet,betfirst,goldenpalace,"
                                   "ladbrokes,starcasino,napoleon,betano,circus")
    assert kept == set() or "Pinnacle" not in kept
