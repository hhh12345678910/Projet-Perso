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
* journée refusée par la source (hors abonnement) .......... au gratuit,
  perdue ; avec un abonnement payant, se reprend ;
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

⚠️ Ces comptes portent sur les CLICS (`played_bets`). Le dashboard, lui,
déduplique par match + pari et filtre sur la date de DÉTECTION : les deux
totaux s'expliquent l'un l'autre, mais ne se comparent pas un pour un.

⚠️ LECTURE SEULE. Rien n'est écrit, ni en base ni sur disque.

Usage :
    .venv/bin/python -m scripts.resultats_manquants
    .venv/bin/python -m scripts.resultats_manquants --depuis 2026-09-19 --lister
"""
from __future__ import annotations

import argparse
import os
import re
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
from src.score_sources import _score_90_minutes  # noqa: E402
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
REFUSEE = "journée REFUSÉE par la source — hors de ton abonnement API-Football"
TROP_TOT = "journée capturée trop tôt — sera reprise au prochain clic"
TROP_TOT_HORS = "journée capturée trop tôt et hors fenêtre — ne sera plus reprise"
FOOT_ABSENT = "journée complète, mais match absent de la source, non apparié ou non terminé"
TENNIS = "tennis sans résultat (doubles et abandons ne se règlent jamais)"
SANS_EVENTS = "aucune ligne `events` — jamais réclamé"
SPORT_INCONNU = "ligne `events` sans sport (« unknown ») — results-update ne la réclame pas"
SANS_SOURCE = "sport sans source de résultats"
FOOT_SANS_PONT = "football, mais results-update n'utilise pas le pont (SCORES_FOOTBALL_BRIDGE≠1)"
TENNIS_SANS_CLE = "tennis, mais SCORES_TENNIS_KEY est vide — aucune source"
NON_RATTACHE = "clic non rattaché à son value bet (marché inconnu)"
MARCHE = "marché que `settle` ne sait pas régler (mi-temps…)"
INEXPLOITABLE = "résultat en base, mais insuffisant pour ce pari"

#: L'ordre d'impression : ce qui est normal d'abord, puis ce qui demande un
#: geste, du plus fréquent attendu au plus rare.
ORDRE = [REGLE, A_VENIR, FOOT_DU_JOUR, FOOT_SANS_PONT, JAMAIS_DEMANDEE,
         HORS_FENETRE, REFUSEE, TROP_TOT, TROP_TOT_HORS, FOOT_ABSENT, TENNIS,
         TENNIS_SANS_CLE, SANS_EVENTS, SPORT_INCONNU, SANS_SOURCE, NON_RATTACHE,
         MARCHE, INEXPLOITABLE]
NORMAUX = {REGLE, A_VENIR, FOOT_DU_JOUR}
#: Les causes qui tiennent à la JOURNÉE du pont football.
FOOT_PONT = (JAMAIS_DEMANDEE, HORS_FENETRE, REFUSEE, TROP_TOT, TROP_TOT_HORS,
             FOOT_ABSENT)

#: Le bouton, écrit comme le menu Tampermonkey l'affiche
#: (`tools/scores-ingest.user.js`) — un libellé approché ne se trouve pas.
BOUTON = "« ⚽ Récupérer les résultats maintenant »"


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


def _col(r, nom: str, defaut=None):
    """Une colonne optionnelle — les lignes de test ne les portent pas toutes."""
    try:
        return r[nom]
    except (KeyError, IndexError):
        return defaut


def etat_journee(jour: date, dossier: Path, maintenant: datetime,
                 jours_pont: int, final_apres: int) -> str:
    """Ce que le pont football a fait de cette journée UTC — la même règle
    que `_handle_scores_plan`, relue et non recopiée à l'aveugle :

    * le pont ne demande que les journées d'HIER à `jours_pont` jours ;
    * une pierre tombale `.refused` l'empêche de redemander la journée ;
    * un fichier capturé moins de `final_apres` secondes après la fin de la
      journée n'est pas définitif : il sera redemandé tant que la journée
      reste dans la fenêtre — et qu'aucune pierre tombale ne l'en empêche.

    ⚠️ Le FICHIER passe avant la pierre tombale. `results-update` ne lit que
    `<jour>.json` et ignore les `.refused` ; une journée capturée tôt puis
    refusée à la reprise garde son fichier, et ses matchs se règlent. La
    déclarer « refusée » la ferait croire perdue."""
    recul = (maintenant.date() - jour).days
    dans_fenetre = 1 <= recul <= jours_pont
    tombe = (dossier / f"{jour.isoformat()}.refused").exists()
    f = dossier / f"{jour.isoformat()}.json"
    if not f.exists():
        if tombe:
            return REFUSEE
        return JAMAIS_DEMANDEE if dans_fenetre else HORS_FENETRE
    fin = datetime.combine(jour + timedelta(days=1), datetime.min.time(),
                           tzinfo=timezone.utc).timestamp()
    if f.stat().st_mtime < fin + final_apres:
        return TROP_TOT if dans_fenetre and not tombe else TROP_TOT_HORS
    return FOOT_ABSENT


def classer(r, maintenant: datetime, dossier: Path, jours_pont: int,
            final_apres: int, pont_actif: bool = True,
            cle_tennis: bool = True) -> str:
    """La raison pour laquelle CE pari joué a — ou n'a pas — son résultat.

    `pont_actif` et `cle_tennis` : ce que `.env` dit des sources. Sans eux, la
    sonde jugerait le football sur des fichiers que `results-update` ne lit
    pas, et accuserait les doubles d'un trou qui vient d'une clé absente."""
    marche = r["market"]
    if r["has_result"]:
        statut = settle(marche, r["outcome_label"], r["line"], r["winner"],
                        r["home_score"], r["away_score"])
        if statut is not None:
            return REGLE
    # L'horaire AVANT le marché : un pari sur un match à venir n'est pas un
    # manque, quel que soit son marché.
    depart = _coup_d_envoi(r)
    if depart is None or depart > maintenant - GRACE:
        return A_VENIR
    if marche is None and _col(r, "value_bet_id") is None:
        return NON_RATTACHE
    if marche not in MARCHES_REGLABLES:
        return MARCHE
    if r["has_result"]:
        return INEXPLOITABLE
    if not r["has_event"]:
        return SANS_EVENTS
    sport = (r["sport"] or "").lower()
    if sport not in SPORTS_AVEC_SOURCE:
        return SANS_SOURCE
    # `results-update` filtre sur `events.sport` : une ligne réparée sans
    # sport (« unknown ») n'est jamais réclamée, même si le clic dit football.
    if (_col(r, "sport_events", sport) or "").lower() in ("", "unknown"):
        return SPORT_INCONNU
    if sport == "tennis":
        return TENNIS if cle_tennis else TENNIS_SANS_CLE
    if not pont_actif:
        return FOOT_SANS_PONT
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
PROLONG = "présent, allé en prolongation — la source ne prouve pas le score à 90 min"
APPARIABLE = "présent et appariable — relancer results-update"
VOISIN = ("présent, mais dans le fichier de la veille ou du lendemain — "
          "results-update ne le lit pas")

ORDRE_POURQUOI = [ABSENT, CLASSE, NOMS, HORAIRE, STATUT, PROLONG, VOISIN,
                  APPARIABLE]

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
    """Les matchs servis par la source la veille, le jour et le lendemain,
    chacun avec la date du fichier qui le porte.

    Les voisins comptent : un match à 23 h 30 chez nous peut être daté du
    lendemain chez la source, et le chercher dans un seul fichier le déclarerait
    absent à tort. Mais `results-update` ne lit que le fichier du jour de NOTRE
    match : trouvé chez un voisin, il n'est pas « appariable » pour autant."""
    out = []
    for d in (jour - timedelta(days=1), jour, jour + timedelta(days=1)):
        if d not in cache:
            f = dossier / f"{d.isoformat()}.json"
            try:
                cache[d] = json.loads(f.read_text()).get("response") or []
            except (OSError, ValueError, AttributeError):
                cache[d] = []
        out.extend((d, f) for f in cache[d])
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
    for jour_fichier, f in _fixtures_autour(dossier, depart.date(), cache):
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
                        "jour_fichier": jour_fichier,
                        "a_90": _score_90_minutes(f) is not None,
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
    # Une prolongation dont le score à 90 min est PROUVÉ se règle comme un FT
    # (`parse_apifootball_results`) : 402 des 409 cas exploitables en base.
    elif meilleur["statut"] == "FT" or (meilleur["statut"] in ("AET", "PEN")
                                        and meilleur["a_90"]):
        verdict = (APPARIABLE if meilleur["jour_fichier"] == depart.date()
                   else VOISIN)
    elif meilleur["statut"] in ("AET", "PEN"):
        verdict = PROLONG
    else:
        verdict = STATUT
    return {"verdict": verdict, "nous": nous, "cand": meilleur}


