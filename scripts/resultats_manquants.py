#!/usr/bin/env python3
"""Les détections sans résultat — et tes paris joués — : combien, et POURQUOI.

Par défaut, TOUTES les détections : c'est la population que `results-update`
règle (un match qui porte au moins un value bet) et celle sur laquelle le
projet mesure CLV et ROI, jouée ou non (§ `events_awaiting_result`). L'unité
est alors le MATCH. `--joues` restreint à tes clics sur « Jouer », pari par
pari.

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
    .venv/bin/python -m scripts.resultats_manquants --depuis 2026-06-01
    .venv/bin/python -m scripts.resultats_manquants --joues --depuis 2026-09-19 --lister
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
from dataclasses import replace  # noqa: E402

from rapidfuzz import fuzz, process  # noqa: E402

from src.clv import settle  # noqa: E402
from src.config import load_env_file  # noqa: E402
from src.matcher import (_strip_class_tag, class_marker_from_league,  # noqa: E402
                         match_event, normalize_team, team_class,
                         team_similarity, with_class_marker)
from src.score_sources import _score_90_minutes, parse_apifootball_results  # noqa: E402
from src.scores import OurEvent, bind_results, tolerance_for_scores  # noqa: E402

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
# Propres aux DÉTECTIONS, où l'unité est le match et non le pari.
REGLE_SANS_SCORE = "réglé, mais sans score — ses totals ne se règlent pas"
AUTRE_CLE = "résultat connu sous une AUTRE clé du même match (horaire révisé)"
DOUBLE = "tennis, double — la source n'en sert aucun"
TENNIS_SIMPLE = "tennis, simple sans résultat — abandon, forfait, ou absent de la source"

#: L'ordre d'impression : ce qui est normal d'abord, puis ce qui demande un
#: geste, du plus fréquent attendu au plus rare.
ORDRE = [REGLE, A_VENIR, FOOT_DU_JOUR, FOOT_SANS_PONT, JAMAIS_DEMANDEE,
         HORS_FENETRE, REFUSEE, TROP_TOT, TROP_TOT_HORS, FOOT_ABSENT, TENNIS,
         TENNIS_SANS_CLE, SANS_EVENTS, SPORT_INCONNU, SANS_SOURCE, NON_RATTACHE,
         MARCHE, INEXPLOITABLE]
NORMAUX = {REGLE, A_VENIR, FOOT_DU_JOUR}
ORDRE_DET = [REGLE, REGLE_SANS_SCORE, A_VENIR, FOOT_DU_JOUR, FOOT_SANS_PONT,
             JAMAIS_DEMANDEE, HORS_FENETRE, REFUSEE, TROP_TOT, TROP_TOT_HORS,
             AUTRE_CLE, FOOT_ABSENT, DOUBLE, TENNIS_SIMPLE, TENNIS_SANS_CLE,
             SPORT_INCONNU, SANS_SOURCE]
NORMAUX_DET = {REGLE, REGLE_SANS_SCORE, A_VENIR, FOOT_DU_JOUR}
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
AMBIGU = ("présent, mais un autre match de la source lui ressemble trop — "
          "le rapprochement refuse de choisir")
SANS_SCORE_SOURCE = "présent et terminé, mais la source ne donne pas son score"
INEXPLIQUE = ("présent et appariable selon la sonde, pourtant la production "
              "ne le lie pas — à signaler")

ORDRE_POURQUOI = [ABSENT, CLASSE, NOMS, HORAIRE, STATUT, PROLONG,
                  SANS_SCORE_SOURCE, VOISIN, AMBIGU, INEXPLIQUE, APPARIABLE]

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


#: Un candidat plausible a une moyenne d'au moins `PLANCHER_CANDIDAT` (70),
#: donc au moins UN camp à 70 ou plus. La recherche part de là : les noms de
#: la source à 70 et plus face à l'un des nôtres désignent les seuls matchs
#: qui peuvent être plausibles, et les scores exacts ne sont calculés que pour
#: eux. Assez rapide pour des milliers de matchs, sans changer un verdict.
SEUIL_BRUT = PLANCHER_CANDIDAT


class SourceFoot:
    """Les fichiers du pont football, lus une fois et préparés une fois.

    Deux lectures d'un même fichier, pour deux questions différentes :

    * `resultats(j)` — la liste EXACTE que `results-update` rapproche, passée
      par `parse_apifootball_results` : seulement les matchs terminés (FT, ou
      prolongation au score à 90 min prouvé), classe de la ligue reportée ;
    * `fenetre(j)` — tous les matchs servis la veille, le jour et le lendemain,
      terminés ou non, pour dire POURQUOI un match n'a pas été rapproché."""

    def __init__(self, dossier: Path):
        self.dossier = dossier
        self._brut: dict = {}
        self._res: dict = {}
        self._prep: dict = {}
        self._fen: dict = {}
        self._proches: dict = {}

    def brut(self, d: date) -> list:
        if d not in self._brut:
            f = self.dossier / f"{d.isoformat()}.json"
            try:
                self._brut[d] = json.loads(f.read_text()).get("response") or []
            except (OSError, ValueError, AttributeError):
                self._brut[d] = []
        return self._brut[d]

    def resultats(self, d: date) -> list:
        if d not in self._res:
            self._res[d] = parse_apifootball_results({"response": self.brut(d)})[0]
        return self._res[d]

    def _prepares(self, d: date) -> list:
        if d not in self._prep:
            out = []
            for f in self.brut(d):
                fx, eq, lg = (f.get("fixture") or {}, f.get("teams") or {},
                              f.get("league") or {})
                m2 = class_marker_from_league(lg.get("name") or "")
                th = with_class_marker(((eq.get("home") or {}).get("name") or "").strip(), m2)
                ta = with_class_marker(((eq.get("away") or {}).get("name") or "").strip(), m2)
                if not th or not ta:
                    continue
                out.append({"jour_fichier": d, "f": f, "th": th, "ta": ta,
                            "sh": _pour_flou(th), "sa": _pour_flou(ta),
                            "t": _instant(fx.get("date")),
                            "ligue": lg.get("name") or "?",
                            "statut": ((fx.get("status") or {}).get("short")
                                       or "?").upper()})
            self._prep[d] = out
        return self._prep[d]

    def fenetre(self, jour: date) -> tuple:
        """(matchs de la veille, du jour et du lendemain ; noms distincts ;
        index nom → matchs qui le portent).

        Les voisins comptent : un match à 23 h 30 chez nous peut être daté du
        lendemain chez la source, et le chercher dans un seul fichier le
        déclarerait absent à tort. Mais `results-update` ne lit que le fichier
        du jour de NOTRE match : trouvé chez un voisin, il n'est pas
        « appariable » pour autant."""
        if jour not in self._fen:
            fx = [x for d in (jour - timedelta(days=1), jour, jour + timedelta(days=1))
                  for x in self._prepares(d)]
            index: dict = defaultdict(list)
            for i, x in enumerate(fx):
                index[x["sh"]].append(i)
                index[x["sa"]].append(i)
            self._fen[jour] = (fx, list(index), index)
        return self._fen[jour]

    def proches(self, nom: str, jour: date) -> set:
        """`_proches` sur la fenêtre de `jour`, gardé : les clés d'un même
        match (horaire révisé) reposent la même question."""
        cle = (nom, jour)
        if cle not in self._proches:
            self._proches[cle] = _proches(nom, self.fenetre(jour)[1])
        return self._proches[cle]


