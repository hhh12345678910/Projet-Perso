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
TROP_TOT_REFUSEE = "journée capturée trop tôt, puis REFUSÉE à la reprise — ne sera plus reprise"
FICHIER_ILLISIBLE = "fichier du pont ILLISIBLE — results-update tombe en panne sur TOUT le football"
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
AUTRE_CLE = ("résultat connu sous une AUTRE clé du même match (horaire révisé "
             "au-delà de la tolérance)")
TENNIS_RELIER = ("tennis, résultat connu sous une autre clé à moins de 12 h — "
                 "un results-update tennis le reliera")
DOUBLE = "tennis, double — la source les sert, notre lecteur les écarte (noms de paires mutilés)"
TENNIS_SIMPLE = "tennis, simple sans résultat — abandon, forfait, ou absent de la source"
#: Posée APRÈS le contrôle de production, sur un match que les fichiers du pont
#: permettent déjà de régler : il ne manque qu'un passage de results-update.
APPARIABLE = "présent et appariable — relancer results-update"

#: L'ordre d'impression : ce qui est normal d'abord, puis ce qui demande un
#: geste, du plus fréquent attendu au plus rare.
ORDRE = [REGLE, A_VENIR, FOOT_DU_JOUR, FOOT_SANS_PONT, FICHIER_ILLISIBLE,
         JAMAIS_DEMANDEE, HORS_FENETRE, REFUSEE, TROP_TOT, TROP_TOT_HORS,
         TROP_TOT_REFUSEE, APPARIABLE, AUTRE_CLE, FOOT_ABSENT, TENNIS,
         TENNIS_SANS_CLE, SANS_EVENTS, SPORT_INCONNU, SANS_SOURCE, NON_RATTACHE,
         MARCHE, INEXPLOITABLE]
NORMAUX = {REGLE, A_VENIR, FOOT_DU_JOUR}
ORDRE_DET = [REGLE, REGLE_SANS_SCORE, A_VENIR, FOOT_DU_JOUR, FOOT_SANS_PONT,
             FICHIER_ILLISIBLE, JAMAIS_DEMANDEE, HORS_FENETRE, REFUSEE, TROP_TOT,
             TROP_TOT_HORS, TROP_TOT_REFUSEE, APPARIABLE, AUTRE_CLE, FOOT_ABSENT,
             TENNIS_RELIER, DOUBLE, TENNIS_SIMPLE, TENNIS_SANS_CLE, SPORT_INCONNU,
             SANS_SOURCE]
NORMAUX_DET = {REGLE, REGLE_SANS_SCORE, A_VENIR, FOOT_DU_JOUR}
#: Les causes qui tiennent à la JOURNÉE du pont football.
FOOT_PONT = (JAMAIS_DEMANDEE, HORS_FENETRE, REFUSEE, TROP_TOT, TROP_TOT_HORS,
             TROP_TOT_REFUSEE, FOOT_ABSENT)
#: Celles où un fichier du pont existe (le sien ou un voisin) et que la
#: production pourrait donc déjà régler : on le vérifie avant de conclure.
A_VERIFIER = FOOT_PONT
#: Les causes qu'un passage de `results-update` peut régler — les seules qui
#: comptent pour le `--days` conseillé. Un match qu'aucune relance ne règlera
#: (source absente, sport sans source…) n'a pas à élargir la fenêtre.
RELANCABLES = {JAMAIS_DEMANDEE, HORS_FENETRE, REFUSEE, TROP_TOT, TROP_TOT_HORS,
               TROP_TOT_REFUSEE, APPARIABLE, SANS_EVENTS, SPORT_INCONNU}

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


def lisible(f: Path, memo: "dict | None" = None) -> bool:
    """La production saura-t-elle lire ce fichier ? `BridgedFootballScores`
    lève sur un JSON illisible, et `results-update` met alors TOUT le football
    en panne — pas seulement cette journée. On rejoue exactement sa lecture :
    `json.loads`, puis `parse_apifootball_results`."""
    if memo is not None and f in memo:
        return memo[f]
    try:
        parse_apifootball_results(json.loads(f.read_text(encoding="utf-8")))
        ok = True
    except Exception:                                         # noqa: BLE001
        ok = False
    if memo is not None:
        memo[f] = ok
    return ok


def etat_journee(jour: date, dossier: Path, maintenant: datetime,
                 jours_pont: int, final_apres: int,
                 memo: "dict | None" = None) -> str:
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
    if not lisible(f, memo):
        return FICHIER_ILLISIBLE
    fin = datetime.combine(jour + timedelta(days=1), datetime.min.time(),
                           tzinfo=timezone.utc).timestamp()
    if f.stat().st_mtime < fin + final_apres:
        # Deux raisons distinctes de ne plus être reprise, deux gestes
        # distincts : la pierre tombale s'efface (abonnement payant), la
        # fenêtre s'élargit.
        if tombe:
            return TROP_TOT_REFUSEE
        return TROP_TOT if dans_fenetre else TROP_TOT_HORS
    return FOOT_ABSENT


def classer(r, maintenant: datetime, dossier: Path, jours_pont: int,
            final_apres: int, pont_actif: bool = True,
            cle_tennis: bool = True, memo: "dict | None" = None) -> str:
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
    return etat_journee(jour, dossier, maintenant, jours_pont, final_apres,
                        None if memo is None else memo.setdefault("lisible", {}))


# ── Pourquoi un match de football n'a pas été rapproché ─────────────
#
# Une journée COMPLÈTE sans résultat pour un match laisse quatre coupables
# possibles, et ils appellent quatre gestes différents. Le fichier du pont est
# sur disque : on y cherche le match le plus proche et on dit lequel.

ABSENT = "absent de la source ce jour-là (aucun candidat plausible)"
CLASSE = "présent, mais la barrière de CLASSE le rejette (réserve / jeunes / féminin)"
NOMS = "candidat aux noms proches — à vérifier à l'œil"
HORAIRE = "présent, mais horaire décalé au-delà de la tolérance"
STATUT = "présent, mais pas terminé normalement (reporté, annulé, arrêté…)"
PROLONG = "présent, allé en prolongation — la source ne prouve pas le score à 90 min"
VOISIN = ("présent dans le fichier de la veille ou du lendemain, que "
          "results-update ne lira pas (aucun autre match à régler ce jour-là)")
