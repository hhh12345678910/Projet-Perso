#!/usr/bin/env python3
"""Reconfigure le routage des canaux dans src/alerter.py :

  - Value >=35% : prematch uniquement (ajoute 'not is_live' au canal critique).
  - Surebets : partition premium [seuil_premium, seuil_critique) / critique [>=seuil_critique],
    et le critique devient prematch uniquement.
  - CLV : plus jamais copiee sur le canal critique.

Sans risque : sauvegarde d'abord, applique seulement si chaque ancre est trouvee
exactement une fois, verifie que le resultat compile, sinon restaure.

Usage : python3 apply_critical_routing.py   (ou un chemin en argument)
"""
from __future__ import annotations

import py_compile
import shutil
import sys
import time
from pathlib import Path

PATCHES: list[tuple[str, str]] = [
    # 1. value critique : prematch uniquement (pas de value live)
    (
        "        if cfg.effective_critical_chat_id and ev >= cfg.min_critical_ev_pct:\n",
        "        if not is_live and cfg.effective_critical_chat_id and ev >= cfg.min_critical_ev_pct:\n",
    ),
    # 2. CLV : on supprime la copie vers le canal critique
    (
        '        # Critical copy fires on its own chat/budget, independent of the CLV send.\n'
        '        if cfg.effective_critical_chat_id and clv_pct >= cfg.min_critical_clv_pct:\n'
        '            critical_text = "🚨 <b>CLV CRITIQUE</b>\\n" + format_clv_alert(\n'
        '                bet, clv_pct, current_pin_odd, mins_to_kickoff,\n'
        '                sport=sport, is_high=True, bankroll=cfg.bankroll,\n'
        '            )\n'
        '            delivered |= self._send(critical_text, chat_id=cfg.effective_critical_chat_id)\n'
        '        return delivered\n',
        '        # CLV n\'est jamais copiee sur le canal critique (choix utilisateur).\n'
        '        return delivered\n',
    ),
    # 3. surebet critique : prematch uniquement
    (
        '        if (\n'
        '            not sb.suspicious\n'
        '            and cfg.effective_critical_chat_id\n'
        '            and margin_pct >= cfg.min_critical_surebet_pct\n'
        '        ):\n',
        '        if (\n'
        '            not sb.suspicious\n'
        '            and not is_live\n'
        '            and cfg.effective_critical_chat_id\n'
        '            and margin_pct >= cfg.min_critical_surebet_pct\n'
        '        ):\n',
    ),
    # 4. surebet premium : borne haute = seuil critique (partition propre)
    (
        '        if (\n'
        '            not sb.suspicious\n'
        '            and not is_live\n'
        '            and cfg.effective_premium_chat_id\n'
        '            and margin_pct >= cfg.min_premium_surebet_pct\n'
        '        ):\n',
        '        if (\n'
        '            not sb.suspicious\n'
        '            and not is_live\n'
        '            and cfg.effective_premium_chat_id\n'
        '            and cfg.min_premium_surebet_pct <= margin_pct < cfg.min_critical_surebet_pct\n'
        '        ):\n',
    ),
]

ALREADY = "CLV n'est jamais copiee sur le canal critique"


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "src/alerter.py")
    if not target.exists():
        sys.exit(f"fichier introuvable : {target}")
    src = target.read_text()

    if ALREADY in src:
        sys.exit("le fichier semble deja patche (routage critique) — abandon")
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
    print("Routage mis a jour : value>=35% prematch (premium+critique), "
          "surebets partitionnes, CLV jamais sur critique.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
