"""Quels codes de mi-temps chaque soft expose-t-il ? — §21.14

Pourquoi cet outil existe
-------------------------
Les marchés de mi-temps sont en service (§21.14) mais **un seul soft est
mappé** : `OUH1` chez Betano. Les autres books les proposent presque
certainement — Circus nomme des `1st-half-*` dans ses exclusions — mais leurs
codes exacts n'ont jamais été relevés, et le §21.13 a montré ce que coûte une
croyance non vérifiée sur ce sujet précis.

**Deviner est interdit ici.** `circus.py` porte l'avertissement en toutes
lettres : le comparatif se fait par égalité exacte, et
`first-set-total-games-over-under-OverUnder` contient `total-games-over-under`
en sous-chaîne tout en ne mesurant qu'un set. Un code mappé par ressemblance
fabriquerait la panne silencieuse du §21.3.

Cette sonde ne mappe RIEN et ne modifie RIEN. Elle lit les dumps déjà sur le
disque, relève les identifiants de marché non reconnus **avec leur libellé**,
et signale ceux dont le libellé évoque une mi-temps. Le libellé est ce qui
transforme une ressemblance en preuve : c'est lui qui permet de décider, puis
d'ajouter le code par égalité exacte.

    .venv/bin/python -m scripts.discover_half_time

Sortie à me renvoyer telle quelle pour que le mappage se fasse sur pièces.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path

# Ce qui, dans un libellé, désigne une PREMIÈRE mi-temps. Volontairement
# large : cette sonde propose à un humain, elle ne décide pas. Un faux positif
# se voit d'un coup d'œil ; un faux négatif laisse un marché dans l'ombre.
_INDICES = re.compile(
    r"(1(ere|ère|er|st)?[\s-]*(mi[\s-]?temps|half|periode|période)"
    r"|mi[\s-]?temps|half[\s-]?time|first[\s-]*half|1e?\s*ht|\bh1\b|\bht\b"
    r"|eerste[\s-]*helft)",
    re.IGNORECASE,
)

# Ce qui trahit une SECONDE mi-temps ou un marché de période plus fine : à
# écarter du signalement, sans quoi la liste se remplit de faux candidats.
_CONTRE = re.compile(r"(2(eme|ème|nd)?[\s-]*(mi[\s-]?temps|half)|second[\s-]*half"
                     r"|tweede[\s-]*helft)", re.IGNORECASE)


def _candidat(code: str, libelle: str) -> bool:
    """Teste le code ET le libellé.

    Les BetType de Circus sont eux-mêmes descriptifs (`1st-half-...`), et le
    champ de libellé exact n'est pas garanti d'un book à l'autre. Ne regarder
    que le libellé ferait échouer la sonde EN SILENCE là où il est absent —
    le mode de défaillance dominant du projet (§11).
    """
    texte = f"{code} {libelle}"
    return bool(_INDICES.search(texte)) and not _CONTRE.search(texte)


def _charger(chemin: Path):
    try:
        return json.loads(chemin.read_text())
    except Exception as e:
        print(f"    {chemin.name} : illisible ({e})")
        return None


def _marche(entrees, titre: str, connus: set) -> None:
    """Affiche les identifiants relevés, candidats d'abord."""
    if not entrees:
        print(f"  {titre} : aucun marché lu.")
        return
    candidats = {k: v for k, v in entrees.items() if _candidat(str(k[0]), k[1])}
    sans_libelle = sum(1 for (_c, l) in entrees if not l.strip())
    autres = {k: v for k, v in entrees.items() if k not in candidats}
    print(f"  {titre} : {len(entrees)} identifiants distincts, "
          f"{len(candidats)} candidats mi-temps")
    for (code, libelle), n in sorted(candidats.items(), key=lambda kv: -kv[1]):
        deja = "  ← DÉJÀ MAPPÉ" if code in connus else ""
        # JAMAIS tronqué, et en repr() : le mappage se fait par égalité
        # exacte, donc un code coupé à la largeur d'une colonne ne matcherait
        # rien — en silence. La première version coupait à 28 caractères, ce
        # qui a masqué la fin de `half-time-totals-over-under-…` : un code
        # illisible vaut un code absent.
        print(f"    ★ {n:>5d} ×  {code!r}{deja}")
        print(f"              {libelle!r}")
    if not candidats:
        print("    (aucun code ni libellé n'évoque une mi-temps)")
    if sans_libelle == len(entrees) and entrees:
        print(f"    ⚠️ AUCUN libellé lu sur {len(entrees)} marchés : le champ de "
              "libellé\n       de ce book porte un autre nom. Le repérage ne "
              "s'appuie ici que\n       sur le code — à compléter avant de "
              "conclure à une absence.")
    # Les non-candidats servent à juger si la sonde a vraiment vu le payload.
    apercu = sorted(autres.items(), key=lambda kv: -kv[1])[:6]
    if apercu:
        print(f"    reste, pour contrôle : "
              + ", ".join(f"{c}×{n}" for (c, _l), n in apercu))