AMBIGU = ("présent, mais un autre match de la source lui ressemble trop — "
          "le rapprochement refuse de choisir")
SANS_SCORE_SOURCE = "présent et terminé, mais la source ne donne pas son score"
ORIENTATION = ("présent et apparié, mais le sens des camps est indécidable "
               "(noms emboîtés) — laissé sans résultat, volontairement")
INEXPLIQUE = ("présent et appariable selon la sonde, pourtant la production "
              "ne le lie pas — à signaler")

ORDRE_POURQUOI = [ABSENT, CLASSE, NOMS, HORAIRE, STATUT, PROLONG,
                  SANS_SCORE_SOURCE, VOISIN, AMBIGU, ORIENTATION, INEXPLIQUE,
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
#: Le score de production (`max(token_set_ratio, partial_ratio)`) juge un nom
#: sur son meilleur FRAGMENT : « Bra » vaut 100 contre « SK Brann W », « Os »
#: 100 contre « Kossa FC ». Dans la tolérance horaire, l'heure tient ces faux
#: amis à distance ; au-delà, plus rien ne les retient. Hors tolérance, la
#: sonde exige donc que chaque camp ressemble à l'autre EN ENTIER
#: (`fuzz.ratio`) — et au-delà de six heures, qu'il soit quasi identique :
#: seul un match reporté garde ses deux noms à l'identique. Le camp
#: « identique » d'un club renommé se juge de la même façon, en entier.
#: Constaté sur la sortie réelle du 26/09 : « SK Brann W - Aalesunds W » tenu
#: pour « Bra - Fezzanese », « Kossa FC - Marist Fire » pour « Os - Førde » à
#: 11 h d'écart.
ENTIER_HORS_TOLERANCE = 60.0
FENETRE_LOINTAINE_MIN = 360.0
ENTIER_LOINTAIN = 90.0


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


def _plausible(u: float, camps: tuple, dt: float,
               entiers: tuple = (100.0, 100.0)) -> bool:
    """Ce candidat peut-il être NOTRE match ? Voir `FENETRE_NOMS_MIN` et
    `ENTIER_HORS_TOLERANCE`. `entiers` : `fuzz.ratio` camp par camp, dans la
    même orientation que `camps`."""
    if u < PLANCHER_CANDIDAT:
        return False
    if dt > FENETRE_LOINTAINE_MIN and min(entiers) < ENTIER_LOINTAIN:
        return False
    if dt > tolerance_for_scores("soccer") and min(entiers) < ENTIER_HORS_TOLERANCE:
        return False
    if u >= SEUIL_APPARIEMENT:
        return True
    if dt > FENETRE_NOMS_MIN:
        return False
    return max(entiers) >= COTE_IDENTIQUE or min(camps) >= COTES_PROCHES


#: Un candidat plausible a une moyenne d'au moins `PLANCHER_CANDIDAT` (70),
#: donc au moins UN camp à 70 ou plus. La recherche part de là : les noms de
#: la source à 70 et plus face à l'un des nôtres désignent les seuls matchs
#: qui peuvent être plausibles, et les scores exacts ne sont calculés que pour
#: eux. Assez rapide pour des milliers de matchs, sans changer un verdict.
SEUIL_BRUT = PLANCHER_CANDIDAT


class SourceFoot:
    """Les fichiers du pont football, lus une fois, préparés une fois — et
    OUBLIÉS dès qu'on a fini avec eux.

    Deux lectures d'un même fichier, pour deux questions différentes :

    * `resultats(j)` — la liste EXACTE que `results-update` rapproche, passée
      par `parse_apifootball_results` : seulement les matchs terminés (FT, ou
      prolongation au score à 90 min prouvé), classe de la ligue reportée ;
    * `fenetre(j)` — tous les matchs servis la veille, le jour et le lendemain,
      terminés ou non, pour dire POURQUOI un match n'a pas été rapproché.

    ⚠️ LA MÉMOIRE. Une journée d'API-Football, c'est 1 000 à 2 000 matchs
    d'environ 1 Ko de JSON chacun, soit une dizaine de Mo une fois en objets
    Python. Garder quatre mois ouverts à la fois pèserait plus d'un Go, sur la
    VM qui fait tourner le daemon. Le JSON brut n'est donc jamais gardé (on en
    tire les résultats et les champs utiles, puis on le lâche), et `oublier`
    libère les journées qu'on a dépassées — la sonde parcourt les matchs dans
    l'ordre chronologique, trois journées suffisent à chaque instant."""

    def __init__(self, dossier: Path):
        self.dossier = dossier
        self._res: dict = {}
        self._prep: dict = {}
        self._fen: dict = {}
        self._proches: dict = {}

    def _charger(self, d: date) -> None:
        f = self.dossier / f"{d.isoformat()}.json"
        try:
            brut = json.loads(f.read_text()).get("response") or []
        except (OSError, ValueError, AttributeError):
            brut = []
        self._res[d] = parse_apifootball_results({"response": brut})[0]
        out = []
        for m in brut:
            fx, eq, lg = (m.get("fixture") or {}, m.get("teams") or {},
                          m.get("league") or {})
            m2 = class_marker_from_league(lg.get("name") or "")
            th = with_class_marker(((eq.get("home") or {}).get("name") or "").strip(), m2)
            ta = with_class_marker(((eq.get("away") or {}).get("name") or "").strip(), m2)
            if not th or not ta:
                continue
            sc = m.get("score") or {}
            ft, et = sc.get("fulltime") or {}, sc.get("extratime") or {}
            buts = m.get("goals") or {}
            out.append({"jour_fichier": d, "th": th, "ta": ta,
                        "sh": _pour_flou(th), "sa": _pour_flou(ta),
                        "t": _instant(fx.get("date")),
                        "ligue": lg.get("name") or "?",
                        "statut": ((fx.get("status") or {}).get("short")
                                   or "?").upper(),
                        "a_90": _score_90_minutes(m) is not None,
                        "a_score": (ft.get("home") is not None
                                    and ft.get("away") is not None),
                        # Pour les prolongations NON prouvables : la forme de
                        # ce que la source a donné, pour chiffrer une règle.
                        "prolong": _forme_prolongation(
                            ((fx.get("status") or {}).get("short") or "").upper(),
                            ft, et, buts)})
        self._prep[d] = out

    def resultats(self, d: date) -> list:
        if d not in self._res:
            self._charger(d)
        return self._res[d]

    def _prepares(self, d: date) -> list:
        if d not in self._prep:
            self._charger(d)
        return self._prep[d]

    def fenetre(self, jour: date) -> tuple:
        """(matchs de la veille, du jour et du lendemain ; noms distincts ;
        index nom → matchs qui le portent).

        Les voisins comptent : un match à 23 h 30 chez nous peut être daté du
        lendemain chez la source, et le chercher dans un seul fichier le
        déclarerait absent à tort."""
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

    def oublier(self, avant: date) -> None:
        """Lâcher tout ce qui concerne les journées antérieures à `avant`."""
        for cache in (self._res, self._prep, self._fen):
            for d in [d for d in cache if d < avant]:
                del cache[d]
        for cle in [c for c in self._proches if c[1] < avant]:
            del self._proches[cle]

    def journees_ouvertes(self) -> int:
        return len(self._prep)


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


#: Les formes d'une prolongation que `_score_90_minutes` ne sait pas prouver.
TAB_DIRECTS = "tirs au but directs — pas de prolongation saisie, buts = score réglementaire"
PROLONG_SANS_DETAIL = "prolongation sans détail exploitable"


def _forme_prolongation(statut: str, ft: dict, et: dict, buts: dict) -> str:
    """Ce que la source a donné pour un AET/PEN, en une étiquette.

    `TAB_DIRECTS` : un PEN sans prolongation saisie (`extratime` vide) et des
    buts égaux au temps réglementaire — la forme d'un match allé DIRECTEMENT aux
    tirs au but (Coupe de la Ligue anglaise, Copa Argentina, MLS Next Pro…).
    Jamais un AET : décidé en prolongation, il en a joué une.
    `_score_90_minutes` exige une prolongation chiffrée et l'écarte ; si la
    source dit vrai, son score à 90 min est `fulltime`. La sonde le COMPTE, elle
    ne le décide pas : c'est une règle de production à trancher sur ce chiffre."""
    vals = (ft.get("home"), ft.get("away"), buts.get("home"), buts.get("away"))
    if (statut == "PEN" and et.get("home") is None and et.get("away") is None
            and None not in vals and vals[:2] == vals[2:]):
        return TAB_DIRECTS
    return PROLONG_SANS_DETAIL


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


def _resultats_production(source: "SourceFoot", jour: date,
                          jours: "set | None") -> list:
    """Les résultats contre lesquels `results-update` rapproche un match du
    jour `jour`.

    ⚠️ Pas le seul fichier de ce jour. `results-update` charge le fichier de
    CHAQUE journée qui a un match de football en attente dans sa fenêtre
    (`days_needed`), met tous leurs résultats dans un même lot, et rapproche
    chaque match contre ce lot (src/main.py, `fetched.extend` puis
    `bind_results(events, fetched)`). La tolérance horaire (10 min) ne laisse
    passer qu'un voisin immédiat : la veille ou le lendemain, s'ils sont
    chargés. `jours` : les journées chargées ; `None` = toutes (le cas réel,
    où chaque journée a au moins un match en attente)."""
    return [x for d in (jour - timedelta(days=1), jour, jour + timedelta(days=1))
            if d == jour or jours is None or d in jours
            for x in source.resultats(d)]


def lie_en_production(r, noms: dict, source: "SourceFoot",
                      jours: "set | None" = None) -> "str | None":
    """Ce que `results-update` ferait de CE match avec les fichiers présents :
    "lie" s'il le rapproche, "orientation" s'il l'apparie mais ne sait pas
    dans quel sens le lire (`scores._orientation`), "ambigu" si seule sa garde
    d'ambiguïté l'en empêche, None sinon. Même construction du match (`_notre_evenement`),
    même lot de résultats (`_resultats_production`), même `bind_results`."""
    ev = _notre_evenement(r, noms)
    if ev.start_time is None:
        return None
    lot = _resultats_production(source, ev.start_time.date(), jours)
    liens, compteurs = bind_results([ev], lot, sport="soccer")
    if liens:
        return "lie"
    if compteurs.get("orientation_indecidable"):
        return "orientation"
    # La garde d'ambiguïté de `match_event` : deux résultats presque aussi
    # bons, et la production refuse de choisir. Sans marge, elle aurait lié.
    marque = class_marker_from_league(ev.league)
    evm = replace(ev, home=with_class_marker(ev.home, marque),
                  away=with_class_marker(ev.away, marque))
    if match_event(evm, lot, time_tolerance_minutes=tolerance_for_scores("soccer"),
                   min_score=SEUIL_APPARIEMENT, ambiguity_margin=0) is not None:
        return "ambigu"
    return None


def diagnostiquer(r, noms: dict, dossier: Path, cache: dict,
                  jours: "set | None" = None, production: "str | None" = "?") -> dict:
    """Le match de la source le plus proche de CE match, et le verdict.

    D'abord la PRODUCTION elle-même (`lie_en_production`) : s'il lie, le match
    est appariable et rien d'autre ne compte. Sinon, on cherche dans les trois
    fichiers le candidat le plus proche, SANS la barrière de classe, pour dire
    ce qui a bloqué. Une sonde qui jugerait autrement que la production
    mentirait (§17.7) ; et si les deux se contredisent, elle le dit
    (`INEXPLIQUE`) au lieu de trancher.

    `cache` : un dict partagé d'un appel à l'autre, qui garde les fichiers lus.
    `jours` : les journées que `results-update` chargera (voir
    `_resultats_production`). `production` : le verdict de
    `lie_en_production` s'il est déjà connu — "?" pour le calculer ici."""
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
        entiers = ((fuzz.ratio(oh, x["sh"]), fuzz.ratio(oa, x["sa"])) if camps is droit
                   else (fuzz.ratio(oh, x["sa"]), fuzz.ratio(oa, x["sh"])))
        u = sum(camps) / 2
        dt = abs((x["t"] - depart).total_seconds()) / 60 if x["t"] else float("inf")
        # Un candidat plausible passe devant un bruit mieux noté : sinon un
        # « Juventus - Atalanta » à 80 masquerait le club renommé à 75.
        plausible = _plausible(u, camps, dt, entiers)
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
                "a_90": x["a_90"], "a_score": x["a_score"],
                "prolong": x["prolong"],
                # Les classes camp par camp, dans l'orientation retenue : c'est
                # ce qui dit QUELLE correction lèverait une barrière de classe.
                "conflits": _conflits(h, a, x, meilleur["droit"])}

    if production == "?":
        production = lie_en_production(r, noms, source, jours)
    if production == "lie":
        return {"verdict": APPARIABLE, "nous": nous, "cand": cand}
    if production == "ambigu":
        return {"verdict": AMBIGU, "nous": nous, "cand": cand}
    if production == "orientation":
        return {"verdict": ORIENTATION, "nous": nous, "cand": cand}

    if cand is None or not cand["plausible"]:
        return {"verdict": ABSENT, "nous": nous, "cand": cand}
    if cand["g"] < SEUIL_APPARIEMENT <= cand["u"]:
        verdict = CLASSE
    elif cand["g"] < SEUIL_APPARIEMENT:
        verdict = NOMS
    elif cand["dt"] > tol:
        verdict = HORAIRE
    elif cand["statut"] == "FT" and not cand["a_score"]:
        # `parse_apifootball_results` l'écarte (`score_manquant`) : sans score,
        # rien à régler, et ce n'est pas une contradiction avec la production.
        verdict = SANS_SCORE_SOURCE
    # Une prolongation dont le score à 90 min est PROUVÉ se règle comme un FT
    # (`parse_apifootball_results`) : 402 des 409 cas exploitables en base.
    elif cand["statut"] == "FT" or (cand["statut"] in ("AET", "PEN")
                                    and cand["a_90"]):
        # La production n'a PAS lié ce match (vérifié plus haut). Trouvé dans
        # un fichier voisin qu'elle ne chargera pas, c'est attendu ; sinon,
        # c'est une contradiction entre la sonde et la production — à
        # signaler, pas à arbitrer ici.
        charge = (cand["jour_fichier"] == depart.date() or jours is None
                  or cand["jour_fichier"] in jours)
        verdict = INEXPLIQUE if charge else VOISIN
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


