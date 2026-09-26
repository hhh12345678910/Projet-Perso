#!/usr/bin/env python3
"""Tes paris JOUÉS sans résultat : combien, et POURQUOI chacun.

POURQUOI CETTE SONDE
--------------------
Le dashboard dit « 100 réglés sur 200 ». Tu sais, toi, que tu n'as pas 100
paris en cours chez les books. L'écart est fait de paris dont le match est
fini mais dont le résultat n'est jamais arrivé en base — et chacun manque
pour UNE raison précise, qui appelle UN geste précis :

* le match n'est pas encore joué, ou vient de finir ........ rien à faire ;
* football d'aujourd'hui ................................... le pont ne prend
  une journée que le LENDEMAIN ;
* journée jamais récupérée par le pont ..................... cliquer, ou
  élargir `SCORES_BRIDGE_DAYS` si la journée est sortie de sa fenêtre ;
* journée refusée par la source (hors abonnement) .......... définitif ;
* journée capturée trop tôt ................................. sera reprise au
  prochain clic ;
* journée complète mais match absent, non apparié ou non
  terminé (reporté, prolongations) ......................... `results-update
  --dry-run --day` dit lequel ;
* tennis, doubles, abandons, sport sans source, marché non réglable.

⚠️ `results-update` résume tout ça en un pourcentage par sport, et un taux ne
dit pas QUELLE cause domine. Deux causes opposées — « le pont n'a pas demandé
la journée » et « la source n'a pas le match » — donnent le même symptôme :
rien. C'est le §13.12.

⚠️ LECTURE SEULE. Rien n'est écrit, ni en base ni sur disque.

Usage :
    .venv/bin/python -m scripts.resultats_manquants
    .venv/bin/python -m scripts.resultats_manquants --depuis 2026-09-19 --lister
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json  # noqa: E402

from rapidfuzz import fuzz  # noqa: E402

from src.clv import settle  # noqa: E402
from src.config import load_env_file  # noqa: E402
from src.matcher import (_strip_class_tag, class_marker_from_league,  # noqa: E402
                         normalize_team, team_class, team_similarity,
                         with_class_marker)
from src.scores import tolerance_for_scores  # noqa: E402

#: Même grâce que `results-update` (`until = now - 2 h`) : avant, le match
#: n'est pas encore réclamé, et le compter comme « manquant » serait faux.
GRACE = timedelta(hours=2)

#: Les sports qui ont une source de résultats (`score_sources.provider_for`).
SPORTS_AVEC_SOURCE = ("soccer", "tennis")
MARCHES_REGLABLES = ("h2h", "totals")

REGLE = "réglé"
A_VENIR = "pas encore joué, ou fini depuis moins de 2 h"
FOOT_DU_JOUR = "football d'aujourd'hui — le pont ne prend une journée que le lendemain"
JAMAIS_DEMANDEE = "journée JAMAIS récupérée — elle est dans la fenêtre du pont"
HORS_FENETRE = "journée JAMAIS récupérée — hors de la fenêtre du pont, ne sera plus demandée"
REFUSEE = "journée REFUSÉE par la source (hors abonnement) — définitif"
TROP_TOT = "journée capturée trop tôt — sera reprise au prochain clic"
TROP_TOT_HORS = "journée capturée trop tôt et hors fenêtre — ne sera plus reprise"
FOOT_ABSENT = "journée complète, mais match absent de la source, non apparié ou non terminé"
TENNIS = "tennis sans résultat (doubles et abandons ne se règlent jamais)"
SANS_EVENTS = "aucune ligne `events` — jamais réclamé"
SANS_SOURCE = "sport sans source de résultats"
MARCHE = "marché que `settle` ne sait pas régler (mi-temps…)"
INEXPLOITABLE = "résultat en base, mais insuffisant pour ce pari"

#: L'ordre d'impression : ce qui est normal d'abord, puis ce qui demande un
#: geste, du plus fréquent attendu au plus rare.
ORDRE = [REGLE, A_VENIR, FOOT_DU_JOUR, JAMAIS_DEMANDEE, HORS_FENETRE, REFUSEE,
         TROP_TOT, TROP_TOT_HORS, FOOT_ABSENT, TENNIS, SANS_EVENTS, SANS_SOURCE,
         MARCHE, INEXPLOITABLE]
NORMAUX = {REGLE, A_VENIR, FOOT_DU_JOUR}


def _instant(brut) -> "datetime | None":
    if not brut:
        return None
    try:
        d = datetime.fromisoformat(str(brut).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _coup_d_envoi(r) -> "datetime | None":
    """`events.start_time`, sinon l'heure UTC écrite en tête de la clé."""
    t = _instant(r["start_time"])
    if t is not None:
        return t
    try:
        return datetime.strptime(str(r["event_key"]).split("::", 1)[0],
                                 "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def etat_journee(jour: date, dossier: Path, maintenant: datetime,
                 jours_pont: int, final_apres: int) -> str:
    """Ce que le pont football a fait de cette journée UTC — la même règle
    que `_handle_scores_plan`, relue et non recopiée à l'aveugle :

    * le pont ne demande que les journées d'HIER à `jours_pont` jours ;
    * une pierre tombale `.refused` est définitive ;
    * un fichier capturé moins de `final_apres` secondes après la fin de la
      journée n'est pas définitif : il sera redemandé tant que la journée
      reste dans la fenêtre."""
    recul = (maintenant.date() - jour).days
    dans_fenetre = 1 <= recul <= jours_pont
    if (dossier / f"{jour.isoformat()}.refused").exists():
        return REFUSEE
    f = dossier / f"{jour.isoformat()}.json"
    if not f.exists():
        return JAMAIS_DEMANDEE if dans_fenetre else HORS_FENETRE
    fin = datetime.combine(jour + timedelta(days=1), datetime.min.time(),
                           tzinfo=timezone.utc).timestamp()
    if f.stat().st_mtime < fin + final_apres:
        return TROP_TOT if dans_fenetre else TROP_TOT_HORS
    return FOOT_ABSENT