def charger(db: str, depuis: date) -> list:
    """Les paris JOUÉS dont le match tombe à partir de `depuis` (UTC)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    # Deux replis, chacun pour une ligne qui sinon mentirait :
    # * un clic « nu » (écrit par le listener avant son rattachement) n'a ni
    #   event_key ni marché — sa date vient de la tête de `dedup_key` ;
    # * une ligne `events` réparée porte sport = 'unknown' — le sport du clic
    #   fait foi pour DIRE de quoi il s'agit, `sport_events` pour dire ce que
    #   `results-update` en fera.
    rows = list(con.execute("""
        SELECT pb.dedup_key, pb.played_at, pb.value_bet_id,
               COALESCE(pb.event_key,
                        CASE WHEN instr(pb.dedup_key, '|') > 0
                             THEN substr(pb.dedup_key, 1,
                                         instr(pb.dedup_key, '|') - 1) END)
                   AS event_key,
               pb.book, pb.market,
               pb.outcome_label, pb.line, pb.odd_taken, pb.stake,
               CASE WHEN COALESCE(e.sport, '') IN ('', 'unknown')
                    THEN COALESCE(pb.sport, e.sport) ELSE e.sport END AS sport,
               e.sport AS sport_events, e.league AS league,
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
             final_apres: int, pont_actif: bool = True,
             cle_tennis: bool = True) -> dict:
    classes = [(r, classer(r, maintenant, dossier, jours_pont, final_apres,
                           pont_actif, cle_tennis))
               for r in rows]
    return {"classes": classes,
            "comptes": Counter(c for _r, c in classes)}