def jours_reclames_base(db: str, depuis: date, maintenant: datetime) -> set:
    """Les journées dont `results-update` chargera le fichier football : celles
    qui ont au moins un match EN ATTENTE — toutes détections confondues, pas
    seulement les paris joués (`events_awaiting_result`)."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        lignes = con.execute("""
            SELECT DISTINCT e.start_time FROM events e
            WHERE e.sport = 'soccer' AND e.start_time >= ? AND e.start_time < ?
              AND EXISTS (SELECT 1 FROM value_bets vb WHERE vb.event_key = e.event_key)
              AND NOT EXISTS (SELECT 1 FROM results r WHERE r.event_key = e.event_key)
        """, ((datetime.combine(depuis, datetime.min.time(), tzinfo=timezone.utc)
               - timedelta(days=1)).isoformat(),
              (maintenant - GRACE).isoformat())).fetchall()
    except sqlite3.OperationalError:
        return set()
    finally:
        con.close()
    return {t.date() for (brut,) in lignes for t in [_instant(brut)] if t}


def analyser(rows: list, maintenant: datetime, dossier: Path, jours_pont: int,
             final_apres: int, pont_actif: bool = True,
             cle_tennis: bool = True, noms: "dict | None" = None,
             jours: "set | None" = None, reglees: "dict | None" = None) -> dict:
    memo: dict = {}
    classes = [(r, classer(r, maintenant, dossier, jours_pont, final_apres,
                           pont_actif, cle_tennis, memo))
               for r in rows]
    classes, diags, _l = verifier_production(classes, noms or {}, dossier, jours,
                                             reglees)
    return {"classes": classes, "diags": diags,
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


#: Pour les DÉTECTIONS : le sport vu à la détection (`bet_features`), sinon
#: celui d'un clic. Ne remplit QUE les lignes sans sport ; n'écrase rien.
SQL_SPORT_DET = ("UPDATE events SET sport = COALESCE((SELECT bf.sport FROM "
                 "bet_features bf WHERE bf.event_key = events.event_key AND "
                 "COALESCE(bf.sport,'') NOT IN ('','unknown') LIMIT 1), (SELECT "
                 "pb.sport FROM played_bets pb WHERE pb.event_key = "
                 "events.event_key AND COALESCE(pb.sport,'') NOT IN "
                 "('','unknown') LIMIT 1), sport) WHERE COALESCE(sport,'') IN "
                 "('','unknown')")


def _commande_sport(db: str, sql: str = SQL_SPORT) -> str:
    """La commande à coller dans le shell : le SQL entre `\\"`, parce que
    l'argument de `-c` est lui-même entre guillemets doubles."""
    return (".venv/bin/python -c \"import sqlite3; "
            f"c = sqlite3.connect('{db}'); "
            f"n = c.execute(\\\"{sql}\\\").rowcount; c.commit(); "
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
    diags = res.get("diags") or []
    if diags:
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
    noms affichés ; matchs qui ONT un résultat, par « équipes + jour + ligue »).

    Le troisième élément sert à reconnaître un match réglé sous une autre
    clé : une révision d'horaire crée une clé neuve (§17.8). La LIGUE en fait
    partie : sans elle, un match féminin ou de jeunes — dont la classe ne vit
    que dans la ligue, les noms compactés étant les mêmes — passerait pour le
    match des seniors."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    borne = datetime.combine(depuis, datetime.min.time(),
                             tzinfo=timezone.utc).isoformat()
    # `bet_features` porte la ligue et le sport vus à la détection ; `events`
    # peut avoir perdu l'une ou l'autre (`upsert_events` complète une ligue
    # vide, mais seulement si une cote plus tardive la porte ; la ligue n'est
    # collectée que depuis le 01/08). La production ne lit QUE `events` —
    # `bet_features` et `played_bets` servent à MESURER ce qu'une réparation
    # rapporterait, jamais à juger.
    #
    # Les détections comptées sont celles que `settle` sait régler (1X2 et
    # totaux) : une mi-temps n'est jamais réglée, la compter gonflerait le
    # dénominateur de la couverture.
    requete = """
        SELECT e.event_key, e.sport, e.league, e.home, e.away, e.start_time,
               v.n_h2h + v.n_tot AS n_det, v.n_h2h AS n_h2h,
               (r.event_key IS NOT NULL) AS has_result,
               (r.home_score IS NOT NULL AND r.away_score IS NOT NULL) AS has_scores,
               {ligue_bf} AS league_bf, {sport_bf} AS sport_bf,
               {sport_pb} AS sport_pb
        FROM (SELECT event_key,
                     SUM(market = 'h2h') AS n_h2h,
                     SUM(market = 'totals') AS n_tot
              FROM value_bets GROUP BY event_key) v
        JOIN events e       ON e.event_key = v.event_key
        LEFT JOIN results r ON r.event_key = e.event_key
        WHERE e.start_time >= ?
    """
    def colonne(table, champ):
        return f"""(SELECT x.{champ} FROM {table} x
                    WHERE x.event_key = e.event_key
                      AND COALESCE(x.{champ}, '') NOT IN ('', 'unknown')
                    LIMIT 1)"""
    options = {"ligue_bf": colonne("bet_features", "league"),
               "sport_bf": colonne("bet_features", "sport"),
               "sport_pb": colonne("played_bets", "sport")}
    tables = {n for (n,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    for cle, table in (("ligue_bf", "bet_features"), ("sport_bf", "bet_features"),
                       ("sport_pb", "played_bets")):
        if table not in tables:
            options[cle] = "NULL"
    rows = [dict(r) for r in con.execute(requete.format(**options), (borne,))]
    try:
        noms = {n: d for n, d in con.execute(
            "SELECT normalized_name, display_name FROM teams")}
    except sqlite3.OperationalError:
        noms = {}
    con.close()
    return rows, noms, charger_reglees(db, depuis)


def charger_reglees(db: str, depuis: date) -> dict:
    """Les matchs qui ONT un résultat, par (équipes, jour UTC, ligue) → leurs
    coups d'envoi. Seulement ceux dont la ligue est connue : voir
    `ecart_cle_voisine`."""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    reglees: dict = defaultdict(list)
    borne = (datetime.combine(depuis, datetime.min.time(), tzinfo=timezone.utc)
             - timedelta(days=1)).isoformat()
    try:
        for h, a, t, lg in con.execute("""
                SELECT lower(e.home), lower(e.away), e.start_time, e.league
                FROM results r JOIN events e ON e.event_key = r.event_key
                WHERE e.start_time >= ? AND COALESCE(e.league, '') <> ''""",
                (borne,)):
            instant = _instant(t)
            if instant is not None:
                reglees[(h, a, instant.date(), lg)].append(instant)
    finally:
        con.close()
    return dict(reglees)


def ecart_cle_voisine(r, reglees: dict) -> "float | None":
    """L'écart, en minutes, avec la plus proche AUTRE clé du même match qui a
    un résultat : mêmes équipes (dans un sens ou dans l'autre), même jour UTC,
    même ligue NON vide. None s'il n'y en a pas.

    Sans ligue, on ne sait pas si deux clés aux mêmes noms compactés sont le
    même match ou un match féminin face au masculin : on ne conclut pas."""
    t = _coup_d_envoi(r)
    lg = r["league"] or ""
    h, a = (r["home"] or "").lower(), (r["away"] or "").lower()
    if t is None or not (h and a and lg):
        return None
    instants = (reglees.get((h, a, t.date(), lg), [])
                + reglees.get((a, h, t.date(), lg), []))
    if not instants:
        return None
    return min(abs((x - t).total_seconds()) / 60 for x in instants)


def _est_double(r, noms: dict) -> bool:
    """Une paire de double s'écrit « Bolelli S / Vavassori A » (matcher.py) —
    lisible sur le nom AFFICHÉ, pas sur la clé compactée."""
    return any("/" in (noms.get(k or "") or k or "")
               for k in (r["home"], r["away"]))


def classer_detection(r, maintenant: datetime, dossier: Path, jours_pont: int,
                      final_apres: int, pont_actif: bool, cle_tennis: bool,
                      reglees: dict, noms: dict, memo: dict) -> str:
    """La raison pour laquelle CE match détecté a — ou n'a pas — son résultat,
    AVANT le contrôle de production (`analyser_detections` le fait ensuite).
    `memo` garde l'état de chaque journée du pont (et la lisibilité de chaque
    fichier) : ils ne changent pas d'un match à l'autre."""
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
    if sport == "tennis":
        if not cle_tennis:
            return TENNIS_SANS_CLE
        # Pas de fichier au tennis : la seule preuve est le résultat d'une clé
        # voisine. À moins de 12 h (la tolérance tennis), `bind_results` —
        # qui n'est pas « un résultat, un match » — la reliera au prochain
        # passage ; au-delà, jamais.
        ecart = ecart_cle_voisine(r, reglees)
        if ecart is not None:
            return (TENNIS_RELIER if ecart <= tolerance_for_scores("tennis")
                    else AUTRE_CLE)
        return DOUBLE if _est_double(r, noms) else TENNIS_SIMPLE
    if not pont_actif:
        return FOOT_SANS_PONT
    jour = depart.date()
    if jour >= maintenant.date():
        return FOOT_DU_JOUR
    cle = ("etat", jour)
    if cle not in memo:
        memo[cle] = etat_journee(jour, dossier, maintenant, jours_pont,
                                 final_apres, memo.setdefault("lisible", {}))
    return memo[cle]


def jours_reclames_detections(rows: list, maintenant: datetime) -> set:
    """Les journées dont `results-update` chargera le fichier : celles qui ont
    au moins un match de football EN ATTENTE (`days_needed`)."""
    return {d.date() for r in rows
            if (r["sport"] or "").lower() == "soccer" and not r["has_result"]
            for d in [_coup_d_envoi(r)] if d is not None and d <= maintenant - GRACE}


def verifier_production(classes: list, noms: dict, dossier: Path, jours: set,
                        reglees: "dict | None", progres=None,
                        mesurer_ligue: bool = False) -> tuple:
    """Le contrôle de production, match par match, pour tout ce que les
    fichiers du pont pourraient déjà régler (`A_VERIFIER`) — dans l'ordre
    chronologique, pour que `SourceFoot.oublier` libère la mémoire au fur et à
    mesure.

    * lié par `bind_results` → `APPARIABLE` : il ne manque qu'un passage ;
    * sinon, réglé sous une autre clé (même ligue) → `AUTRE_CLE` ;
    * sinon, journée complète → diagnostic (`diagnostiquer`) ;
    * sinon, la cause du pont reste.

    Rend (classes mises à jour, diagnostics, matchs que la ligue de
    `bet_features` rendrait appariables)."""
    source = SourceFoot(dossier)
    cache: dict = {dossier: source}
    a_voir = sorted((i for i, (_r, c) in enumerate(classes) if c in A_VERIFIER),
                    key=lambda i: _coup_d_envoi(classes[i][0]))
    nouvelles = list(classes)
    diags = []
    ligue_rendue = 0
    for n, i in enumerate(a_voir, 1):
        r, c = classes[i]
        source.oublier(_coup_d_envoi(r).date() - timedelta(days=1))
        prod = lie_en_production(r, noms, source, jours)
        if prod == "lie":
            nouvelles[i] = (r, APPARIABLE)
        elif reglees is not None and ecart_cle_voisine(r, reglees) is not None:
            nouvelles[i] = (r, AUTRE_CLE)
        elif c == FOOT_ABSENT:
            d = diagnostiquer(r, noms, dossier, cache, jours, prod)
            diags.append((r, d))
            # Ce que rapporterait la ligue de `bet_features` là où `events`
            # n'en a pas : même rapprochement, seule la ligue change. Une
            # mesure, pas un verdict.
            if (mesurer_ligue and d["verdict"] != APPARIABLE
                    and not (r["league"] or "") and _col(r, "league_bf")):
                ev = replace(_notre_evenement(r, noms), league=r["league_bf"])
                lot = _resultats_production(source, ev.start_time.date(), jours)
                if bind_results([ev], lot, sport="soccer")[0]:
                    ligue_rendue += 1
        if progres and n % 250 == 0:
            progres(n, len(a_voir))
    return nouvelles, diags, ligue_rendue


def analyser_detections(rows: list, maintenant: datetime, dossier: Path,
                        jours_pont: int, final_apres: int, pont_actif: bool,
                        cle_tennis: bool, reglees: dict, noms: dict,
                        progres=None) -> dict:
    memo: dict = {}
    classes = [(r, classer_detection(r, maintenant, dossier, jours_pont,
                                     final_apres, pont_actif, cle_tennis,
                                     reglees, noms, memo))
               for r in rows]
    classes, diags, ligue_rendue = verifier_production(
        classes, noms, dossier, jours_reclames_detections(rows, maintenant),
        reglees, progres, mesurer_ligue=True)
    comptes = Counter(c for _r, c in classes)
    dets = Counter()
    for r, c in classes:
        dets[c] += r["n_det"] or 0
    return {"classes": classes, "comptes": comptes, "dets": dets,
            "diags": diags, "ligue_rendue": ligue_rendue, "reglees": reglees}


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
        t[0] += c in (REGLE, REGLE_SANS_SCORE)
        t[1] += 1
        # Un résultat sans score règle le 1X2, jamais un total.
        t[2] += ((r["n_det"] or 0) if c == REGLE
                 else (_col(r, "n_h2h", 0) or 0) if c == REGLE_SANS_SCORE else 0)
        t[3] += r["n_det"] or 0
    print("  (détections : 1X2 et totaux, les seules que `settle` sait régler)")
    print(f"  {'sport':12} {'matchs réglés':>18}   {'détections réglées':>22}")
    for sp, (a, b, da, db_) in sorted(par_sport.items(), key=lambda x: -x[1][1]):
        pct = f"{100 * a / b:3.0f} %" if b else "  — "
        pct_d = f"{100 * da / db_:3.0f} %" if db_ else "  — "
        note = "" if sp in SPORTS_AVEC_SOURCE else "  — aucune source"
        print(f"  {sp[:12]:12} {a:7} / {b:<7} {pct}   {da:9} / {db_:<8} {pct_d}{note}")

    jours = defaultdict(Counter)
    for r, c in classes:
        if (c in FOOT_PONT or c == FICHIER_ILLISIBLE) and c != FOOT_ABSENT:
            jours[_coup_d_envoi(r).date()][c] += 1
    foot_ponte = any(c in FOOT_PONT or c == FICHIER_ILLISIBLE
                     or (c in (APPARIABLE, AUTRE_CLE)
                         and (r["sport"] or "").lower() == "soccer")
                     for r, c in classes)
    if pont_actif and foot_ponte:
        print("\nFOOTBALL — LES JOURNÉES DU PONT")
        if not jours:
            print("  Toutes complètes : ce qui manque au football vient de la "
                  "source ou du rapprochement.")
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
        print("(la production d'abord — `bind_results` sur les fichiers que "
              "results-update charge —\npuis le candidat le plus proche, veille "
              "et lendemain compris)")
        for v in ORDRE_POURQUOI:
            if pourquoi[v]:
                print(f"  {pourquoi[v]:5}  ({pourquoi_d[v]:5} dét.)  {v}")
        if sans_ligue:
            connues = sum(1 for r in sans_ligue if r["league_bf"])
            dates = sorted(_coup_d_envoi(r).date() for r in sans_ligue)
            print(f"\n  ⚠️ {len(sans_ligue)} de ces matchs n'ont pas de ligue dans "
                  f"`events` (du {dates[0].isoformat()} au {dates[-1].isoformat()}) : "
                  f"la classe\n  (féminin, jeunes, réserve) ne peut pas leur être "
                  f"posée. `bet_features` en connaît la\n  ligue pour {connues} ; "
                  f"avec elle, la production en rapprocherait "
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
            if v == PROLONG:
                fp = Counter(d["cand"]["prolong"] for _r, d in lot)
                print("   forme : " + ", ".join(f"{k} : {n}" for k, n in fp.most_common()))
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
            ecart = ecart_cle_voisine(r, res.get("reglees") or {})
            print(f"  {r['event_key'][:60]:60}  {r['n_det']} dét."
                  + (f" · Δ {ecart:.0f} min" if ecart is not None else ""))
    sans_src = Counter((r["sport"] or "?") for r, c in classes if c == SANS_SOURCE)
    if sans_src:
        print("\nSPORTS SANS SOURCE : " + ", ".join(
            f"{sp} {n}" for sp, n in sans_src.most_common()))

    _conseils(classes, comptes, pourquoi, dossier, jours_pont, maintenant, db,
              "match(s)")


def _maj(n: int, sports: str) -> str:
    return (f"      .venv/bin/python -m src.main results-update --days {n} "
            f"--sport {sports}\n"
            f"      .venv/bin/python -m src.main track-update")


def _conseils(classes, comptes, pourquoi, dossier: Path, jours_pont: int,
              maintenant: datetime, db: str, u: str) -> None:
    """Ce qu'il faut faire, dans l'ordre où il faut le faire — pour les
    paris joués comme pour les détections ; `u` nomme l'unité comptée."""
    print("\nQUE FAIRE, DANS CET ORDRE")
    joues = u == "pari(s)"

    def fenetre(causes) -> int:
        """Le `--days` qui couvre les manques de ces causes — et seulement
        eux : un match qu'aucune relance ne réglera n'élargit rien."""
        return jours_a_couvrir(maintenant, [_coup_d_envoi(r) for r, c in classes
                                            if c in causes])
    n_foot = fenetre(RELANCABLES)
    n_tennis = fenetre({TENNIS, TENNIS_SIMPLE, TENNIS_RELIER})

    # 1. Ce qui empêche TOUT le reste : configuration, fichier illisible.
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
    illisibles = sorted({_coup_d_envoi(r).date() for r, c in classes
                         if c == FICHIER_ILLISIBLE})
    if illisibles:
        print(f"  • ⚠️ {len(illisibles)} fichier(s) du pont ILLISIBLE(S) : tant "
              f"qu'il(s) reste(nt), results-update\n    met TOUT le football en "
              f"panne, pas seulement ces journées. Supprime-les, le pont\n    "
              f"les redemandera au prochain clic (si elles sont dans sa "
              f"fenêtre) :\n"
              + "\n".join(f"      rm -f {dossier}/{j.isoformat()}.json"
                          for j in illisibles))

    # 2. Élargir, ou effacer les refus, AVANT de cliquer : un clic sur
    #    l'ancienne fenêtre ne reprendrait pas ces journées, et on croirait le
    #    trou comblé. UNE seule commande : deux `sed` successifs laisseraient
    #    le second écraser le premier.
    jours_hors = sorted({_coup_d_envoi(r).date() for r, c in classes
                         if c in (HORS_FENETRE, TROP_TOT_HORS)})
    jours_refus = sorted({_coup_d_envoi(r).date() for r, c in classes
                          if c in (REFUSEE, TROP_TOT_REFUSEE)})
    if jours_hors or jours_refus:
        besoin = max([jours_pont] + [(maintenant.date() - j).days
                                     for j in jours_hors + jours_refus])
        if jours_hors:
            print(f"  • {len(jours_hors)} journée(s) sont SORTIES de la fenêtre "
                  f"du pont ({jours_hors[0].isoformat()} → "
                  f"{jours_hors[-1].isoformat()}) : il ne les demandera plus.")
        if jours_refus:
            n_refus = comptes[REFUSEE] + comptes[TROP_TOT_REFUSEE]
            print(f"  • {n_refus} {u} sur {len(jours_refus)} journée(s) "
                  f"REFUSÉE(S) par ton abonnement API-Football\n    "
                  f"({jours_refus[0].isoformat()} → {jours_refus[-1].isoformat()})."
                  f" Au palier gratuit (trois jours autour d'aujourd'hui),\n    "
                  f"ils ne se régleront pas.")
        print("    Pour les reprendre" + (" — avec un abonnement PAYANT pour les "
                                          "journées refusées" if jours_refus else "")
              + " :")
        if jours_refus:
            print(f"      rm -f {dossier}/*.refused")
        if besoin > jours_pont:
            print(f"      sed -i '/^SCORES_BRIDGE_DAYS=/d' .env && echo "
                  f"'SCORES_BRIDGE_DAYS={besoin}' >> .env\n"
                  f"      sudo systemctl restart betano-ingest")
        if jours_hors and not jours_refus:
            print("    ⚠️ Si ton abonnement API-Football est gratuit, il ne sert "
                  "que trois jours autour\n    d'aujourd'hui : les journées plus "
                  "anciennes seront REFUSÉES.")

    # 3. Cliquer, puis écrire en base.
    if jours_hors or jours_refus or comptes[JAMAIS_DEMANDEE] or comptes[TROP_TOT]:
        print(f"  • Dans le menu Tampermonkey (onglet Betano, Circus ou "
              f"MagicBetting), clique\n    {BOUTON}, attends « rien à "
              f"récupérer — tout est à jour », puis :\n"
              f"{_maj(n_foot, 'soccer')}\n"
              f"    et relance cette sonde pour voir ce qui reste.")
    n_app = comptes[APPARIABLE] + pourquoi[APPARIABLE]
    if n_app:
        print(f"  • {n_app} {u} que les fichiers du pont permettent DÉJÀ de "
              f"régler : results-update\n    n'est pas repassé depuis leur "
              f"arrivée —\n{_maj(n_foot, 'soccer')}")
    if pourquoi[VOISIN]:
        print(f"  • {pourquoi[VOISIN]} {u} présents dans le fichier d'un jour "
              f"voisin que results-update ne charge\n    pas (aucun autre match "
              f"en attente ce jour-là) : relancer ne suffit pas. Rare, sans\n"
              f"    correctif prévu.")
    if pourquoi[AMBIGU]:
        print(f"  • {pourquoi[AMBIGU]} {u} que deux matchs de la source "
              f"revendiquent presque à égalité : le\n    rapprochement refuse de "
              f"choisir, et c'est voulu — un mauvais choix réglerait des paris "
              f"faux.")
    if pourquoi[ORIENTATION]:
        print(f"  • {pourquoi[ORIENTATION]} {u} appariés, mais dont le sens des "
              f"camps est indécidable (deux clubs\n    aux noms emboîtés) : "
              f"laissés sans résultat plutôt que réglés peut-être à l'envers.\n"
              f"    À noter à la main si tu veux le P&L exact.")
    if pourquoi[INEXPLIQUE]:
        print(f"  • {pourquoi[INEXPLIQUE]} {u} où la sonde et la production "
              f"se contredisent : envoie la section\n    ci-dessus — c'est un "
              f"défaut de la sonde ou du rapprochement, et il faut savoir "
              f"lequel.")
    if pourquoi[CLASSE] or pourquoi[NOMS] or pourquoi[HORAIRE]:
        n = pourquoi[CLASSE] + pourquoi[NOMS] + pourquoi[HORAIRE]
        print(f"  • {n} {u} que la source a sans doute, mais que le "
              f"rapprochement rejette —\n    classe {pourquoi[CLASSE]}, horaire "
              f"{pourquoi[HORAIRE]}, noms proches {pourquoi[NOMS]} (ceux-là "
              f"contiennent du bruit :\n    un candidat n'est qu'un candidat). "
              f"Corrigeable dans le code : envoie la section\n    ci-dessus, "
              f"chaque règle se décide sur ses exemples.")
    if pourquoi[SANS_SCORE_SOURCE]:
        print(f"  • {pourquoi[SANS_SCORE_SOURCE]} {u} terminés dont la source "
              f"ne donnait pas le score à la capture :\n    la journée est "
              f"définitive, le pont ne la redemandera pas. Rien à régler.")
    if pourquoi[STATUT]:
        print(f"  • {pourquoi[STATUT]} {u} reportés, annulés ou arrêtés : "
              f"vérifie le règlement chez le book\n    (souvent remboursé) ; rien "
              f"à corriger ici.")
    if pourquoi[PROLONG]:
        print(f"  • {pourquoi[PROLONG]} {u} allés en prolongation sans score "
              f"à 90 min prouvable : le book règle\n    sur 90 min, la source ne "
              f"le donne pas — à noter à la main si tu veux le P&L exact.")
    if pourquoi[ABSENT]:
        print(f"  • {pourquoi[ABSENT]} {u} ABSENTS de la source (petites "
              f"ligues, amicaux) : rien à\n    corriger — seul un autre "
              f"fournisseur de résultats les couvrirait.")
    if comptes[AUTRE_CLE]:
        print(f"  • {comptes[AUTRE_CLE]} {u} ont leur résultat sous une AUTRE "
              f"clé du même match (même ligue),\n    à un horaire au-delà de la "
              f"tolérance : results-update vient d'échouer sur la clé\n    "
              f"orpheline et échouera encore. Le dashboard lit le résultat sur "
              f"la clé de la\n    MEILLEURE COTE : quand c'est l'orpheline, "
              f"l'opportunité reste non réglée alors que\n    le score est "
              f"connu. Corrigeable dans le code — à décider.")

    if comptes[TENNIS]:
        print(f"  • {comptes[TENNIS]} {u} de tennis : relancer\n"
              f"      .venv/bin/python -m src.main results-update --days "
              f"{n_tennis} --sport tennis\n"
              f"    Ce qui reste ensuite est surtout des DOUBLES et des ABANDONS, "
              f"que la source ne règle\n    pas — ceux-là sont à noter à la "
              f"main, ou à laisser non réglés.")
    if comptes[TENNIS_RELIER]:
        print(f"  • {comptes[TENNIS_RELIER]} match(s) de tennis ont leur "
              f"résultat sous une autre clé à moins de\n    12 h : un passage "
              f"de results-update les reliera —\n"
              f"      .venv/bin/python -m src.main results-update --days "
              f"{n_tennis} --sport tennis")
    if comptes[TENNIS_SIMPLE]:
        print(f"  • {comptes[TENNIS_SIMPLE]} match(s) de tennis en SIMPLE sans "
              f"résultat. Si results-update n'est pas\n    repassé sur le "
              f"tennis depuis ces matchs :\n"
              f"      .venv/bin/python -m src.main results-update --days "
              f"{n_tennis} --sport tennis\n"
              f"    S'il vient de passer, c'est le reste : surtout des ABANDONS et "
              f"des forfaits, que\n    la source ne règle pas — relancer ne "
              f"ferait que consommer du quota.")
    if comptes[DOUBLE]:
        print(f"  • {comptes[DOUBLE]} match(s) de DOUBLE : la source les sert, "
              f"mais notre lecteur les écarte —\n    leurs noms de paires "
              f"arrivent mutilés (`score_sources`). Rien à relancer.")
    if comptes[SANS_SOURCE] and not joues:
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
    if comptes[SPORT_INCONNU] and joues:
        print(f"  • {comptes[SPORT_INCONNU]} pari(s) dont la ligne `events` n'a "
              f"pas de sport : remets celui du clic —\n"
              f"      {_commande_sport(db)}")
    elif comptes[SPORT_INCONNU]:
        n_rep = sum(1 for r, c in classes if c == SPORT_INCONNU
                    and (_col(r, "sport_bf") or _col(r, "sport_pb")))
        print(f"  • {comptes[SPORT_INCONNU]} match(s) dont la ligne `events` n'a "
              f"pas de sport (« unknown ») : results-update\n    ne les réclame "
              f"pas. `bet_features` ou un clic en connaît le sport pour {n_rep}"
              + (" — remets-le :\n"
                 f"      {_commande_sport(db, SQL_SPORT_DET)}" if n_rep
                 else " ; les autres sont à laisser."))
    if comptes[SANS_EVENTS] or comptes[SPORT_INCONNU]:
        print(f"    puis :\n{_maj(fenetre({SANS_EVENTS, SPORT_INCONNU}), 'soccer,tennis')}")
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
                       pont_actif, cle_tennis, noms,
                       jours_reclames_base(a.db, depuis, maintenant),
                       charger_reglees(a.db, depuis))
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