#: Les classes du matcher, en mots.
CLASSES_LISIBLES = {"main": "aucune", "xwomen": "féminin", "xyouth": "jeunes",
                    "xreserve": "réserve"}


def _classe_lisible(nom: str) -> str:
    return CLASSES_LISIBLES.get(team_class(normalize_team(nom)), "?")


def _conflits(h: str, a: str, x: dict, droit: bool) -> list:
    """Les couples (notre classe, classe de la source) qui DIFFÈRENT, camp par
    camp dans l'orientation retenue — « aucune → féminin » dit une ligue
    féminine que nous ne reconnaissons pas, « réserve → jeunes » un « B »
    contre un « U21 »."""
    paires = ((h, x["th"]), (a, x["ta"])) if droit else ((h, x["ta"]), (a, x["th"]))
    return sorted({(_classe_lisible(n), _classe_lisible(t)) for n, t in paires
                   if _classe_lisible(n) != _classe_lisible(t)})


def _pour_flou(nom: str) -> str:
    """La forme que `team_similarity` compare : normalisée, classe retirée."""
    return _strip_class_tag(normalize_team(nom))


def _proches(nom: str, choix: list) -> set:
    """Les noms de la source à `SEUIL_BRUT` et plus face à `nom`, pour le
    `max(token_set_ratio, partial_ratio)` de `team_similarity` — en une passe
    par `rapidfuzz.process`, qui élague sous le seuil."""
    return {s for scorer in (fuzz.token_set_ratio, fuzz.partial_ratio)
            for s, _score, _i in process.extract(nom, choix, scorer=scorer,
                                                  score_cutoff=SEUIL_BRUT,
                                                  limit=None)}


#: Un camp sous 40 ne rend jamais un candidat plausible : l'autre camp
#: plafonne à 100, la moyenne reste sous 70. Sous ce seuil, `_flou` rend 0 —
#: plus vite, et sans changer ni la plausibilité ni le score d'un candidat
#: plausible (l'orientation qui l'est garde ses valeurs, l'autre ne peut que
#: baisser).
PLANCHER_CAMP = 40.0


def _flou(a: str, b: str) -> float:
    """`team_similarity` sans la barrière, sur des formes déjà préparées ; 0
    sous `PLANCHER_CAMP`."""
    return max(fuzz.token_set_ratio(a, b, score_cutoff=PLANCHER_CAMP),
               fuzz.partial_ratio(a, b, score_cutoff=PLANCHER_CAMP))