def _nom(r) -> str:
    return f"{r['home'] or '?'} - {r['away'] or '?'}"


def jours_a_couvrir(maintenant: datetime, departs) -> int:
    """Le `--days` qui fait réclamer à `results-update` TOUS ces matchs.

    `results-update` prend `since = maintenant - days`, à l'heure près. Pour
    couvrir un match du jour J dès 00 h 00, il faut remonter d'un jour de plus
    que l'écart en jours : `--days 10` à 17 h ne prend plus un match à 01 h il
    y a dix jours. Une valeur figée ferait répéter à la sonde un conseil qui
    ne règle rien."""
    jours = [d.date() for d in departs if d is not None]
    if not jours:
        return 3                            # le défaut de results-update
    return max(3, (maintenant.date() - min(jours)).days + 1)


#: Remet le sport du CLIC sur une ligne `events` réparée sans sport. Tenue ici
#: pour être imprimée ET testée : une commande qu'on n'a jamais exécutée n'est
#: pas un conseil.
SQL_SPORT = ("UPDATE events SET sport = (SELECT pb.sport FROM played_bets pb "
             "WHERE pb.event_key = events.event_key AND pb.sport IN "
             "('soccer','tennis') LIMIT 1) WHERE COALESCE(sport,'') IN "
             "('','unknown') AND event_key IN (SELECT event_key FROM "
             "played_bets WHERE sport IN ('soccer','tennis'))")


def _commande_sport(db: str) -> str:
    """La commande à coller dans le shell : le SQL entre `\\"`, parce que
    l'argument de `-c` est lui-même entre guillemets doubles."""
    return (".venv/bin/python -c \"import sqlite3; "
            f"c = sqlite3.connect('{db}'); "
            f"n = c.execute(\\\"{SQL_SPORT}\\\").rowcount; c.commit(); "
            "print(n, 'ligne(s) corrigée(s)')\"")


