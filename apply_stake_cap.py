#!/usr/bin/env python3
"""Plafonne la mise (quart de Kelly) dans src/alerter.py.

La mise affichee/enregistree est bornee a TELEGRAM_MAX_STAKE_PCT (defaut 3.0 %),
pour eviter les mises gonflees sur cotes basses. Cap applique a 3 endroits :
  - affichage value bet,
  - ligne Excel ecrite par le bouton Jouer,
  - affichage CLV.

Sans risque : sauvegarde, n'applique que si chaque ancre est unique, verifie la
compilation, restaure sinon.

Usage : python3 apply_stake_cap.py   (ou un chemin en argument)
"""
from __future__ import annotations

import py_compile
import shutil
import sys
import time
from pathlib import Path

CONST = (
    "# Plafond de mise (% bankroll) applique par-dessus le quart de Kelly.\n"
    "_MAX_STAKE_PCT = float(os.getenv(\"TELEGRAM_MAX_STAKE_PCT\", \"3.0\"))\n\n\n"
)

PATCHES: list[tuple[str, str]] = [
    # 1. la constante, juste avant la section d'affichage local
    (
        "# Belgium-friendly display: dates relative to today, kickoff in local time.\n"
        "_LOCAL_TZ = ZoneInfo(\"Europe/Brussels\") if ZoneInfo is not None else timezone.utc\n",
        CONST
        + "# Belgium-friendly display: dates relative to today, kickoff in local time.\n"
        "_LOCAL_TZ = ZoneInfo(\"Europe/Brussels\") if ZoneInfo is not None else timezone.utc\n",
    ),
    # 2. affichage value bet
    (
        '        f"Mise conseillée : {bet.kelly_stake_pct:.2f}% → "\n'
        '        f"{bet.kelly_stake_pct / 100.0 * bankroll:.2f}€ (sur {bankroll:.0f}€)"\n',
        '        f"Mise conseillée : {min(bet.kelly_stake_pct, _MAX_STAKE_PCT):.2f}% → "\n'
        '        f"{min(bet.kelly_stake_pct, _MAX_STAKE_PCT) / 100.0 * bankroll:.2f}€ (sur {bankroll:.0f}€)"\n',
    ),
    # 3. ligne Excel (payload du bouton Jouer)
    (
        '        "mise": round(bet.kelly_stake_pct / 100.0 * bankroll, 2),\n',
        '        "mise": round(min(bet.kelly_stake_pct, _MAX_STAKE_PCT) / 100.0 * bankroll, 2),\n',
    ),
    # 4. affichage CLV
    (
        '        stake_eur = kelly_pct / 100.0 * bankroll\n'
        '        stake_line = f"\\nMise conseillée : {kelly_pct:.2f}% → {stake_eur:.2f}€ (sur {bankroll:.0f}€)"\n',
        '        kelly_pct = min(kelly_pct, _MAX_STAKE_PCT)\n'
        '        stake_eur = kelly_pct / 100.0 * bankroll\n'
        '        stake_line = f"\\nMise conseillée : {kelly_pct:.2f}% → {stake_eur:.2f}€ (sur {bankroll:.0f}€)"\n',
    ),
]

ALREADY = "_MAX_STAKE_PCT"


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "src/alerter.py")
    if not target.exists():
        sys.exit(f"fichier introuvable : {target}")
    src = target.read_text()

    if ALREADY in src:
        sys.exit("le fichier semble deja patche (_MAX_STAKE_PCT present) — abandon")
    for i, (old, _new) in enumerate(PATCHES, 1):
        n = src.count(old)
        if n != 1:
            sys.exit(f"patch {i} : ancre trouvee {n} fois (attendu 1) — abandon, rien modifie")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = target.with_name(f"{target.name}.bak-{stamp}")
    shutil.copy2(target, bak)

    out = src
    for old, new in PATCHES:
        out = out.replace(old, new, 1)
    target.write_text(out)

    try:
        py_compile.compile(str(target), doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copy2(bak, target)
        sys.exit(f"erreur de compilation, restauration de la sauvegarde :\n{e}")

    print(f"OK : {target} patche. Sauvegarde -> {bak}")
    print("Mise plafonnee a TELEGRAM_MAX_STAKE_PCT (defaut 3%).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