def _notre_evenement(r, noms: dict) -> "OurEvent":
    """Notre match tel que `results-update` le construit : le nom affiché du
    registre `teams` (repli : la clé capitalisée, comme `teams.display`), la
    ligue brute — la classe, c'est `bind_results` qui la pose."""
    def affiche(k):
        k = k or ""
        return noms.get(k) or k.capitalize()
    return OurEvent(event_key=r["event_key"] or "", home=affiche(r["home"]),
                    away=affiche(r["away"]), start_time=_coup_d_envoi(r),
                    league=r["league"] or "")


def diagnostiquer(r, noms: dict, dossier: Path, cache: dict) -> dict:
    """Le match de la source le plus proche de CE match, et le verdict.

    D'abord la PRODUCTION elle-même : `bind_results` sur les résultats du
    fichier du jour, exactement comme `results-update`. S'il lie, le match est
    appariable et rien d'autre ne compte. Sinon, on cherche dans les trois
    fichiers le candidat le plus proche, SANS la barrière de classe, pour dire
    ce qui a bloqué. Une sonde qui jugerait autrement que la production
    mentirait (§17.7) ; et si les deux se contredisent, elle le dit
    (`INEXPLIQUE`) au lieu de trancher.

    `cache` : un dict partagé d'un appel à l'autre, qui garde les fichiers lus."""
    source = cache.get(dossier)
    if source is None:
        source = cache[dossier] = SourceFoot(dossier)
    ev = _notre_evenement(r, noms)
    depart = ev.start_time
    marque = class_marker_from_league(ev.league)
    h, a = with_class_marker(ev.home, marque), with_class_marker(ev.away, marque)
    nous = f"{h} - {a}"
    tol = tolerance_for_scores("soccer")

    fx, choix, index = source.fenetre(depart.date())
    oh, oa = _pour_flou(h), _pour_flou(a)
    forts = source.proches(oh, depart.date()) | source.proches(oa, depart.date())
    meilleur = None
    for i in sorted({i for n in forts for i in index[n]}):
        x = fx[i]
        droit = (_flou(oh, x["sh"]), _flou(oa, x["sa"]))
        croise = (_flou(oh, x["sa"]), _flou(oa, x["sh"]))
        camps = max(droit, croise, key=sum)
        u = sum(camps) / 2
        dt = abs((x["t"] - depart).total_seconds()) / 60 if x["t"] else float("inf")
        # Un candidat plausible passe devant un bruit mieux noté : sinon un
        # « Juventus - Atalanta » à 80 masquerait le club renommé à 75.
        plausible = _plausible(u, camps, dt)
        cle = (plausible, u, -dt)
        if meilleur is None or cle > meilleur["_cle"]:
            meilleur = {"_cle": cle, "u": u, "plausible": plausible, "dt": dt,
                        "x": x, "droit": camps is droit}
    cand = None
    if meilleur is not None:
        x = meilleur["x"]
        cand = {"u": meilleur["u"], "plausible": meilleur["plausible"],
                "dt": meilleur["dt"], "g": _paire(team_similarity, h, a, x["th"], x["ta"]),
                "nom": f"{x['th']} - {x['ta']}", "ligue": x["ligue"],
                "statut": x["statut"], "jour_fichier": x["jour_fichier"],
                "a_90": _score_90_minutes(x["f"]) is not None,
                "a_score": all(v is not None for v in (
                    ((x["f"].get("score") or {}).get("fulltime") or {}).get(k)
                    for k in ("home", "away"))),
                # Les classes camp par camp, dans l'orientation retenue : c'est
                # ce qui dit QUELLE correction lèverait une barrière de classe.
                "conflits": _conflits(h, a, x, meilleur["droit"])}

    resultats = source.resultats(depart.date())
    if bind_results([ev], resultats, sport="soccer")[0]:
        return {"verdict": APPARIABLE, "nous": nous, "cand": cand}
    # La garde d'ambiguïté de `match_event` : deux résultats presque aussi
    # bons, et la production refuse de choisir. Sans marge, elle aurait lié.
    evm = replace(ev, home=h, away=a)
    if match_event(evm, resultats, time_tolerance_minutes=tol,
                   min_score=SEUIL_APPARIEMENT, ambiguity_margin=0) is not None:
        return {"verdict": AMBIGU, "nous": nous, "cand": cand}

    if cand is None or not cand["plausible"]:
        return {"verdict": ABSENT, "nous": nous, "cand": cand}
    if cand["g"] < SEUIL_APPARIEMENT <= cand["u"]:
        verdict = CLASSE
    elif cand["g"] < SEUIL_APPARIEMENT:
        verdict = NOMS
    elif cand["dt"] > tol:
        verdict = HORAIRE
    # Une prolongation dont le score à 90 min est PROUVÉ se règle comme un FT
    # (`parse_apifootball_results`) : 402 des 409 cas exploitables en base.
    elif cand["statut"] == "FT" and not cand["a_score"]:
        # `parse_apifootball_results` l'écarte (`score_manquant`) : sans score,
        # rien à régler, et ce n'est pas une contradiction avec la production.
        verdict = SANS_SCORE_SOURCE
    elif cand["statut"] == "FT" or (cand["statut"] in ("AET", "PEN")
                                    and cand["a_90"]):
        # La production n'a PAS lié ce match (vérifié plus haut). Trouvé chez
        # un voisin, c'est attendu ; dans le bon fichier, c'est une
        # contradiction entre la sonde et la production — à signaler, pas à
        # arbitrer ici.
        verdict = VOISIN if cand["jour_fichier"] != depart.date() else INEXPLIQUE
    elif cand["statut"] in ("AET", "PEN"):
        verdict = PROLONG
    else:
        verdict = STATUT
    return {"verdict": verdict, "nous": nous, "cand": cand}


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

    _conseils(classes, comptes, pourquoi, dossier, jours_pont, maintenant, db, "pari(s)")

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


