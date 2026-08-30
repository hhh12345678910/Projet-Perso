"""Voir et installer les canaux persistes. Aucune commande Telegram ici.

La bascule vers le routage par canaux est un ACTE EXPLICITE : tant que la
table `channels` est vide, le daemon route exactement comme avant. Rien
n'est deduit des variables .env sans qu'on le demande.

    .venv/bin/python -m scripts.canaux --lister
    .venv/bin/python -m scripts.canaux --traduire     # montre, n'ecrit pas
    .venv/bin/python -m scripts.canaux --installer    # ecrit

`--installer` n'ecrase jamais un canal existant : un nom deja present est
saute et signale. Pour revenir en arriere, supprimer les lignes de
`channels` suffit — le daemon reprend le chemin historique au cycle suivant.
"""
from __future__ import annotations

import argparse

from src.alerter import TelegramConfig
from src.channels import charger, depuis_config, installer
from src.config import ScanConfig, load_env_file
from src.storage import Storage


def _resume(canal) -> str:
    lignes = []
    for r in canal.regles:
        bouts = []
        for nom, b, signe in (("EV", r.ev_min, ">"), ("EV", r.ev_max, "<"),
                              ("cote", r.odd_min, ">"), ("cote", r.odd_max, "<")):
            if b is not None:
                bouts.append(f"{nom} {signe}{'' if b.stricte else '='} {b.valeur:g}")
        if r.phase:
            bouts.append(r.phase)
        for c in r.criteres:
            bouts.append(f"{c.dimension} {'∈' if c.inclut else '∉'} "
                         f"{{{', '.join(sorted(c.valeurs))}}}")
        lignes.append("      · " + (" ET ".join(bouts) or "tout"))
    return "\n".join(lignes) or "      · (aucune regle — ne recoit rien)"


def _montrer(canaux, titre: str) -> None:
    print(f"\n── {titre} ({len(canaux)}) ──")
    for c in canaux:
        etat = "actif" if c.actif else "COUPE"
        excl = ", exclusif" if c.exclusif else ""
        print(f"  {c.nom}  chat={c.chat_id}  prio={c.priorite}  {etat}{excl}")
        print(_resume(c))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--lister", action="store_true", help="les canaux en base")
    g.add_argument("--traduire", action="store_true",
                   help="ce que .env donnerait, sans rien ecrire")
    g.add_argument("--installer", action="store_true",
                   help="ecrit la traduction de .env en base")
    a = p.parse_args()

    print(f"env : {load_env_file('.env')} cles chargees depuis .env")
    st = Storage(ScanConfig().db_path)

    if a.lister:
        canaux = charger(st)
        _montrer(canaux, "canaux en base")
        if not canaux:
            print("  (aucun — le daemon route selon .env, comme avant)")
        return 0

    cfg = TelegramConfig.from_env()
    if cfg is None:
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID absents.")
        return 2
    traduits = depuis_config(cfg)

    if a.traduire:
        _montrer(traduits, "traduction de .env (RIEN n'est ecrit)")
        return 0

    ecrits = installer(st, traduits)
    print(f"\ncanaux ecrits : {', '.join(ecrits) or 'aucun'}")
    _montrer(charger(st), "canaux en base apres installation")
    print("\n⚠️ Le daemon bascule sur le routage par canaux au prochain cycle.")
    print("   Pour revenir : supprimer les lignes de `channels`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