def classer(r, maintenant: datetime, dossier: Path, jours_pont: int,
            final_apres: int) -> str:
    """La raison pour laquelle CE pari joué a — ou n'a pas — son résultat."""
    marche = r["market"]
    if r["has_result"]:
        statut = settle(marche, r["outcome_label"], r["line"], r["winner"],
                        r["home_score"], r["away_score"])
        if statut is not None:
            return REGLE
    if marche not in MARCHES_REGLABLES:
        return MARCHE
    if r["has_result"]:
        return INEXPLOITABLE
    depart = _coup_d_envoi(r)
    if depart is None or depart > maintenant - GRACE:
        return A_VENIR
    if not r["has_event"]:
        return SANS_EVENTS
    sport = (r["sport"] or "").lower()
    if sport not in SPORTS_AVEC_SOURCE:
        return SANS_SOURCE
    if sport == "tennis":
        return TENNIS
    jour = depart.date()
    if jour >= maintenant.date():
        return FOOT_DU_JOUR
    return etat_journee(jour, dossier, maintenant, jours_pont, final_apres)


# ── Pourquoi un match de football n'a pas été rapproché ─────────────
#
# Une journée COMPLÈTE sans résultat pour un match laisse quatre coupables
# possibles, et ils appellent quatre gestes différents. Le fichier du pont est
# sur disque : on y cherche le match le plus proche et on dit lequel.

ABSENT = "absent de la source ce jour-là (trou de catalogue)"
CLASSE = "présent, mais la barrière de CLASSE le rejette (réserve / jeunes / féminin)"
NOMS = "candidat aux noms proches — à vérifier à l'œil"
HORAIRE = "présent, mais horaire décalé au-delà de la tolérance"
STATUT = "présent, mais pas terminé normalement (reporté, annulé, arrêté…)"
PROLONG = "présent, allé en prolongation — score à 90 min non prouvable"
APPARIABLE = "présent et appariable — relancer results-update"

ORDRE_POURQUOI = [ABSENT, CLASSE, NOMS, HORAIRE, STATUT, PROLONG, APPARIABLE]

#: En dessous, aucun candidat n'est retenu comme « le même match ». Le score
#: flou est large : « Sporting Lisbon » contre « Porto » vaut 80. Au-dessus de
#: ce plancher, la sonde MONTRE le candidat au lieu de le déclarer identique.
PLANCHER_CANDIDAT = 70.0
#: Le seuil du vrai rapprochement (`bind_results`, `match_event`).
SEUIL_APPARIEMENT = 85.0
#: Entre le plancher et le seuil, le flou seul ne suffit pas : sur une journée
#: de mille matchs, « Juventus - Atalanta » vaut 80 contre « Amatitlan -
#: Juventud Copalera ». Un candidat de la zone grise n'est montré que s'il a la
#: forme d'un vrai même-match — un camp identique (club renommé : « York
#: United » devenu « Inter Toronto ») ou deux camps proches (abréviations :
#: « Dep. Maipu », « Atl. Tucuman ») — ET qu'il se joue à moins de deux heures
#: du nôtre. Sinon le match est absent, et la sonde le dit.
FENETRE_NOMS_MIN = 120.0
COTE_IDENTIQUE = 95.0
COTES_PROCHES = 75.0