# ── TOUTES les détections, et non les seuls paris joués ────────────
#
# L'unité change : `results-update` règle des MATCHS (une `event_key`), et un
# match porte en moyenne plusieurs détections. La population est celle de
# `Storage.events_awaiting_result` — un match avec au moins un value bet —
# relue à l'identique, parce qu'une sonde qui compterait autre chose que la
# production expliquerait un chiffre qui n'existe pas (§17.7).

#: Un match à gros volume de détections pèse plus sur le ROI : les exemples
#: montrés sont ceux qui en portent le plus.
EXEMPLES = 5
LIGUES_MONTREES = 8


def charger_detections(db: str, depuis: date) -> tuple:
    """(matchs portant au moins un value bet et joués à partir de `depuis` ;
    noms affichés ; clés « équipes + jour » des matchs qui ONT un résultat).

    Le troisième élément sert à reconnaître un match réglé sous une autre
    clé : une révision d'horaire crée une clé neuve (§17.8), et la même clé
    « équipes + jour » est celle du dashboard (`analytics.requete.EXPR_CLE`)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    borne = datetime.combine(depuis, datetime.min.time(),
                             tzinfo=timezone.utc).isoformat()
    # `bet_features` porte la ligue vue au moment de la détection ; `events`
    # peut l'avoir perdue (`build_event_rows` écrit "" quand aucune cote n'en
    # portait, et INSERT OR IGNORE ne la corrige jamais). La production ne lit
    # QUE `events.league` — celle de `bet_features` sert à mesurer ce qu'elle
    # rapporterait, jamais à juger.
    requete = """
        SELECT e.event_key, e.sport, e.league, e.home, e.away, e.start_time,
               v.n AS n_det,
               (r.event_key IS NOT NULL) AS has_result,
               (r.home_score IS NOT NULL AND r.away_score IS NOT NULL) AS has_scores,
               {ligue_bf} AS league_bf
        FROM (SELECT event_key, COUNT(*) AS n FROM value_bets
              GROUP BY event_key) v
        JOIN events e       ON e.event_key = v.event_key
        LEFT JOIN results r ON r.event_key = e.event_key
        WHERE e.start_time >= ?
    """
    try:
        rows = list(con.execute(requete.format(ligue_bf="""(
            SELECT bf.league FROM bet_features bf
            WHERE bf.event_key = e.event_key AND COALESCE(bf.league, '') <> ''
            LIMIT 1)"""), (borne,)))
    except sqlite3.OperationalError:
        rows = list(con.execute(requete.format(ligue_bf="NULL"), (borne,)))
    reglees = {(h, a, j) for h, a, j in con.execute("""
        SELECT lower(e.home), lower(e.away), substr(e.start_time, 1, 10)
        FROM results r JOIN events e ON e.event_key = r.event_key
        WHERE e.start_time >= ?""", ((datetime.fromisoformat(borne)
                                      - timedelta(days=1)).isoformat(),))}
    try:
        noms = {n: d for n, d in con.execute(
            "SELECT normalized_name, display_name FROM teams")}
    except sqlite3.OperationalError:
        noms = {}
    con.close()
    return [dict(r) for r in rows], noms, reglees


def _autre_cle(r, reglees: set) -> bool:
    """Ce match a-t-il son résultat sous une autre clé : mêmes équipes, même
    jour UTC, dans un sens ou dans l'autre ?"""
    h, a = (r["home"] or "").lower(), (r["away"] or "").lower()
    j = (r["start_time"] or "")[:10]
    return bool(h and a and j) and ((h, a, j) in reglees or (a, h, j) in reglees)


def _est_double(r, noms: dict) -> bool:
    """Une paire de double s'écrit « Bolelli S / Vavassori A » (matcher.py) —
    lisible sur le nom AFFICHÉ, pas sur la clé compactée."""
    return any("/" in (noms.get(k or "") or k or "")
               for k in (r["home"], r["away"]))