def circus(racine: Path) -> None:
    from src.scrapers.circus import _MARKETS
    d = Path(os.getenv("CIRCUS_INGEST_DIR", racine / "data" / "circus"))
    print(f"\n--- Circus  ({d}) ---")
    if not d.is_dir():
        print("  répertoire absent — le pont navigateur n'a rien déposé.")
        return
    # Traversée générique, comme pour Betano. La version précédente supposait
    # `Leagues → Events → Markets` à la racine et n'a RIEN lu sur le dump réel,
    # là où le répertoire existait bel et bien : le pont enveloppe visiblement
    # la réponse. Une sonde de découverte qui suppose la forme de ce qu'elle
    # découvre ne découvre rien — elle affiche « aucun marché », ce qui se lit
    # comme « ce book n'en a pas ».
    vus: Counter = Counter()
    fichiers = 0
    for f in sorted(d.glob("*.json")):
        data = _charger(f)
        if data is None:
            continue
        fichiers += 1
        piles = [data]
        while piles:
            n = piles.pop()
            if isinstance(n, dict):
                if isinstance(n.get("Markets"), list):
                    for m in n["Markets"]:
                        if isinstance(m, dict):
                            vus[(str(m.get("BetType") or "?"),
                                 str(m.get("Name") or m.get("MarketName")
                                     or m.get("N") or ""))] += 1
                piles.extend(v for v in n.values() if isinstance(v, (dict, list)))
            elif isinstance(n, list):
                piles.extend(x for x in n if isinstance(x, (dict, list)))
    if fichiers and not vus:
        print(f"  ⚠️ {fichiers} fichier(s) lu(s), AUCUNE clé `Markets` trouvée.\n"
              "     Le dump a une autre forme — à inspecter avant de conclure\n"
              "     que ce book n'expose pas de mi-temps :\n"
              "       .venv/bin/python -m src.main inspect-json <fichier> --path .")
    _marche(vus, "BetType", set(_MARKETS))


def betano(racine: Path) -> None:
    from src.scrapers.betano import _PREMATCH_MARKET_BY_TYPE
    d = racine / "data" / "prematch"
    print(f"\n--- Betano  ({d}) ---")
    if not d.is_dir():
        print("  répertoire absent.")
        return
    vus: Counter = Counter()
    for f in sorted(d.glob("*.json")):
        data = _charger(f)
        if data is None:
            continue
        piles = [data]
        while piles:
            n = piles.pop()
            if isinstance(n, dict):
                if "markets" in n and isinstance(n.get("markets"), list):
                    for m in n["markets"]:
                        if isinstance(m, dict):
                            vus[(str(m.get("type") or "?"),
                                 str(m.get("name") or ""))] += 1
                piles.extend(v for v in n.values() if isinstance(v, (dict, list)))
            elif isinstance(n, list):
                piles.extend(x for x in n if isinstance(x, (dict, list)))
    _marche(vus, "type", set(_PREMATCH_MARKET_BY_TYPE))


def magicbetting(racine: Path) -> None:
    from src.scrapers.magicbetting import MARKET_BY_STAKE_TYPE
    d = Path(os.getenv("MAGIC_INGEST_DIR", racine / "data" / "magicbetting"))
    print(f"\n--- MagicBetting  ({d}) ---")
    if not d.is_dir():
        print("  répertoire absent.")
        return
    connus = {i for m in MARKET_BY_STAKE_TYPE.values() for i in m}
    vus: Counter = Counter()
    for f in sorted(d.glob("*.json")):
        data = _charger(f)
        if data is None:
            continue
        piles = [data]
        while piles:
            n = piles.pop()
            if isinstance(n, dict):
                if "StakeTypes" in n and isinstance(n.get("StakeTypes"), list):
                    for st in n["StakeTypes"]:
                        if isinstance(st, dict):
                            vus[(st.get("Id"), str(st.get("N") or ""))] += 1
                piles.extend(v for v in n.values() if isinstance(v, (dict, list)))
            elif isinstance(n, list):
                piles.extend(x for x in n if isinstance(x, (dict, list)))
    _marche(vus, "StakeType Id", connus)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--racine", default=".", help="Racine du projet.")
    args = ap.parse_args()
    racine = Path(args.racine).resolve()

    print("Codes de marché relevés dans les dumps — AUCUN mappage effectué.")
    print("Les ★ sont des candidats mi-temps, à confirmer par leur libellé.")
    for f in (circus, betano, magicbetting):
        try:
            f(racine)
        except Exception as e:
            print(f"\n--- {f.__name__} : sonde en échec ({type(e).__name__}: {e})")

    print("\n" + "=" * 64)
    print("Les books sans dump sur disque (Unibet et la famille Kambi,")
    print("GoldenPalace, StarCasino, Ladbrokes, Napoleon, Betcenter) tirent")
    print("leurs cotes d'une API en direct : leurs codes se relèvent en")
    print("interrogeant le book, pas ici.")
    print("\n⚠️ Ne mapper un code que sur son LIBELLÉ, par égalité exacte.")
    print("   `first-set-total-games-over-under` contient")
    print("   `total-games-over-under` en sous-chaîne et ne mesure qu'un set.")


if __name__ == "__main__":
    main()