def _sim_sans_classe(a: str, b: str) -> float:
    """`team_similarity` SANS la barrière de classe — pour la voir agir."""
    sa, sb = _strip_class_tag(normalize_team(a)), _strip_class_tag(normalize_team(b))
    return max(fuzz.token_set_ratio(sa, sb), fuzz.partial_ratio(sa, sb))


def _paire(sim, h, a, th, ta) -> float:
    """Même règle que `match_event` : moyenne des deux camps, meilleure des
    deux orientations."""
    return max((sim(h, th) + sim(a, ta)) / 2, (sim(h, ta) + sim(a, th)) / 2)


def _camps(sim, h, a, th, ta) -> tuple:
    """Les deux similarités camp par camp, dans l'orientation retenue par
    `_paire` (la meilleure moyenne)."""
    return max((sim(h, th), sim(a, ta)), (sim(h, ta), sim(a, th)), key=sum)


def _plausible(u: float, camps: tuple, dt: float) -> bool:
    """Ce candidat peut-il être NOTRE match ? Voir `FENETRE_NOMS_MIN`."""
    if u >= SEUIL_APPARIEMENT:
        return True
    if u < PLANCHER_CANDIDAT or dt > FENETRE_NOMS_MIN:
        return False
    return max(camps) >= COTE_IDENTIQUE or min(camps) >= COTES_PROCHES


def _fixtures_autour(dossier: Path, jour: date, cache: dict) -> list:
    """Les matchs servis par la source la veille, le jour et le lendemain.

    Les voisins comptent : un match à 23 h 30 chez nous peut être daté du
    lendemain chez la source, et le chercher dans un seul fichier le déclarerait
    absent à tort."""
    out = []
    for d in (jour - timedelta(days=1), jour, jour + timedelta(days=1)):
        if d not in cache:
            f = dossier / f"{d.isoformat()}.json"
            try:
                cache[d] = json.loads(f.read_text()).get("response") or []
            except (OSError, ValueError, AttributeError):
                cache[d] = []
        out.extend(cache[d])
    return out


def diagnostiquer(r, noms: dict, dossier: Path, cache: dict) -> dict:
    """Le match de la source le plus proche de CE pari, et le verdict.

    Nos noms passent par le registre `teams` (le nom affiché, pas la forme
    compactée de la clé) et reçoivent la classe de leur ligue ; ceux de la
    source reçoivent la classe de LEUR ligue — exactement ce que font
    `results-update` et `parse_apifootball_results`. Une sonde qui comparerait
    autre chose que la production mentirait (§17.7)."""
    marque = class_marker_from_league(r["league"] or "")
    h = with_class_marker(noms.get(r["home"]) or r["home"] or "", marque)
    a = with_class_marker(noms.get(r["away"]) or r["away"] or "", marque)
    depart = _coup_d_envoi(r)
    meilleur = None
    for f in _fixtures_autour(dossier, depart.date(), cache):
        fx, eq, lg = f.get("fixture") or {}, f.get("teams") or {}, f.get("league") or {}
        m2 = class_marker_from_league(lg.get("name") or "")
        th = with_class_marker(((eq.get("home") or {}).get("name") or "").strip(), m2)
        ta = with_class_marker(((eq.get("away") or {}).get("name") or "").strip(), m2)
        if not th or not ta:
            continue
        camps = _camps(_sim_sans_classe, h, a, th, ta)
        u = sum(camps) / 2
        t = _instant(fx.get("date"))
        dt = abs((t - depart).total_seconds()) / 60 if t else float("inf")
        # Un candidat plausible passe devant un bruit mieux noté : sinon un
        # « Juventus - Atalanta » à 80 masquerait le club renommé à 75.
        plausible = _plausible(u, camps, dt)
        cle = (plausible, u, -dt)
        if meilleur is None or cle > meilleur["_cle"]:
            meilleur = {"_cle": cle, "u": u, "plausible": plausible,
                        "g": _paire(team_similarity, h, a, th, ta),
                        "dt": dt, "nom": f"{th} - {ta}",
                        "ligue": lg.get("name") or "?",
                        "statut": ((fx.get("status") or {}).get("short") or "?").upper(),
                        "classes": (team_class(normalize_team(h)),
                                    team_class(normalize_team(th)))}
    nous = f"{h} - {a}"
    if meilleur is None or not meilleur["plausible"]:
        return {"verdict": ABSENT, "nous": nous, "cand": meilleur}
    if meilleur["g"] < SEUIL_APPARIEMENT <= meilleur["u"]:
        verdict = CLASSE
    elif meilleur["g"] < SEUIL_APPARIEMENT:
        verdict = NOMS
    elif meilleur["dt"] > tolerance_for_scores("soccer"):
        verdict = HORAIRE
    elif meilleur["statut"] == "FT":
        verdict = APPARIABLE
    elif meilleur["statut"] in ("AET", "PEN"):
        verdict = PROLONG
    else:
        verdict = STATUT
    return {"verdict": verdict, "nous": nous, "cand": meilleur}