def classer_detection(r, maintenant: datetime, dossier: Path, jours_pont: int,
                      final_apres: int, pont_actif: bool, cle_tennis: bool,
                      reglees: set, noms: dict, memo: dict) -> str:
    """La raison pour laquelle CE match détecté a — ou n'a pas — son résultat.
    `memo` garde l'état de chaque journée du pont : il ne change pas d'un
    match à l'autre."""
    if r["has_result"]:
        return REGLE if r["has_scores"] else REGLE_SANS_SCORE
    depart = _coup_d_envoi(r)
    if depart is None or depart > maintenant - GRACE:
        return A_VENIR
    sport = (r["sport"] or "").lower()
    if sport in ("", "unknown"):
        return SPORT_INCONNU
    if sport not in SPORTS_AVEC_SOURCE:
        return SANS_SOURCE
    if _autre_cle(r, reglees):
        return AUTRE_CLE
    if sport == "tennis":
        if not cle_tennis:
            return TENNIS_SANS_CLE
        return DOUBLE if _est_double(r, noms) else TENNIS_SIMPLE
    if not pont_actif:
        return FOOT_SANS_PONT
    jour = depart.date()
    if jour >= maintenant.date():
        return FOOT_DU_JOUR
    if jour not in memo:
        memo[jour] = etat_journee(jour, dossier, maintenant, jours_pont, final_apres)
    return memo[jour]


def analyser_detections(rows: list, maintenant: datetime, dossier: Path,
                        jours_pont: int, final_apres: int, pont_actif: bool,
                        cle_tennis: bool, reglees: set, noms: dict,
                        progres=None) -> dict:
    memo: dict = {}
    classes = [(r, classer_detection(r, maintenant, dossier, jours_pont,
                                     final_apres, pont_actif, cle_tennis,
                                     reglees, noms, memo))
               for r in rows]
    comptes = Counter(c for _r, c in classes)
    dets = Counter()
    for r, c in classes:
        dets[c] += r["n_det"] or 0
    a_diagnostiquer = sorted((r for r, c in classes if c == FOOT_ABSENT),
                             key=_coup_d_envoi)
    cache: dict = {}
    diags = []
    for i, r in enumerate(a_diagnostiquer, 1):
        diags.append((r, diagnostiquer(r, noms, dossier, cache)))
        if progres and i % 250 == 0:
            progres(i, len(a_diagnostiquer))
    # Ce que rapporterait la ligue de `bet_features` là où `events` n'en a
    # pas : même rapprochement, seule la ligue change. Une mesure, pas un
    # verdict — la production, elle, ne lit que `events`.
    ligue_rendue = 0
    source = cache.get(dossier) or SourceFoot(dossier)
    for r, d in diags:
        if d["verdict"] == APPARIABLE or (r["league"] or "") or not r["league_bf"]:
            continue
        ev = replace(_notre_evenement(r, noms), league=r["league_bf"])
        if bind_results([ev], source.resultats(ev.start_time.date()),
                        sport="soccer")[0]:
            ligue_rendue += 1
    return {"classes": classes, "comptes": comptes, "dets": dets,
            "diags": diags, "ligue_rendue": ligue_rendue}


def _tranche_horaire(dt: float) -> str:
    for borne in (30, 60, 120, 360):
        if dt <= borne:
            return f"≤ {borne} min"
    return "> 6 h"


