"""Quelle couverture de ligues une source de résultats football doit-elle avoir ?

Pourquoi cette sonde existe
---------------------------
Le §21.9 pt 1 dit que « la couverture est le critère qui élimine les API
gratuites généralistes » — mais ce critère n'a jamais été CHIFFRÉ. On choisit
donc entre trois voies (nouveau compte API-Sports, RapidAPI, cinquième pont
navigateur) sur une intuition : « les détections portent sur des centaines de
ligues ». Cette sonde remplace l'intuition par un nombre.

Elle ne regarde AUCUNE source. Elle mesure la DEMANDE — ce que notre propre
flux de détections réclame — parce que c'est le seul côté qu'on puisse mesurer
sans clé, sans quota et sans réseau. Une source se juge ensuite contre ce
chiffre, pas contre une impression.

    .venv/bin/python -m scripts.scores_coverage
    .venv/bin/python -m scripts.scores_coverage --min-ev 5 --since 2026-07-15
    .venv/bin/python -m scripts.scores_coverage --top 40

La question à laquelle elle répond
----------------------------------
« Une source limitée aux grands championnats réglerait quelle PART de nos
détections football ? » Si la réponse est 80 %, les API gratuites redeviennent
candidates et le pont navigateur est du travail inutile. Si c'est 25 %, elles
sont éliminées et le §21.9 a raison — mais alors on le SAIT.

⚠️ Le biais qu'il faut avoir en tête. On mesure les ligues où le système
détecte AUJOURD'HUI, c'est-à-dire là où Pinnacle price et où un soft suit. Ce
n'est pas l'univers des ligues possibles ; c'est exactement la population dont
le §20.4 attend un P&L, donc c'est la bonne pour cette décision — mais elle ne
dit rien de ce qu'une meilleure couverture ferait GAGNER.

⚠️ La part du top 5 se lit comme un PLAFOND, jamais comme une promesse. Une
source « qui couvre les grands championnats » n'est pas garantie de couvrir
les cinq, ni toute leur saison. Le chiffre dit ce qu'on perdrait au mieux, pas
ce qu'on obtiendrait.
"""
from __future__ import annotations

import argparse
import sqlite3
from collections import Counter

from src.config import ScanConfig
from src.leagues import TOP5, categorize

# Les paliers de concentration. On veut savoir combien de ligues il faut
# couvrir pour atteindre chacun — c'est la forme de la queue qui décide, pas
# le nombre total de ligues. Cent ligues dont cinq portent 90 % du volume et
# cent ligues qui se partagent le volume à parts égales appellent deux
# décisions opposées.
_PALIERS = (50.0, 80.0, 90.0, 95.0, 99.0)

# Une ligue vue au plus deux fois sur toute la fenêtre est de la queue longue :
# c'est précisément ce qu'une source généraliste laisse tomber, et ce qu'un
# pont navigateur vers un site public ramasse sans effort.
_SEUIL_QUEUE = 2


def _detections(db_path: str, *, since: str, until: str,
                min_ev: float) -> tuple[Counter, dict[str, int]]:
    """Les détections football, comptées par ligue BRUTE.

    On part de `value_bets` et non de `bet_features` : la table de features
    n'a été remplie qu'à partir du 04/08 (§13.6), et fonder la décision
    dessus amputerait silencieusement la fenêtre de juillet — le mode de
    défaillance dominant du projet (§13.12).

    La ligue vient de `events.league`, le nom BRUT tel que Pinnacle l'écrit.
    C'est le parti du §leagues : la catégorie est une vue dérivée, révisable ;
    le nom, lui, est ce qu'une source devra reconnaître.

    Renvoie aussi des compteurs de rejet. Sans eux, « nos détections tiennent
    en peu de ligues » et « la moitié de nos détections n'a pas de ligue en
    base » donnent le même tableau rassurant.
    """
    compteurs = {"retenues": 0, "sans_event": 0, "ligue_vide": 0}
    par_ligue: Counter = Counter()

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        # LEFT JOIN et non JOIN : une détection dont la ligne `events` manque
        # doit se COMPTER comme un trou, pas disparaître du dénominateur. Le
        # §19.7 est exactement ce trou-là.
        sql = ("SELECT e.sport AS sport, e.league AS league "
               "FROM value_bets vb LEFT JOIN events e ON e.event_key = vb.event_key "
               "WHERE vb.ev_pct >= ?")
        params: list = [min_ev]
        if since:
            sql += " AND vb.detected_at >= ?"
            params.append(since)
        if until:
            sql += " AND vb.detected_at <= ?"
            params.append(until + "T23:59:59")

        for r in con.execute(sql, params):
            sport = (r["sport"] or "").lower()
            # Un sport nul veut dire ligne `events` absente : on ne peut pas
            # savoir si c'était du football, donc on le déclare au lieu de le
            # ranger d'un côté ou de l'autre.
            if not sport:
                compteurs["sans_event"] += 1
                continue
            if sport != "soccer":
                continue
            ligue = (r["league"] or "").strip()
            if not ligue:
                compteurs["ligue_vide"] += 1
                continue
            par_ligue[ligue] += 1
            compteurs["retenues"] += 1
    finally:
        con.close()

    return par_ligue, compteurs


