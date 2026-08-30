"""`src/routing.py` ne doit pouvoir atteindre ni le moteur, ni Telegram.

Le routage décide où part un pari. S'il pouvait atteindre `detection` ou
`reference`, plus rien ne garantirait structurellement qu'un filtre
n'influence pas une EV — il faudrait le relire à chaque modification. En
n'important rien du projet, la garantie devient vérifiable.

Deux mesures, parce qu'elles ne disent pas la même chose :

  * `test_le_module_n_importe_rien_du_projet` lit le SOURCE (AST). Il
    attrape un import ajouté même s'il est inoffensif à l'exécution ;
  * `test_importer_routing_ne_charge_ni_le_moteur_ni_telegram` lance un
    interpréteur NEUF et regarde `sys.modules`. Il attrape ce que l'AST ne
    voit pas : un import indirect, un `importlib`, un import dans une
    fonction. C'est celui qui prouve vraiment quelque chose.

Sans le sous-processus, le test serait faux dès sa première exécution :
pytest a déjà chargé `src.alerter` pour les autres fichiers de tests, donc
`sys.modules` du processus courant les contient tous.
"""
from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

RACINE = pathlib.Path(__file__).resolve().parent.parent
MODULE = RACINE / "src" / "routing.py"

# Le moteur de valorisation, la persistance, et tout ce qui parle au dehors.
INTERDITS = (
    "src.main", "src.alerter", "src.detection", "src.orchestration",
    "src.reference", "src.storage", "src.config", "src.models",
    "src.late_markets", "src.live_value", "src.clv",
    "httpx", "requests", "sqlite3", "telegram",
)


def _imports_du_source() -> set[str]:
    arbre = ast.parse(MODULE.read_text(encoding="utf-8"))
    noms: set[str] = set()
    for n in ast.walk(arbre):
        if isinstance(n, ast.Import):
            noms.update(a.name for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            noms.add(n.module)
    return noms


def test_le_module_n_importe_rien_du_projet():
    """Stdlib uniquement. `models` compris : le module lit `.value` par
    getattr précisément pour ne pas ouvrir cette porte."""
    interdits = {"__future__", "dataclasses", "typing"}
    inattendus = _imports_du_source() - interdits
    assert not inattendus, f"imports inattendus dans routing.py : {sorted(inattendus)}"


def test_aucun_import_interdit_dans_le_source():
    trouves = {i for i in _imports_du_source()
               if any(i == x or i.startswith(x + ".") for x in INTERDITS)}
    assert not trouves, f"routing.py importe : {sorted(trouves)}"


def test_importer_routing_ne_charge_ni_le_moteur_ni_telegram():
    """Interpréteur neuf : la seule mesure qui vaille. Un import indirect ou
    différé apparaîtrait ici et nulle part ailleurs."""
    code = (
        "import sys\n"
        "import src.routing\n"
        f"interdits = {INTERDITS!r}\n"
        "charges = [m for m in sys.modules if m in interdits]\n"
        "print('|'.join(sorted(charges)))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=RACINE,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    charges = [m for m in r.stdout.strip().split("|") if m]
    assert not charges, f"importer src.routing charge aussi : {charges}"


def test_le_module_ne_touche_ni_disque_ni_reseau():
    """Aucun appel de lecture/écriture ni d'ouverture de connexion dans le
    source. Grossier, mais il attraperait un `open()` glissé plus tard."""
    source = MODULE.read_text(encoding="utf-8")
    arbre = ast.parse(source)
    appels = {n.func.id for n in ast.walk(arbre)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    for interdit in ("open", "exec", "eval", "__import__", "compile"):
        assert interdit not in appels, f"routing.py appelle {interdit}()"


def test_le_sous_processus_verrait_vraiment_une_violation():
    """Falsification permanente du test précédent : le même contrôle sur un
    module qui, lui, importe l'alerter, doit échouer. Sans ça, rien ne dit
    que la mesure n'est pas systématiquement vide."""
    code = (
        "import sys\n"
        "import src.alerter\n"
        f"interdits = {INTERDITS!r}\n"
        "charges = [m for m in sys.modules if m in interdits]\n"
        "print('|'.join(sorted(charges)))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], cwd=RACINE,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    charges = [m for m in r.stdout.strip().split("|") if m]
    assert charges, ("le contrôle ne détecte rien même sur src.alerter — "
                     "il ne prouve donc rien sur src.routing")