def imprimer_detections(res: dict, depuis: date, dossier: Path, jours_pont: int,
                        source_jours: str, maintenant: datetime, lister: bool,
                        db: str, pont_actif: bool, cle_tennis: bool) -> None:
    classes, comptes, dets = res["classes"], res["comptes"], res["dets"]
    print(f"DÉTECTIONS SANS RÉSULTAT — matchs à partir du {depuis.isoformat()} (UTC)")
    print(f"Pont football : {dossier}  ·  SCORES_BRIDGE_DAYS = {jours_pont} "
          f"({source_jours})")
    print("Source football : " + ("le pont (SCORES_FOOTBALL_BRIDGE=1)" if pont_actif
          else "⚠️ l'API en direct — SCORES_FOOTBALL_BRIDGE n'est pas à 1, "
               "results-update IGNORE le pont"))
    print("Source tennis   : " + ("Live Tennis (clé présente)" if cle_tennis
          else "⚠️ aucune — SCORES_TENNIS_KEY est vide"))
    print("Unité : le MATCH (une event_key) — ce que results-update règle. Entre "
          "parenthèses,\nles détections (lignes value_bets) qu'il porte.\n")
    if not classes:
        print("Aucune détection sur cette fenêtre.")
        return

    total, total_d = len(classes), sum(dets.values())
    manquants = [(r, c) for r, c in classes if c not in NORMAUX_DET]
    larg = max(len(c) for c in ORDRE_DET)
    print(f"Sur {total} matchs détectés ({total_d} détections) :")
    for c in ORDRE_DET:
        if comptes[c]:
            marque = "  " if c in NORMAUX_DET else "❗"
            print(f"  {marque} {c:{larg}}  {comptes[c]:6}  ({dets[c]})")
    print(f"\n→ {len(manquants)} match(s) FINIS sans résultat, portant "
          f"{sum(r['n_det'] or 0 for r, _c in manquants)} détections (lignes ❗).")

    # La couverture : la part des matchs FINIS qui ont leur résultat. C'est
    # elle qui dit si le ROI mesuré sur les détections est représentatif.
    print("\nCOUVERTURE DES MATCHS FINIS, PAR SPORT")
    par_sport = defaultdict(lambda: [0, 0, 0, 0])
    for r, c in classes:
        if c in (A_VENIR, FOOT_DU_JOUR):
            continue
        t = par_sport[(r["sport"] or "?").lower()]
        regle = c in (REGLE, REGLE_SANS_SCORE)
        t[0] += regle
        t[1] += 1
        t[2] += (r["n_det"] or 0) if regle else 0
        t[3] += r["n_det"] or 0
    print(f"  {'sport':12} {'matchs réglés':>18}   {'détections réglées':>22}")
    for sp, (a, b, da, db_) in sorted(par_sport.items(), key=lambda x: -x[1][1]):
        pct = f"{100 * a / b:3.0f} %" if b else "  — "
        pct_d = f"{100 * da / db_:3.0f} %" if db_ else "  — "
        note = "" if sp in SPORTS_AVEC_SOURCE else "  — aucune source"
        print(f"  {sp[:12]:12} {a:7} / {b:<7} {pct}   {da:9} / {db_:<8} {pct_d}{note}")

    jours = defaultdict(Counter)
    for r, c in classes:
        if c in FOOT_PONT and c != FOOT_ABSENT:
            jours[_coup_d_envoi(r).date()][c] += 1
    print("\nFOOTBALL — LES JOURNÉES DU PONT")
    if not jours:
        print("  Toutes complètes : ce qui manque au football vient de la source ou "
              "du rapprochement.")
    for j in sorted(jours):
        (c, _n), = jours[j].most_common(1)
        print(f"  {j.isoformat()}  {sum(jours[j].values()):4} match(s)  — {c}")

    diags = res["diags"]
    pourquoi, pourquoi_d = Counter(), Counter()
    for r, d in diags:
        pourquoi[d["verdict"]] += 1
        pourquoi_d[d["verdict"]] += r["n_det"] or 0
    if diags:
        sans_ligue = [r for r, _d in diags if not (r["league"] or "")]
        print(f"\nFOOTBALL — CE QUE LA SOURCE AVAIT, pour les {len(diags)} matchs "
              f"des journées complètes")
        print("(la production d'abord — `bind_results` sur le fichier du jour — "
              "puis le candidat le\nplus proche, veille et lendemain compris)")
        for v in ORDRE_POURQUOI:
            if pourquoi[v]:
                print(f"  {pourquoi[v]:5}  ({pourquoi_d[v]:5} dét.)  {v}")
        if sans_ligue:
            connues = sum(1 for r in sans_ligue if r["league_bf"])
            print(f"\n  ⚠️ {len(sans_ligue)} de ces matchs n'ont pas de ligue dans "
                  f"`events` : la classe (féminin,\n  jeunes, réserve) ne peut pas "
                  f"leur être posée. `bet_features` en connaît la ligue pour "
                  f"{connues} ;\n  avec elle, la production en rapprocherait "
                  f"{res['ligue_rendue']} de plus.")
        for v in ORDRE_POURQUOI:
            lot = [(r, d) for r, d in diags if d["verdict"] == v]
            if not lot:
                continue
            print(f"\n── {v} ({len(lot)} matchs, {pourquoi_d[v]} détections)")
            ligues = Counter((r["league"] or f"? ({r['league_bf'] or 'inconnue'})")
                             for r, _d in lot)
            print("   ligues : " + ", ".join(
                f"{lg[:34]} {n}" for lg, n in ligues.most_common(LIGUES_MONTREES)))
            if v == CLASSE:
                cf = Counter(f"{n} → {t}" for _r, d in lot
                             for n, t in d["cand"]["conflits"])
                print("   classes (nous → source) : " + ", ".join(
                    f"{k} : {n}" for k, n in cf.most_common()))
            if v == HORAIRE:
                tr = Counter(_tranche_horaire(d["cand"]["dt"]) for _r, d in lot)
                print("   écart d'horaire : " + ", ".join(
                    f"{k} : {tr[k]}" for k in ("≤ 30 min", "≤ 60 min", "≤ 120 min",
                                               "≤ 360 min", "> 6 h") if tr[k]))
            montres = lot if lister else sorted(
                lot, key=lambda x: -(x[0]["n_det"] or 0))[:EXEMPLES]
            for r, d in montres:
                c = d["cand"]
                print(f"  {_coup_d_envoi(r).strftime('%Y-%m-%d %H:%M')}  "
                      f"{d['nous'][:40]:40}  [{(r['league'] or '?')[:26]}] "
                      f"{r['n_det']} dét.")
                if c is not None and v != ABSENT:
                    ecart = ("" if c["dt"] == float("inf")
                             else f" · Δ {c['dt']:.0f} min")
                    print(f"      source : {c['nom'][:44]:44} [{c['ligue'][:24]}, "
                          f"{c['statut']}] · noms {c['u']:.0f} / avec classes "
                          f"{c['g']:.0f}{ecart}")
            if not lister and len(lot) > EXEMPLES:
                print(f"  … et {len(lot) - EXEMPLES} autres (--lister pour tout voir)")

    tennis = [(r, c) for r, c in classes if c in (DOUBLE, TENNIS_SIMPLE)]
    if tennis:
        print("\nTENNIS — SANS RÉSULTAT")
        for c in (TENNIS_SIMPLE, DOUBLE):
            lot = [r for r, cc in tennis if cc == c]
            if lot:
                ligues = Counter((r["league"] or "?") for r in lot)
                print(f"  {len(lot):5}  {c}\n         ligues : " + ", ".join(
                    f"{lg[:30]} {n}" for lg, n in ligues.most_common(LIGUES_MONTREES)))
    autres = [r for r, c in classes if c == AUTRE_CLE]
    if autres:
        print(f"\nRÉSULTAT SOUS UNE AUTRE CLÉ — {len(autres)} match(s), "
              f"{dets[AUTRE_CLE]} détections")
        for r in sorted(autres, key=lambda r: -(r["n_det"] or 0))[:EXEMPLES]:
            print(f"  {r['event_key'][:60]:60}  {r['n_det']} dét.")
    sans_src = Counter((r["sport"] or "?") for r, c in classes if c == SANS_SOURCE)
    if sans_src:
        print("\nSPORTS SANS SOURCE : " + ", ".join(
            f"{sp} {n}" for sp, n in sans_src.most_common()))

    _conseils(classes, comptes, pourquoi, dossier, jours_pont, maintenant, db,
              "match(s)")