def imprimer(res: dict, depuis: date, dossier: Path, jours_pont: int,
             source_jours: str, maintenant: datetime, lister: bool,
             noms: "dict | None" = None, db: str = "data/valuebet.db",
             pont_actif: bool = True, cle_tennis: bool = True) -> None:
    classes, comptes = res["classes"], res["comptes"]
    total = len(classes)
    print(f"PARIS JOUÉS SANS RÉSULTAT — matchs à partir du {depuis.isoformat()} (UTC)")
    print(f"Pont football : {dossier}  ·  SCORES_BRIDGE_DAYS = {jours_pont} "
          f"({source_jours})")
    print("Source football : " + ("le pont (SCORES_FOOTBALL_BRIDGE=1)" if pont_actif
          else "⚠️ l'API en direct — SCORES_FOOTBALL_BRIDGE n'est pas à 1, "
               "results-update IGNORE le pont"))
    print("Source tennis   : " + ("Live Tennis (clé présente)" if cle_tennis
          else "⚠️ aucune — SCORES_TENNIS_KEY est vide"))
    print("(Ces comptes portent sur tes CLICS ; le dashboard déduplique par "
          "match + pari et date\n à la détection — ses totaux ne se comparent "
          "pas un pour un.)\n")
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
        if c in FOOT_PONT:
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
    manques = [(r, c) for r, c in classes if c not in NORMAUX]
    # UN `--days`, calculé sur le plus vieux match qui manque : c'est ce que
    # `results-update` doit couvrir, et rien de plus petit ne le règle.
    n_jours = jours_a_couvrir(maintenant, [_coup_d_envoi(r) for r, _c in manques])
    n_tennis = jours_a_couvrir(maintenant, [_coup_d_envoi(r) for r, c in manques
                                            if c == TENNIS])
    maj = (f"      .venv/bin/python -m src.main results-update --days {n_jours} "
           f"--sport soccer,tennis\n"
           f"      .venv/bin/python -m src.main track-update")

    # 1. La configuration d'abord : sans source, aucun autre geste n'aboutit.
    if comptes[FOOT_SANS_PONT]:
        print(f"  • {comptes[FOOT_SANS_PONT]} pari(s) de football jugés par "
              f"l'API en direct, que la VM ne peut pas appeler :\n"
              f"    mets SCORES_FOOTBALL_BRIDGE=1 dans .env —\n"
              f"      sed -i '/^SCORES_FOOTBALL_BRIDGE=/d' .env && echo "
              f"'SCORES_FOOTBALL_BRIDGE=1' >> .env\n"
              f"    puis relance cette sonde : elle jugera alors les fichiers "
              f"du pont.")
    if comptes[TENNIS_SANS_CLE]:
        print(f"  • {comptes[TENNIS_SANS_CLE]} pari(s) de tennis sans source : "
              f"SCORES_TENNIS_KEY est vide dans .env (Live Tennis,\n"
              f"    palier Basic). Sans elle, results-update tombe en « panne » "
              f"sur le tennis.")

    # 2. Élargir AVANT de cliquer : un clic sur l'ancienne fenêtre ne
    #    reprendrait pas les journées qui en sont sorties, et on croirait le
    #    trou comblé.
    jours_hors = sorted({_coup_d_envoi(r).date() for r, c in classes
                         if c in (HORS_FENETRE, TROP_TOT_HORS)})
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
              f"anciennes seront REFUSÉES.")
    jours_refus = sorted({_coup_d_envoi(r).date() for r, c in classes
                          if c == REFUSEE})
    if jours_refus:
        recul_r = max(jours_pont, (maintenant.date() - jours_refus[0]).days)
        print(f"  • {comptes[REFUSEE]} pari(s) sur {len(jours_refus)} journée(s) "
              f"REFUSÉE(S) par ton abonnement API-Football ({jours_refus[0].isoformat()}"
              f" → {jours_refus[-1].isoformat()}).\n"
              f"    Au palier gratuit (trois jours autour d'aujourd'hui), ils "
              f"ne se régleront pas. Avec un\n    abonnement payant, efface les "
              f"refus et ouvre la fenêtre, puis clique :\n"
              f"      rm -f {dossier}/*.refused\n"
              f"      sed -i '/^SCORES_BRIDGE_DAYS=/d' .env && echo "
              f"'SCORES_BRIDGE_DAYS={recul_r}' >> .env\n"
              f"      sudo systemctl restart betano-ingest")

    # 3. Cliquer, puis écrire en base.
    if jours_hors or jours_refus or comptes[JAMAIS_DEMANDEE] or comptes[TROP_TOT]:
        print(f"  • Dans le menu Tampermonkey (onglet Betano, Circus ou "
              f"MagicBetting), clique\n    {BOUTON}, attends « rien à "
              f"récupérer — tout est à jour », puis :\n{maj}\n"
              f"    et relance cette sonde pour voir ce qui reste.")
    if pourquoi[APPARIABLE]:
        print(f"  • {pourquoi[APPARIABLE]} match(s) présents et appariables : "
              f"results-update n'est pas repassé\n    depuis leur arrivée —\n{maj}")
    if pourquoi[VOISIN]:
        print(f"  • {pourquoi[VOISIN]} match(s) que la source date de la veille "
              f"ou du lendemain : results-update ne lit\n    que le fichier du "
              f"jour de NOTRE match. Relancer ne sert à rien — c'est à "
              f"corriger dans\n    src/main.py (lire aussi les journées "
              f"voisines).")
    if pourquoi[CLASSE] or pourquoi[NOMS] or pourquoi[HORAIRE]:
        n = pourquoi[CLASSE] + pourquoi[NOMS] + pourquoi[HORAIRE]
        print(f"  • {n} match(s) que la source A, mais que le rapprochement "
              f"rejette (classe, noms ou\n    horaire) : c'est corrigeable dans "
              f"le code. Envoie la section ci-dessus — chaque\n    candidat est "
              f"à confirmer à l'œil avant de toucher aux règles.")
    if pourquoi[STATUT]:
        print(f"  • {pourquoi[STATUT]} match(s) reportés, annulés ou arrêtés : "
              f"vérifie le règlement chez le book\n    (souvent remboursé) ; rien "
              f"à corriger ici.")
    if pourquoi[PROLONG]:
        print(f"  • {pourquoi[PROLONG]} match(s) allés en prolongation sans score "
              f"à 90 min prouvable : le book règle\n    sur 90 min, la source ne "
              f"le donne pas — à noter à la main si tu veux le P&L exact.")
    if pourquoi[ABSENT]:
        print(f"  • {pourquoi[ABSENT]} match(s) ABSENTS de la source (petites "
              f"ligues, amicaux) : rien à\n    corriger — seul un autre "
              f"fournisseur de résultats les couvrirait.")
    if comptes[TENNIS]:
        print(f"  • {comptes[TENNIS]} match(s) de tennis : relancer\n"
              f"      .venv/bin/python -m src.main results-update --days "
              f"{n_tennis} --sport tennis\n"
              f"    Ce qui reste ensuite est surtout des DOUBLES et des ABANDONS, "
              f"que la source ne règle\n    pas — ceux-là sont à noter à la "
              f"main, ou à laisser non réglés.")

    # 4. Les lignes abîmées en base.
    if comptes[SANS_EVENTS]:
        print(f"  • {comptes[SANS_EVENTS]} pari(s) sans ligne `events`. La "
              f"réparation écrit sport = « unknown »,\n    que results-update "
              f"ignore : il faut donc remettre le sport du clic ensuite —\n"
              f"      .venv/bin/python -m scripts.repair_events --apply\n"
              f"      {_commande_sport(db)}")
    if comptes[SPORT_INCONNU]:
        print(f"  • {comptes[SPORT_INCONNU]} pari(s) dont la ligne `events` n'a "
              f"pas de sport : remets celui du clic —\n"
              f"      {_commande_sport(db)}")
    if comptes[SANS_EVENTS] or comptes[SPORT_INCONNU]:
        print(f"    puis :\n{maj}")
    if comptes[NON_RATTACHE]:
        print(f"  • {comptes[NON_RATTACHE]} clic(s) jamais rattaché(s) à leur "
              f"value bet (marché inconnu) :\n"
              f"      .venv/bin/python -m src.main backfill-played-bets")

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