def charger(db: str, depuis: date) -> list:
    """Les paris JOUÉS dont le match tombe à partir de `depuis` (UTC)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = list(con.execute("""
        SELECT pb.dedup_key, pb.played_at, pb.event_key, pb.book, pb.market,
               pb.outcome_label, pb.line, pb.odd_taken, pb.stake,
               COALESCE(e.sport, pb.sport) AS sport, e.league AS league,
               e.home AS home, e.away AS away, e.start_time AS start_time,
               (e.event_key IS NOT NULL) AS has_event,
               (r.event_key IS NOT NULL) AS has_result,
               r.winner, r.home_score, r.away_score
        FROM played_bets pb
        LEFT JOIN events e  ON e.event_key = pb.event_key
        LEFT JOIN results r ON r.event_key = pb.event_key
    """))
    try:
        noms = {n: d for n, d in con.execute(
            "SELECT normalized_name, display_name FROM teams")}
    except sqlite3.OperationalError:
        noms = {}                  # base sans registre : les noms de la clé
    con.close()
    seuil = datetime.combine(depuis, datetime.min.time(), tzinfo=timezone.utc)
    return [r for r in rows
            if (_coup_d_envoi(r) or _instant(r["played_at"]) or seuil) >= seuil], noms


def analyser(rows: list, maintenant: datetime, dossier: Path, jours_pont: int,
             final_apres: int) -> dict:
    classes = [(r, classer(r, maintenant, dossier, jours_pont, final_apres))
               for r in rows]
    return {"classes": classes,
            "comptes": Counter(c for _r, c in classes)}


def _nom(r) -> str:
    return f"{r['home'] or '?'} - {r['away'] or '?'}"


def imprimer(res: dict, depuis: date, dossier: Path, jours_pont: int,
             source_jours: str, maintenant: datetime, lister: bool,
             noms: "dict | None" = None) -> None:
    classes, comptes = res["classes"], res["comptes"]
    total = len(classes)
    print(f"PARIS JOUÉS SANS RÉSULTAT — matchs à partir du {depuis.isoformat()} (UTC)")
    print(f"Pont football : {dossier}  ·  SCORES_BRIDGE_DAYS = {jours_pont} "
          f"({source_jours})\n")
    if not total:
        print("Aucun pari joué sur cette fenêtre.")
        return

    manquants = total - sum(comptes[c] for c in NORMAUX)
    larg = max(len(c) for c in ORDRE)
    print(f"Sur {total} paris joués :")
    for c in ORDRE:
        if comptes[c]:
            marque = "  " if c in NORMAUX else "❗"
            print(f"  {marque} {c:{larg}}  {comptes[c]:5}")
    print(f"\n→ {manquants} pari(s) dont le match est FINI et le résultat "
          f"ABSENT (lignes ❗).")
    if not manquants:
        print("  Rien ne manque : les paris non réglés sont des matchs à venir.")
        return

    # Par journée, pour le football : c'est l'unité que le pont récupère.
    foot = defaultdict(Counter)
    for r, c in classes:
        if c not in NORMAUX and c not in (TENNIS, SANS_EVENTS, SANS_SOURCE,
                                          MARCHE, INEXPLOITABLE):
            foot[_coup_d_envoi(r).date()][c] += 1
    if foot:
        print("\nFOOTBALL, JOURNÉE PAR JOURNÉE (la journée est l'unité du pont)")
        for j in sorted(foot):
            (c, n), = foot[j].most_common(1)
            print(f"  {j.isoformat()}  {sum(foot[j].values()):3} pari(s)  — {c}")

    pourquoi = Counter()
    absents_foot = [r for r, c in classes if c == FOOT_ABSENT]
    if absents_foot:
        cache: dict = {}
        diags = [(r, diagnostiquer(r, noms or {}, dossier, cache))
                 for r in sorted(absents_foot, key=_coup_d_envoi)]
        pourquoi = Counter(d["verdict"] for _r, d in diags)
        print("\nFOOTBALL — CE QUE LA SOURCE AVAIT CE JOUR-LÀ, match par match")
        print("(le candidat le plus proche dans le fichier du pont, veille et "
              "lendemain compris)")
        for v in ORDRE_POURQUOI:
            if pourquoi[v]:
                print(f"  {pourquoi[v]:3}  {v}")
        for v in ORDRE_POURQUOI:
            lot = [(r, d) for r, d in diags if d["verdict"] == v]
            if not lot:
                continue
            print(f"\n── {v} ({len(lot)})")
            for r, d in lot:
                c = d["cand"]
                print(f"  {_coup_d_envoi(r).strftime('%Y-%m-%d %H:%M')}  "
                      f"{d['nous'][:40]:40}  [{(r['league'] or '?')[:30]}]")
                if c is not None and v != ABSENT:
                    ecart = ("" if c["dt"] == float("inf")
                             else f" · Δ {c['dt']:.0f} min")
                    print(f"      source : {c['nom'][:44]:44} [{c['ligue'][:24]}, "
                          f"{c['statut']}] · noms {c['u']:.0f} / avec classes "
                          f"{c['g']:.0f}{ecart}")

    print("\nQUE FAIRE, DANS CET ORDRE")
    jours_hors = sorted({_coup_d_envoi(r).date() for r, c in classes
                         if c in (HORS_FENETRE, TROP_TOT_HORS)})
    # Élargir AVANT de cliquer : un clic sur l'ancienne fenêtre ne reprendrait
    # pas les journées qui en sont sorties, et on croirait le trou comblé.
    if jours_hors:
        recul = (maintenant.date() - jours_hors[0]).days
        print(f"  • {len(jours_hors)} journée(s) sont SORTIES de la fenêtre du "
              f"pont ({jours_hors[0].isoformat()} → {jours_hors[-1].isoformat()}) "
              f": le pont ne\n    les demandera plus. Élargis la fenêtre à "
              f"{recul} jours au moins, puis redémarre le serveur :\n"
              f"      sed -i '/^SCORES_BRIDGE_DAYS=/d' .env && echo "
              f"'SCORES_BRIDGE_DAYS={recul}' >> .env\n"
              f"      sudo systemctl restart betano-ingest\n"
              f"    ⚠️ Si ton abonnement API-Football est gratuit, il ne sert que "
              f"trois jours autour\n    d'aujourd'hui : les journées plus "
              f"anciennes seront REFUSÉES, définitivement.")
    if jours_hors or comptes[JAMAIS_DEMANDEE] or comptes[TROP_TOT]:
        print("  • Clique « Récupérer mes résultats » dans le script, attends la "
              "fin, puis :\n"
              "      .venv/bin/python -m src.main results-update --days 10 "
              "--sport soccer,tennis\n"
              "      .venv/bin/python -m src.main track-update\n"
              "    et relance cette sonde pour voir ce qui reste.")
    if pourquoi[APPARIABLE]:
        print(f"  • {pourquoi[APPARIABLE]} match(s) présents et appariables : "
              f"results-update n'est pas repassé\n    depuis leur arrivée —\n"
              f"      .venv/bin/python -m src.main results-update --days 10 "
              f"--sport soccer\n"
              f"      .venv/bin/python -m src.main track-update")
    if pourquoi[CLASSE] or pourquoi[NOMS] or pourquoi[HORAIRE]:
        n = pourquoi[CLASSE] + pourquoi[NOMS] + pourquoi[HORAIRE]
        print(f"  • {n} match(s) que la source A, mais que le rapprochement "
              f"rejette (classe, noms ou\n    horaire) : c'est corrigeable dans "
              f"le code. Envoie la section ci-dessus — chaque\n    candidat est "
              f"à confirmer à l'œil avant de toucher aux règles.")
    if pourquoi[STATUT] or pourquoi[PROLONG]:
        print(f"  • {pourquoi[STATUT] + pourquoi[PROLONG]} match(s) reportés, "
              f"annulés, arrêtés ou allés en prolongation :\n    vérifie le "
              f"règlement chez le book (souvent remboursé) ; rien à corriger ici.")
    if pourquoi[ABSENT]:
        print(f"  • {pourquoi[ABSENT]} match(s) ABSENTS de la source (petites "
              f"ligues, amicaux) : rien à\n    corriger — seul un autre "
              f"fournisseur de résultats les couvrirait.")
    if comptes[TENNIS]:
        print(f"  • {comptes[TENNIS]} match(s) de tennis : relancer "
              f"`results-update --days 10 --sport tennis`. Ce qui reste\n"
              f"    ensuite est surtout des DOUBLES et des ABANDONS, que la "
              f"source ne règle pas —\n    ceux-là sont à noter à la main, "
              f"ou à laisser non réglés.")
    if comptes[SANS_EVENTS]:
        print(f"  • {comptes[SANS_EVENTS]} pari(s) sans ligne `events` : "
              f"`.venv/bin/python -m scripts.repair_events --apply`.")
    if comptes[REFUSEE]:
        print(f"  • {comptes[REFUSEE]} pari(s) sur des journées refusées par la "
              f"source : perdus pour le P&L automatique.")

    if lister:
        print("\nLES PARIS SANS RÉSULTAT, UN PAR UN")
        for c in ORDRE:
            lot = [r for r, cc in classes if cc == c and c not in NORMAUX]
            if not lot:
                continue
            print(f"\n── {c} ({len(lot)})")
            for r in sorted(lot, key=lambda r: _coup_d_envoi(r) or maintenant):
                d = _coup_d_envoi(r)
                pari = f"{r['outcome_label']}"
                # Le libellé porte parfois déjà la ligne (« over 2.5 ») : ne
                # pas l'écrire deux fois.
                if r["line"] is not None and f"{r['line']:g}" not in pari:
                    pari += f" {r['line']:g}"
                print(f"  {d.strftime('%Y-%m-%d %H:%M') if d else '?':16} "
                      f"{(r['sport'] or '?')[:6]:6} {_nom(r)[:38]:38} "
                      f"{(r['market'] or '')[:7]:7} {pari[:12]:12} "
                      f"@{float(r['odd_taken'] or 0):5.2f} "
                      f"{(r['book'] or '')[:14]:14} "
                      f"{(r['league'] or '')[:28]}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Tes paris JOUÉS sans résultat : combien, et pourquoi "
                    "chacun — journée non récupérée par le pont, refusée par "
                    "la source, match absent, tennis, etc. Lecture seule.")
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--depuis", default=None, metavar="AAAA-MM-JJ",
                    help="Premier jour de match inclus (UTC). Défaut : il y a "
                         "10 jours.")
    ap.add_argument("--lister", action="store_true",
                    help="Nommer chaque pari sans résultat, groupé par cause.")
    a = ap.parse_args(argv)

    # `.env` d'abord : `SCORES_BRIDGE_DAYS` y vit, et c'est lui qui décide de
    # la fenêtre du pont. Le lire avant le chargement ferait juger la fenêtre
    # sur le défaut (2 jours) pendant que le serveur en utilise une autre.
    load_env_file()
    brut = os.getenv("SCORES_BRIDGE_DAYS")
    jours_pont = int(brut) if brut else 2
    source_jours = ("lu dans l'environnement" if brut
                    else "absent de .env — défaut du serveur")
    final_apres = int(os.getenv("SCORES_FINAL_AFTER_SEC", str(6 * 3600)))
    # Même défaut que le serveur d'ingestion : relatif au PROJET, pas au
    # répertoire courant — lancée d'ailleurs, la sonde regarderait un dossier
    # vide et déclarerait toutes les journées « jamais récupérées ».
    racine = Path(__file__).resolve().parents[1]
    dossier = Path(os.getenv("SCORES_INGEST_DIR",
                             str(racine / "data" / "scores"))) / "soccer"

    maintenant = datetime.now(timezone.utc)
    try:
        depuis = (date.fromisoformat(a.depuis) if a.depuis
                  else maintenant.date() - timedelta(days=10))
    except ValueError:
        ap.error(f"--depuis attend AAAA-MM-JJ, reçu {a.depuis!r}")
    rows, noms = charger(a.db, depuis)
    res = analyser(rows, maintenant, dossier, jours_pont, final_apres)
    imprimer(res, depuis, dossier, jours_pont, source_jours, maintenant, a.lister,
             noms)


if __name__ == "__main__":
    main()
