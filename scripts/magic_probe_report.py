#!/usr/bin/env python3
"""Inventaire des sondes MagicBetting — quels sports, quels marchés, où.

Lit ce que `tools/magicbetting-discover.user.js` a fait capturer, et répond aux
deux questions ouvertes du §18.6 :

  1. Quel endpoint rend l'OFFRE COMPLÈTE ? `gettopeventslist` en rend 27, et
     c'est structurel — c'est la liste des vedettes. On compare donc le nombre
     d'événements rendu par chaque endpoint observé.
  2. Quels sont les trois identifiants du TENNIS ? sportId, marché vainqueur,
     totaux de jeux.

⚠️ Ce script ne DÉCIDE rien, il montre des preuves. Le §10 est formel : un Id
de marché se confirme par égalité exacte, jamais par ressemblance de nom — les
noms arrivent en néerlandais et changent avec `langId`. Ce qu'on affiche, c'est
la structure : combien d'issues, est-ce que leurs noms sont ceux des deux
joueurs, y a-t-il une ligne `A`. C'est ça qui identifie un marché, pas son
libellé.

Usage :  .venv/bin/python scripts/magic_probe_report.py
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROBE_DIR = Path(os.getenv(
    "MAGIC_INGEST_DIR",
    str(Path(__file__).resolve().parent.parent / "data" / "magicbetting"),
)) / "probes"


# Le même extracteur que la production, et surtout PAS une copie. La première
# version le dupliquait ici : le rapport comptait donc correctement 25
# événements sur l'endpoint du balayage pendant que le serveur d'ingestion, qui
# exigeait un tableau nu, les refusait tous. Deux lectures divergentes de la
# même réponse, et le rapport donnait tort au serveur.
from src.scrapers.magicbetting import extract_events as _events  # noqa: E402


def main() -> int:
    if not PROBE_DIR.exists():
        print(f"Aucune sonde dans {PROBE_DIR}.\n"
              "Active tools/magicbetting-discover.user.js, recharge "
              "magicbetting.be et navigue (football complet, puis tennis).")
        return 1

    files = sorted(PROBE_DIR.glob("*.json"))
    if not files:
        print(f"{PROBE_DIR} est vide.")
        return 1

    print(f"=== {len(files)} sondes dans {PROBE_DIR} ===\n")

    # Un dépouillement par sport, alimenté par TOUTES les sondes : le tennis
    # peut arriver par un endpoint et le football par un autre.
    by_sport_events: dict[object, list[dict]] = defaultdict(list)
    rows = []

    for f in files:
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            print(f"  {f.name} : illisible ({e})")
            continue
        evs = _events(rec.get("clear"))
        for ev in evs:
            by_sport_events[ev.get("SId")].append(ev)
        rows.append((
            f.name,
            rec.get("url", "").split("?", 1)[0],
            rec.get("query", ""),
            len(evs),
            rec.get("note"),
        ))

    # ⚠️ L'URL s'affiche ENTIÈRE, origine comprise. La première version la
    # tronquait aux deux derniers segments : impossible alors de voir si un
    # appel venait de magicbetting.be ou de l'iframe sport-ak.bldiframe.com,
    # ni de repérer le préfixe de session de 36 caractères. Or c'est
    # exactement ce qu'il faut savoir pour comprendre POURQUOI il manque des
    # appels — un rapport qui cache la donnée qui explique le silence ne sert
    # à rien.
    for _, url, query, n, note in sorted(rows, key=lambda r: -r[3]):
        mark = "⚠ " + note if note else f"{n:,} événements"
        print(f"[{mark}]")
        print(f"    {url}")
        if query:
            print(f"    ? {query}")

    origins = Counter()
    for _, url, _, _, _ in rows:
        try:
            from urllib.parse import urlparse as _up
            origins[_up(url).netloc] += 1
        except Exception:                                           # noqa: BLE001
            pass
    print("\norigines observées :",
          ", ".join(f"{o} ({n})" for o, n in origins.most_common()) or "aucune")

    print("\n=== L'OFFRE COMPLÈTE ===")
    best = max(rows, key=lambda r: r[3]) if rows else None
    if best and best[3] > 40:
        print(f"Candidat : {'/'.join(best[1])} avec {best[3]:,} événements "
              f"(contre 27 pour gettopeventslist).")
        print(f"Paramètres : {best[2]}")
    else:
        print("Aucune sonde ne dépasse 40 événements. L'écran de l'offre "
              "complète n'a probablement pas été ouvert pendant la capture,\n"
              "ou bien le site la charge par morceaux (une compétition à la "
              "fois) — auquel cas il faut regarder les paramètres ci-dessus :\n"
              "un endpoint appelé plusieurs fois avec un id de compétition "
              "différent est le balayage à reproduire, comme pour Betano (§3).")

    print("\n=== SPORTS OBSERVÉS ===")
    try:
        from src.scrapers.magicbetting import MARKET_BY_STAKE_TYPE, SPORT_IDS
    except Exception:                                               # noqa: BLE001
        MARKET_BY_STAKE_TYPE, SPORT_IDS = {}, {}

    for sid, evs in sorted(by_sport_events.items(), key=lambda kv: -len(kv[1])):
        known = SPORT_IDS.get(sid)
        tag = f"→ {known}" if known else "→ NON RACCORDÉ"
        champs = Counter(str(e.get("CN") or "?") for e in evs).most_common(4)
        print(f"\nSId {sid}  {len(evs):>5} événements  {tag}")
        print(f"   compétitions : {', '.join(f'{c} ({n})' for c, n in champs)}")
        sample = evs[0]
        print(f"   exemple      : {sample.get('HT')} — {sample.get('AT')}")

        # Inventaire des marchés de CE sport. C'est ici que se lisent les
        # identifiants à raccorder.
        agg: dict[object, dict] = {}
        for e in evs:
            for st in e.get("StakeTypes") or []:
                if not isinstance(st, dict):
                    continue
                a = agg.setdefault(st.get("Id"), {
                    "name": st.get("N"), "events": 0, "stakes": 0,
                    "labels": Counter(), "lines": 0, "vals": [],
                })
                a["events"] += 1
                for s in st.get("Stakes") or []:
                    if not isinstance(s, dict):
                        continue
                    a["stakes"] += 1
                    a["labels"][str(s.get("N") or "?")] += 1
                    if s.get("A") is not None:
                        a["lines"] += 1
                        try:
                            a["vals"].append(float(s["A"]))
                        except (TypeError, ValueError):
                            pass

        def _span(a) -> str:
            """L'étendue des lignes, seule façon de savoir ce qu'on compte.

            Un total de JEUX au tennis court de 18,5 à 24,5 ; un total de SETS
            vaut 2,5. Les deux s'appellent « total », portent une ligne et
            affichent Au-dessus/Moins : rien ne les distingue SAUF la valeur.
            Comparer un total de sets au total de jeux de Pinnacle ne rendrait
            aucune erreur, seulement des cotes absurdes — c'est déjà le piège
            noté sur Circus."""
            v = a["vals"]
            return f"{min(v):g}…{max(v):g}" if v else ""

        print(f"   {'Id':>7} {'nom':28} {'évts':>5} {'cotes':>7} {'lignes':>13}  issues")
        for mid, a in sorted(agg.items(), key=lambda kv: -kv[1]["stakes"]):
            mapped = MARKET_BY_STAKE_TYPE.get(mid)
            flag = f"[{mapped.value}]" if mapped else ("[doublon vedette]"
                                                       if isinstance(mid, int) and mid < 0 else "")
            labels = ", ".join(l for l, _ in a["labels"].most_common(4))
            span = f"{a['lines']:>5} {_span(a):>7}" if a["lines"] else f"{'—':>13}"
            print(f"   {str(mid):>7} {str(a['name'])[:28]:28} {a['events']:>5} "
                  f"{a['stakes']:>7} {span}  {labels[:52]} {flag}")

        # Preuve structurelle, pas devinette de libellé.
        if not known:
            noms = {str(e.get("HT")) for e in evs} | {str(e.get("AT")) for e in evs}
            print("   --- indices pour le raccordement ---")
            for mid, a in agg.items():
                if isinstance(mid, int) and mid < 0:
                    continue
                lab = set(a["labels"])
                # ⚠️ La PRÉSENCE D'UNE LIGNE se teste EN PREMIER. Un handicap
                # nomme lui aussi ses issues d'après les équipes : la première
                # version classait donc « marché vainqueur » les Id 2 et 2532,
                # qui sont des handicaps. Ce qui sépare les deux n'est pas le
                # nom des issues mais le fait de porter un seuil.
                if a["lines"] and a["lines"] == a["stakes"]:
                    kind = "handicap" if lab <= noms else "totaux"
                    print(f"   Id {mid} : toutes les cotes portent une ligne "
                          f"({_span(a)}) → {kind} ({', '.join(sorted(lab)[:3])})")
                elif lab and lab <= noms:
                    print(f"   Id {mid} : issues = noms de joueur/équipe, aucune "
                          f"ligne → vainqueur, {len(lab & noms)} issues distinctes")

    print("\nRappel §10 : ne raccorder un Id qu'après l'avoir VU dans ce "
          "tableau. Un Id deviné rend un book muet sans lever d'erreur.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