def _paliers(par_ligue: Counter) -> dict[float, int]:
    """Combien de ligues faut-il couvrir pour atteindre chaque palier ?"""
    total = sum(par_ligue.values())
    atteints: dict[float, int] = {}
    if not total:
        return atteints
    cumul = 0
    restants = list(_PALIERS)
    for rang, (_, n) in enumerate(par_ligue.most_common(), start=1):
        cumul += n
        part = 100.0 * cumul / total
        while restants and part >= restants[0]:
            atteints[restants.pop(0)] = rang
    return atteints


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-ev", type=float, default=0.0,
                    help="EV minimale des détections comptées (défaut 0 = toutes). "
                         "Poser 5 pour ne juger que sur le flux réellement détecté.")
    ap.add_argument("--since", default="", help="Date de détection minimale (AAAA-MM-JJ).")
    ap.add_argument("--until", default="", help="Date de détection maximale, incluse.")
    ap.add_argument("--top", type=int, default=25,
                    help="Nombre de ligues détaillées dans le tableau (défaut 25).")
    ap.add_argument("--db", default=ScanConfig().db_path)
    args = ap.parse_args()

    par_ligue, compteurs = _detections(
        args.db, since=args.since, until=args.until, min_ev=args.min_ev)
    total = sum(par_ligue.values())

    if not total:
        print("Aucune détection football sur cette fenêtre.")
        if compteurs["sans_event"] or compteurs["ligue_vide"]:
            print(f"⚠️ {compteurs['sans_event']} détections sans ligne `events`, "
                  f"{compteurs['ligue_vide']} avec une ligue vide — "
                  "ce n'est pas une absence de football, c'est un trou de données.")
        return

    fenetre = f"{args.since or 'origine'} → {args.until or 'aujourd’hui'}"
    print(f"\nDétections football : {total} sur {len(par_ligue)} ligues distinctes "
          f"({fenetre}, EV ≥ {args.min_ev:g} %)")

    # ------------------------------------------------------ concentration ----
    atteints = _paliers(par_ligue)
    print("\nConcentration — combien de ligues pour couvrir…")
    for p in _PALIERS:
        rang = atteints.get(p)
        if rang is None:
            continue
        print(f"  {p:5.0f} % des détections  →  {rang:4d} ligues "
              f"({100.0 * rang / len(par_ligue):.0f} % des ligues)")

    # ---------------------------------------------------------- catégories ----
    par_cat: Counter = Counter()
    for ligue, n in par_ligue.items():
        par_cat[categorize(ligue)] += n

    print(f"\nPar catégorie de ligue ({len(par_ligue)} ligues) :")
    print(f"  {'catégorie':<16s} {'détections':>11s} {'part':>7s} {'ligues':>7s}")
    print("  " + "-" * 44)
    ligues_par_cat: Counter = Counter(categorize(lg) for lg in par_ligue)
    for cat, n in par_cat.most_common():
        print(f"  {cat:<16s} {n:11d} {100.0 * n / total:6.1f} % {ligues_par_cat[cat]:7d}")

    # ----------------------------------------------------------- la queue ----
    queue = [lg for lg, n in par_ligue.items() if n <= _SEUIL_QUEUE]
    n_queue = sum(par_ligue[lg] for lg in queue)

    print(f"\nLes {args.top} premières ligues :")
    print(f"  {'ligue':<44s} {'n':>6s} {'part':>7s} {'cumul':>7s}")
    print("  " + "-" * 68)
    cumul = 0
    for ligue, n in par_ligue.most_common(args.top):
        cumul += n
        print(f"  {ligue:<44.44s} {n:6d} {100.0 * n / total:6.1f} % "
              f"{100.0 * cumul / total:6.1f} %")

    # ------------------------------------------------------------ verdict ----
    n_top5 = par_cat.get(TOP5, 0)
    print("\n" + "=" * 72)
    print("CE QUE ÇA DIT DU CHOIX DE SOURCE (§21.9 pt 1)")
    print("=" * 72)
    print(f"  Une source limitée aux CINQ GRANDS CHAMPIONNATS réglerait au mieux "
          f"{100.0 * n_top5 / total:.1f} %\n  des détections football "
          f"({n_top5} sur {total}). Les {total - n_top5} autres "
          f"({100.0 * (total - n_top5) / total:.1f} %)\n"
          f"  se répartissent sur {len(par_ligue) - ligues_par_cat[TOP5]} ligues.")
    print(f"\n  Queue longue : {len(queue)} ligues n'apparaissent qu'une ou deux fois "
          f"et ne pèsent\n  que {100.0 * n_queue / total:.1f} % des détections — "
          "mais ce sont elles qui décident entre une API\n"
          "  à catalogue et un pont vers un site public (couverture illimitée).")

    if compteurs["sans_event"] or compteurs["ligue_vide"]:
        print(f"\n⚠️ Non comptées : {compteurs['sans_event']} détections sans ligne "
              f"`events` (§19.7), {compteurs['ligue_vide']} dont la ligue est vide.\n"
              "   Elles ne sont ni football ni autre chose — elles sont invisibles, "
              "et\n   ce tableau les ignore. Les régler d'abord si elles pèsent.")

    print("\n⚠️ La part du top 5 est un PLAFOND. Une source « qui couvre les grands\n"
          "   championnats » ne les couvre pas forcément tous, ni toute la saison.\n"
          "   Et ce tableau mesure ce que le système détecte AUJOURD'HUI, pas ce\n"
          "   qu'une meilleure couverture ferait gagner.\n")


if __name__ == "__main__":
    main()