def _entier(brut, defaut: int) -> int:
    """Un réglage entier de `.env`, commentaire en ligne toléré.

    `.env.example` écrit `SCORES_BRIDGE_DAYS=3          # journées…`, et
    `load_env_file` ne retire pas le commentaire : un `int()` nu lèverait une
    trace au lieu de lire 3."""
    m = re.match(r"\s*(\d+)", brut or "")
    return int(m.group(1)) if m else defaut


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
    jours_pont = _entier(brut, 2)
    source_jours = ("lu dans l'environnement" if brut
                    else "absent de .env — défaut du serveur")
    final_apres = _entier(os.getenv("SCORES_FINAL_AFTER_SEC"), 6 * 3600)
    # La même règle que `score_sources.provider_for`, relue au même endroit.
    pont_actif = (os.getenv("SCORES_FOOTBALL_BRIDGE", "").strip().lower()
                  in ("1", "true", "yes"))
    cle_tennis = bool(os.getenv("SCORES_TENNIS_KEY", "").strip())
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
    res = analyser(rows, maintenant, dossier, jours_pont, final_apres,
                   pont_actif, cle_tennis)
    imprimer(res, depuis, dossier, jours_pont, source_jours, maintenant, a.lister,
             noms, a.db, pont_actif, cle_tennis)


if __name__ == "__main__":
    main()
