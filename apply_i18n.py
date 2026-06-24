#!/usr/bin/env python3
"""Branche la table d'alias pays dans matcher.normalize_team.

Ajoute l'import de COUNTRY_ALIASES + un lookup final dans normalize_team (FR/NL
-> canonique anglais), pour debloquer le matching international, sans toucher
aux scrapers. Sauvegarde, verifie les ancres, recompile (rollback sinon).

Usage : python3 apply_i18n.py   (depuis ~/Projet-Perso)
"""
from __future__ import annotations
import py_compile, shutil, sys, time
from pathlib import Path

TARGET = Path(sys.argv[1] if len(sys.argv) > 1 else "src/matcher.py")
PATCHES = [
    (
        "from .models import Event\n",
        "from .models import Event\nfrom .teams_i18n import COUNTRY_ALIASES\n",
    ),
    (
        '    s = re.sub(r"\\s+", " ", s).strip()\n'
        "    if cls:\n",
        '    s = re.sub(r"\\s+", " ", s).strip()\n'
        "    s = COUNTRY_ALIASES.get(s, s)        # FR/NL/variantes -> canonique EN (international)\n"
        "    if cls:\n",
    ),
]

if not TARGET.exists():
    sys.exit(f"introuvable : {TARGET}")
src = TARGET.read_text()
if "COUNTRY_ALIASES" in src:
    sys.exit("deja patche (COUNTRY_ALIASES present) — abandon")
for i,(old,_n) in enumerate(PATCHES,1):
    c=src.count(old)
    if c!=1: sys.exit(f"patch {i}: ancre trouvee {c}x (attendu 1) — abandon")
bak=TARGET.with_name(f"{TARGET.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
shutil.copy2(TARGET,bak)
out=src
for old,new in PATCHES: out=out.replace(old,new,1)
TARGET.write_text(out)
try:
    py_compile.compile(str(TARGET),doraise=True)
except py_compile.PyCompileError as e:
    shutil.copy2(bak,TARGET); sys.exit(f"compilation KO, restauration:\n{e}")
print(f"OK : {TARGET} patche (backup {bak.name})")