def _conseils(classes, comptes, pourquoi, dossier: Path, jours_pont: int,
              maintenant: datetime, db: str, u: str) -> None:
    """Ce qu'il faut faire, dans l'ordre où il faut le faire — pour les
    paris joués comme pour les détections ; `u` nomme l'unité comptée."""
    print("\nQUE FAIRE, DANS CET ORDRE")
    manques = [(r, c) for r, c in classes if c not in NORMAUX]
    # UN `--days`, calculé sur le plus vieux match qui manque : c'est ce que
    # `results-update` doit couvrir, et rien de plus petit ne le règle.
    n_jours = jours_a_couvrir(maintenant, [_coup_d_envoi(r) for r, _c in manques])
    n_tennis = jours_a_couvrir(maintenant, [_coup_d_envoi(r) for r, c in manques
                                            if c in (TENNIS, TENNIS_SIMPLE)])
    maj = (f"      .venv/bin/python -m src.main results-update --days {n_jours} "
           f"--sport soccer,tennis\n"
           f"      .venv/bin/python -m src.main track-update")

    # 1. La configuration d'abord : sans source, aucun autre geste n'aboutit.
    if comptes[FOOT_SANS_PONT]:
        print(f"  • {comptes[FOOT_SANS_PONT]} {u} de football jugés par "
              f"l'API en direct, que la VM ne peut pas appeler :\n"
              f"    mets SCORES_FOOTBALL_BRIDGE=1 dans .env —\n"
              f"      sed -i '/^SCORES_FOOTBALL_BRIDGE=/d' .env && echo "
              f"'SCORES_FOOTBALL_BRIDGE=1' >> .env\n"
              f"    puis relance cette sonde : elle jugera alors les fichiers "
              f"du pont.")
    if comptes[TENNIS_SANS_CLE]:
        print(f"  • {comptes[TENNIS_SANS_CLE]} {u} de tennis sans source : "
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
        print(f"  • {comptes[REFUSEE]} {u} sur {len(jours_refus)} journée(s) "
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
    if pourquoi[AMBIGU]:
        print(f"  • {pourquoi[AMBIGU]} match(s) que deux matchs de la source "
              f"revendiquent presque à égalité : le\n    rapprochement refuse de "
              f"choisir, et c'est voulu — un mauvais choix réglerait des paris "
              f"faux.")
    if pourquoi[INEXPLIQUE]:
        print(f"  • {pourquoi[INEXPLIQUE]} match(s) où la sonde et la production "
              f"se contredisent : envoie la section\n    ci-dessus — c'est un "
              f"défaut de la sonde ou du rapprochement, et il faut savoir "
              f"lequel.")
    if pourquoi[CLASSE] or pourquoi[NOMS] or pourquoi[HORAIRE]:
        n = pourquoi[CLASSE] + pourquoi[NOMS] + pourquoi[HORAIRE]
        print(f"  • {n} match(s) que la source A, mais que le rapprochement "
              f"rejette (classe, noms ou\n    horaire) : c'est corrigeable dans "
              f"le code. Envoie la section ci-dessus — chaque\n    candidat est "
              f"à confirmer à l'œil avant de toucher aux règles.")
    if pourquoi[SANS_SCORE_SOURCE]:
        print(f"  • {pourquoi[SANS_SCORE_SOURCE]} match(s) terminés dont la source "
              f"ne donnait pas le score à la capture :\n    la journée est "
              f"définitive, le pont ne la redemandera pas. Rien à régler.")
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

    if comptes[TENNIS_SIMPLE]:
        print(f"  • {comptes[TENNIS_SIMPLE]} match(s) de tennis en SIMPLE sans "
              f"résultat : relancer\n"
              f"      .venv/bin/python -m src.main results-update --days "
              f"{n_tennis} --sport tennis\n"
              f"    Ce qui reste ensuite est surtout des ABANDONS et des forfaits, "
              f"que la source ne règle pas.")
    if comptes[DOUBLE]:
        print(f"  • {comptes[DOUBLE]} match(s) de DOUBLE : la source tennis n'en "
              f"sert aucun. Rien à relancer —\n    seule une autre source les "
              f"réglerait.")
    if comptes[AUTRE_CLE]:
        print(f"  • {comptes[AUTRE_CLE]} match(s) ont leur résultat sous une AUTRE "
              f"clé du même match : l'horaire\n    a été révisé au-delà de la "
              f"tolérance (10 min au football, 12 h au tennis), et\n    "
              f"results-update ne relie pas la clé orpheline. Le dashboard lit le "
              f"résultat sur la\n    clé de la MEILLEURE COTE : quand c'est "
              f"l'orpheline, l'opportunité reste non réglée\n    alors que le "
              f"score est connu. Corrigeable dans le code — à décider, relancer "
              f"ne sert à rien.")
    if comptes[SANS_SOURCE] and u != "pari(s)":
        print(f"  • {comptes[SANS_SOURCE]} match(s) d'un sport sans source de "
              f"résultats : leur ROI n'est pas\n    mesurable. Rien à corriger "
              f"ici.")

    # 4. Les lignes abîmées en base.
    if comptes[SANS_EVENTS]:
        print(f"  • {comptes[SANS_EVENTS]} {u} sans ligne `events`. La "
              f"réparation écrit sport = « unknown »,\n    que results-update "
              f"ignore : il faut donc remettre le sport du clic ensuite —\n"
              f"      .venv/bin/python -m scripts.repair_events --apply\n"
              f"      {_commande_sport(db)}")
    if comptes[SPORT_INCONNU] and u == "pari(s)":
        print(f"  • {comptes[SPORT_INCONNU]} pari(s) dont la ligne `events` n'a "
              f"pas de sport : remets celui du clic —\n"
              f"      {_commande_sport(db)}")
    elif comptes[SPORT_INCONNU]:
        print(f"  • {comptes[SPORT_INCONNU]} match(s) dont la ligne `events` n'a "
              f"pas de sport (« unknown ») :\n    results-update ne les réclame "
              f"pas. Sans clic, rien en base ne dit leur sport — à laisser.")
    if comptes[SANS_EVENTS] or (comptes[SPORT_INCONNU] and u == "pari(s)"):
        print(f"    puis :\n{maj}")
    if comptes[NON_RATTACHE]:
        print(f"  • {comptes[NON_RATTACHE]} clic(s) jamais rattaché(s) à leur "
              f"value bet (marché inconnu) :\n"
              f"      .venv/bin/python -m src.main backfill-played-bets")


def _entier(brut, defaut: int) -> int:
    """Un réglage entier de `.env`, commentaire en ligne toléré.

    `.env.example` écrit `SCORES_BRIDGE_DAYS=3          # journées…`, et
    `load_env_file` ne retire pas le commentaire : un `int()` nu lèverait une
    trace au lieu de lire 3."""
    m = re.match(r"\s*(\d+)", brut or "")
    return int(m.group(1)) if m else defaut


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Les DÉTECTIONS sans résultat — ou, avec --joues, tes paris "
                    "joués : combien, et pourquoi chacun (journée non récupérée "
                    "par le pont, refusée par la source, match absent, tennis, "
                    "etc.). Lecture seule.")
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--depuis", default=None, metavar="AAAA-MM-JJ",
                    help="Premier jour de match inclus (UTC). Défaut : il y a "
                         "10 jours.")
    ap.add_argument("--joues", action="store_true",
                    help="Tes paris JOUÉS (clics sur « Jouer ») au lieu de "
                         "toutes les détections.")
    ap.add_argument("--lister", action="store_true",
                    help="Tout nommer, au lieu des exemples.")
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
    if a.joues:
        rows, noms = charger(a.db, depuis)
        res = analyser(rows, maintenant, dossier, jours_pont, final_apres,
                       pont_actif, cle_tennis)
        imprimer(res, depuis, dossier, jours_pont, source_jours, maintenant,
                 a.lister, noms, a.db, pont_actif, cle_tennis)
        return

    rows, noms, reglees = charger_detections(a.db, depuis)

    # Des milliers de matchs à diagnostiquer prennent du temps : une sonde
    # muette ressemble à une sonde bloquée. La progression va sur stderr, pour
    # ne pas se mêler au rapport qu'on redirige dans un fichier.
    def progres(i, n):
        print(f"  … {i}/{n} matchs de football diagnostiqués", file=sys.stderr)
    res = analyser_detections(rows, maintenant, dossier, jours_pont, final_apres,
                              pont_actif, cle_tennis, reglees, noms, progres)
    imprimer_detections(res, depuis, dossier, jours_pont, source_jours,
                        maintenant, a.lister, a.db, pont_actif, cle_tennis)


if __name__ == "__main__":
    main()
