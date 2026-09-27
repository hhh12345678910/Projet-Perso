#!/usr/bin/env python3
"""Les règles de rapprochement ASSOUPLIES : ce qu'elles récupèrent, et combien
de fois elles se trompent — mesuré à l'aveugle, sur tes propres données.

POURQUOI CE BANC
----------------
Sortie du 27/09 : 3 166 matchs de football finis sans résultat depuis juin,
dont 1 964 ABSENTS de la source. L'idée « même horaire, noms à peu près
pareils : presque aucun risque » est juste sur le principe — c'est déjà
l'ordre du rapprochement (horaire ±10 min, puis les deux noms à 85/100, puis
pas de second candidat presque aussi bon). Mais une règle assouplie tournerait
AUSSI sur les matchs absents, et là, tout ce qu'elle trouverait serait faux.
La question n'est donc pas « le candidat choisi est-il le bon ? », c'est :
« quand le bon match n'existe pas, trouve-t-elle quand même quelque chose ? ».

Ça se mesure. Pour chaque match DÉJÀ réglé par la source (la réponse est
connue), le banc retrouve le match de la source que la production a retenu,
le CACHE, et rejoue : la production d'abord, puis chaque règle assouplie.
Tout ce qu'une règle trouve alors est une erreur qu'elle aurait écrite.

Deux mesures par règle :
* À L'AVEUGLE — le vrai match caché : combien de fois elle en prend un autre.
  Une règle ne tourne qu'APRÈS la production : elle n'est éprouvée que là où
  la production, elle, n'a rien pris. Un match de la source sous plusieurs de
  nos clés (horaire révisé) ne fait qu'une épreuve ;
* RÉCUPÈRE — les matchs aujourd'hui sans résultat qu'elle réglerait, dont tes
  paris joués.

Une règle n'est SÛRE qu'à 0 erreur à l'aveugle. Zéro erreur sur N épreuves ne
prouve pas un taux nul : au seuil de 95 %, il reste sous 3/N (la « règle de
trois »). Et plusieurs règles sûres ensemble REFUSENT un match pour lequel
elles choisissent deux matchs différents de la source (`ensemble`) : l'une des
deux écrirait un score faux.

(Pas de mesure « le vrai match présent, la règle en choisit un autre » : sur
un match que la production a lié, le vrai match est le meilleur du créneau et
un rival de chaque règle — elle vaut 0 par construction.)

Les règles essayées, chacune seule (l'ordre de tes points de contrôle :
l'horaire, les noms, la classe) :
* N — noms plus souples, MÊME horaire (±10 min) : la paire à 80, 75 ou 70
  au lieu de 85, chaque camp à 60 au moins, 10 points d'avance sur tout autre
  match du créneau ; variantes « même pays » (ligue connue des deux côtés) ;
* H — horaire plus souple, MÊMES noms (en entier, 90 au moins sur chaque
  camp ; 80 pour la variante L, « Kalmar » contre « Kalmar FF ») : ±1 h,
  ±3 h, ±6 h, et aucun autre match des deux mêmes clubs dans les fichiers de
  la veille, du jour et du lendemain (match retour, match rejoué) ;
* C — barrière de classe levée (hommes / femmes / jeunes / réserve) : même
  horaire, noms à 85, et AUCUN autre match du créneau qui ressemble (70) —
  le jumeau d'une autre classe compris.
Toutes exigent un match réglable (terminé, score à 90 min prouvé, à la même
heure que le candidat — un match reporté puis rejoué garde son identifiant)
et un sens de lecture sûr (`scores._orientation`).

⚠️ Les règles voient TOUS les matchs du pont, quel que soit leur statut
(reporté, non commencé…), et leur pays : un match reporté au même horaire est
un rival, un match retour pas encore joué aussi. Une règle SÛRE ici ne l'est
en production que si elle y voit la même chose — pas seulement les matchs
terminés (`parse_apifootball_results`) que results-update reçoit aujourd'hui,
sans ligue ni pays. Mesuré par la revue du 27/09 : bâtie sur ces seuls
matchs terminés, la règle C réglait les hommes avec le score des femmes quand
le match des hommes était reporté.

⚠️ LIMITE. L'épreuve à l'aveugle porte sur les matchs que la source CONNAÎT
(ceux qu'on a réglés) ; les ligues qu'elle ne sert pas ont peut-être plus de
sosies. Les exemples imprimés — les plus fragiles d'abord — servent à le
vérifier à l'œil.

⚠️ LECTURE SEULE. Rien n'est écrit, ni en base ni sur disque.

Usage :
    .venv/bin/python -m scripts.banc_rapprochement --depuis 2026-06-01
"""
from __future__ import annotations

import argparse
import os
import sys
import unicodedata
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rapidfuzz import fuzz, process  # noqa: E402

from scripts.resultats_manquants import (GRACE, SEUIL_BRUT, SourceFoot,  # noqa: E402
                                         _coup_d_envoi, _flou, _notre_evenement,
                                         _pour_flou, charger_detections,
                                         jours_a_couvrir, jours_reclames_detections)
from scripts.verif_resultats import SOURCES_API, _apparier  # noqa: E402
from scripts.verif_resultats import charger as charger_regles  # noqa: E402
from src.config import load_env_file  # noqa: E402
from src.matcher import (class_marker_from_league, team_similarity,  # noqa: E402
                         with_class_marker)
from src.scores import (_orientation, _sans_cote, bind_results,  # noqa: E402
                        tolerance_for_scores)


@dataclass(frozen=True)
class Regle:
    """Une règle assouplie, appliquée APRÈS la production — jamais à sa place."""
    code: str
    titre: str
    critere: str              # "noms", "horaire" ou "classe"
    seuil: float = 85.0       # la paire : avec classe (noms), sans (classe)
    cote_min: float = 0.0     # chaque camp, même score
    entier_min: float = 0.0   # chaque camp, EN ENTIER (`fuzz.ratio`) — horaire
    fenetre: float = 10.0     # minutes d'écart au plus
    marge: float = 10.0       # avance sur le second du créneau — noms
    pays: bool = False        # le pays de notre ligue = celui de la source


REGLES = (
    Regle("N80", "noms ≥ 80, même horaire", "noms", seuil=80, cote_min=60),
    Regle("N75", "noms ≥ 75, même horaire", "noms", seuil=75, cote_min=60),
    Regle("N70", "noms ≥ 70, même horaire", "noms", seuil=70, cote_min=60),
    Regle("N75P", "noms ≥ 75, même horaire, même pays", "noms", seuil=75,
          cote_min=60, pays=True),
    Regle("N70P", "noms ≥ 70, même horaire, même pays", "noms", seuil=70,
          cote_min=60, pays=True),
    Regle("H1", "mêmes noms, horaire ± 1 h", "horaire", entier_min=90, fenetre=60),
    Regle("H3", "mêmes noms, horaire ± 3 h", "horaire", entier_min=90, fenetre=180),
    Regle("H6", "mêmes noms, horaire ± 6 h", "horaire", entier_min=90, fenetre=360),
    Regle("H3L", "noms proches (80), horaire ± 3 h", "horaire", entier_min=80,
          fenetre=180),
    Regle("C", "classe levée, même horaire, seul candidat", "classe"),
)

#: Le seuil de la production (`bind_results`) : au-dessus, les noms suffisent.
SEUIL_PRODUCTION = 85.0
#: Deux matchs « des mêmes clubs » pour la règle H : chaque camp à 80 en entier.
MEMES_CLUBS = 80.0
#: Un rival pour la règle C : tout ce qui ressemble à 70 dans le créneau.
RIVAL_CLASSE = 70.0
#: Le créneau où l'on cherche les rivaux d'une règle « même horaire ».
CRENEAU_MIN = 10.0
#: Exemples imprimés par règle.
EXEMPLES = 8


def _sans_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    return " ".join(s.lower().replace("-", " ").split())


def pays_de_ligue(ligue: str) -> str:
    """« Scotland - Premiership » → « scotland » ; "" si la ligue ne le dit pas."""
    if " - " not in (ligue or ""):
        return ""
    return _sans_accents(ligue.split(" - ", 1)[0])


def ident_match(x: dict) -> tuple:
    """L'identité d'un match de la source : son identifiant, sinon ses noms et
    son heure (un fichier de test n'en porte pas toujours)."""
    return ("id", x["id"]) if x.get("id") else ("noms", x["th"], x["ta"], x["t"])


def ident_resultat(r) -> tuple:
    """La même identité, lue sur un `MatchResult`."""
    return (("id", r.source_id) if r.source_id
            else ("noms", r.home, r.away, r.start_time))


class Fenetres:
    """Les matchs de la source autour de chaque journée, rangés par heure et
    par identité. S'appuie sur `SourceFoot` (une lecture par fichier) et
    oublie avec lui."""

    def __init__(self, source: SourceFoot):
        self.source = source
        self._cache: dict = {}
        self._cache_entiers: dict = {}

    def _prep(self, jour: date) -> dict:
        if jour not in self._cache:
            fx, _noms, index = self.source.fenetre(jour)
            par_heure = sorted((x["t"], i) for i, x in enumerate(fx) if x["t"])
            res = sorted(((r.start_time, n, d, r) for n, (d, r) in enumerate(
                (d, r) for d in (jour - timedelta(days=1), jour, jour + timedelta(days=1))
                for r in self.source.resultats(d))), key=lambda z: (z[0], z[1]))
            self._cache[jour] = {
                "fx": fx, "index": index,
                "heures": [t for t, _i in par_heure],
                "ordre": [i for _t, i in par_heure],
                # Un match reporté puis rejoué garde son identifiant : le
                # résultat d'un candidat est celui du même identifiant À LA
                # MÊME HEURE, jamais celui du match rejoué le lendemain.
                "resultats": {(ident_resultat(r), r.start_time): r for _t, _n, _d, r in res},
                "res": [(d, r) for _t, _n, d, r in res],
                "res_heures": [t for t, _n, _d, _r in res]}
        return self._cache[jour]

    def oublier(self, avant: date) -> None:
        self.source.oublier(avant)
        for d in [d for d in self._cache if d < avant]:
            del self._cache[d]
        for cle in [c for c in self._cache_entiers if c[1] < avant]:
            del self._cache_entiers[cle]

    def lot(self, t: datetime, tol: int, jours: "set | None" = None) -> list:
        """Les résultats contre lesquels `results-update` rapproche un match
        de l'instant `t` — le lot de `_resultats_production` : la journée du
        match, plus la veille et le lendemain s'ils sont chargés (`jours`,
        `days_needed` ; None = toutes). Réduit d'emblée à la tolérance
        horaire, que `match_event` appliquerait de toute façon : même liaison,
        sans parcourir quatre mille résultats par match."""
        jour = t.date()
        p = self._prep(jour)
        pas = timedelta(minutes=tol)
        return [r for d, r in p["res"][bisect_left(p["res_heures"], t - pas):
                                       bisect_right(p["res_heures"], t + pas)]
                if d == jour or jours is None or d in jours]

    def _entiers(self, nom: str, jour: date) -> set:
        """Les noms de la fenêtre à `MEMES_CLUBS` et plus EN ENTIER face à
        `nom` — ce que `proches` (70 par fragment) ne garantit pas : « ss »
        contre « s s » vaut 80 en entier, 67 par fragment."""
        cle = (nom, jour)
        if cle not in self._cache_entiers:
            self._cache_entiers[cle] = {
                n for n, _s, _i in process.extract(
                    nom, self.source.fenetre(jour)[1], scorer=fuzz.ratio,
                    score_cutoff=MEMES_CLUBS, limit=None)}
        return self._cache_entiers[cle]

    def candidats(self, evm) -> list:
        """Les matchs de la source qui peuvent compter pour une règle.

        * Dans le créneau (±10 min) : TOUS — les règles « même horaire » y
          cherchent leur candidat et leurs rivaux.
        * Au-delà, seules les règles H regardent, et seulement des matchs dont
          les DEUX camps ressemblent aux nôtres (un candidat à 85, ou un rival
          « mêmes clubs ») : on part des noms proches du nôtre à domicile (70
          par fragment, comme la sonde, ou 80 en entier), et l'autre camp est
          jugé directement. Sans ce tri, un « Deportivo » ramène des centaines
          de matchs par match, et le banc prend une demi-heure."""
        depart = evm.start_time
        jour = depart.date()
        p = self._prep(jour)
        fx = p["fx"]
        oh, oa = _pour_flou(evm.home), _pour_flou(evm.away)
        pas = timedelta(minutes=CRENEAU_MIN)
        idx = set(p["ordre"][bisect_left(p["heures"], depart - pas):
                             bisect_right(p["heures"], depart + pas)])
        for n in self.source.proches(oh, jour) | self._entiers(oh, jour):
            for i in p["index"][n]:
                if i in idx:
                    continue
                x = fx[i]
                autre = x["sa"] if x["sh"] == n else x["sh"]
                if _flou(oa, autre) >= SEUIL_BRUT or fuzz.ratio(oa, autre) >= MEMES_CLUBS:
                    idx.add(i)
        pays = pays_de_ligue(evm.league)
        return [_decrire(evm, oh, oa, fx[i], p["resultats"], pays)
                for i in sorted(idx) if fx[i]["t"] is not None]


def _decrire(evm, oh: str, oa: str, x: dict, resultats: dict, pays: str) -> dict:
    """Ce qu'une règle regarde d'un candidat. Le sens de lecture et le pays ne
    sont calculés que pour le candidat retenu (`_sens`, `_pays_ok`)."""
    h, a = evm.home, evm.away
    g_cotes = max((team_similarity(h, x["th"]), team_similarity(a, x["ta"])),
                  (team_similarity(h, x["ta"]), team_similarity(a, x["th"])), key=sum)
    u_cotes = max((_flou(oh, x["sh"]), _flou(oa, x["sa"])),
                  (_flou(oh, x["sa"]), _flou(oa, x["sh"])), key=sum)
    entier = max(min(fuzz.ratio(oh, x["sh"]), fuzz.ratio(oa, x["sa"])),
                 min(fuzz.ratio(oh, x["sa"]), fuzz.ratio(oa, x["sh"])))
    ident = ident_match(x)
    return {
        "x": x, "ident": ident, "evm": evm, "pays": pays,
        "dt": abs((x["t"] - evm.start_time).total_seconds()) / 60,
        "g": sum(g_cotes) / 2, "g_cotes": g_cotes,
        "u": sum(u_cotes) / 2, "entier": entier,
        "res": resultats.get((ident, x["t"])),
    }


def _sens(c: dict) -> "str | None":
    """"direct", "inverse", ou None — `scores._orientation`, comme la
    production ; un nul symétrique se lit dans les deux sens."""
    if c["res"] is None:
        return None
    sens = _orientation(c["evm"], c["res"])
    if sens is None and _sans_cote(c["res"]):
        return "direct"
    return sens


def _pays_ok(c: dict) -> "bool | None":
    source = _sans_accents(c["x"].get("pays") or "")
    return None if not c["pays"] or not source else c["pays"] == source


def choisir(regle: Regle, cands: list) -> tuple:
    """(candidat retenu ou None, raison) — la règle seule, sur ces candidats."""
    creneau = [c for c in cands if c["dt"] <= regle.fenetre]
    if regle.critere == "noms":
        admis = [c for c in creneau
                 if c["g"] >= regle.seuil and min(c["g_cotes"]) >= regle.cote_min]
        cle = "g"
    elif regle.critere == "horaire":
        admis = [c for c in creneau
                 if c["g"] >= SEUIL_PRODUCTION and c["entier"] >= regle.entier_min]
        cle = "g"
    else:
        admis = [c for c in creneau if c["u"] >= regle.seuil]
        cle = "u"
    if not admis:
        return None, "rien"
    best = max(admis, key=lambda c: (c[cle], -c["dt"]))
    autres = [c for c in cands if c is not best]
    if regle.critere == "noms":
        rival = max((c["g"] for c in autres if c["dt"] <= CRENEAU_MIN), default=0.0)
        if best["g"] - rival < regle.marge:
            return None, "ambigu"
    elif regle.critere == "horaire":
        # Un autre match des deux mêmes clubs, n'importe où dans les trois
        # journées : match retour, match rejoué, jumeau d'une autre classe.
        if any(c["entier"] >= MEMES_CLUBS or c["g"] >= SEUIL_PRODUCTION for c in autres):
            return None, "mêmes clubs ailleurs"
    elif any(c["u"] >= RIVAL_CLASSE for c in autres if c["dt"] <= CRENEAU_MIN):
        return None, "ambigu"
    if regle.pays and _pays_ok(best) is not True:
        return None, "pays"
    if best["res"] is None:
        return None, "pas réglable"
    if _sens(best) is None:
        return None, "sens"
    return best, "ok"


def _doublons(vrai: dict, cands: list) -> set:
    """Le vrai match et ses doublons exacts (mêmes noms, même créneau) : un
    doublon n'est pas un sosie, le compter comme erreur mentirait."""
    out = {vrai["ident"]}
    for c in cands:
        if ((c["x"]["th"], c["x"]["ta"]) == (vrai["x"]["th"], vrai["x"]["ta"])
                and abs((c["x"]["t"] - vrai["x"]["t"]).total_seconds()) <= CRENEAU_MIN * 60):
            out.add(c["ident"])
    return out


class Bilan:
    def __init__(self):
        self.epreuves = 0              # matchs de la source éprouvés (un par match)
        self.epreuves_regles = 0       # … où la production n'a pas pris de sosie
        self.hors_epreuve = 0          # saisis/importés, ou non retrouvés
        self.cles_en_double = 0        # autre clé d'un match déjà éprouvé
        self.manquants = 0
        self.par_production = 0        # manquants que la production lierait déjà
        self.sans_fichier = 0          # manquants dont la journée n'a pas de fichier
        self.reglees = 0               # résultats de football en base
        self.erreurs = defaultdict(list)    # code → [exemple]
        self.recup = defaultdict(list)      # code → [(exemple, paris joués)]
        self.choix: dict = {}               # event_key → {code: (identité, exemple)}
        self.joues: dict = {}               # event_key → paris joués
        self.departs_production: list = []  # coups d'envoi des « réglerait déjà »
        self.illisibles: list = []          # journées au fichier illisible
        self.maintenant = datetime.now(timezone.utc)
        self._vus: set = set()

    @property
    def tournent(self) -> int:
        """Les manquants sur lesquels une règle assouplie tournerait : pas liés
        par la production, et dont la journée a un fichier du pont."""
        return self.manquants - self.par_production - self.sans_fichier


def _exemple(ev, r, c) -> dict:
    x = c["x"]
    return {"t": ev.start_time, "nous": f"{ev.home} - {ev.away}",
            "ligue": r["league"] or "?", "source": f"{x['th']} - {x['ta']}",
            "source_ligue": x["ligue"], "dt": c["dt"], "g": c["g"], "u": c["u"],
            "entier": c["entier"], "event_key": r["event_key"]}


def analyser(reglees: list, manquants: list, noms: dict, joues: dict,
             dossier: Path, regles=REGLES, progres=None,
             jours: "set | None" = None) -> Bilan:
    """Rejoue chaque match dans l'ordre chronologique : trois journées de la
    source en mémoire à la fois, comme `resultats_manquants`.

    `jours` : les journées que `results-update` chargera pour les manquants
    (`days_needed`) — la veille ou le lendemain d'un match n'entrent dans son
    lot que s'ils en sont. None = toutes. Les épreuves, elles, supposent
    toutes les journées chargées : c'est le cas de la production, où chaque
    journée a au moins un match en attente."""
    bilan = Bilan()
    bilan.reglees = len(reglees)
    source = SourceFoot(dossier)
    fen = Fenetres(source)
    tol = tolerance_for_scores("soccer")
    tous = []
    for genre, rows in ((0, reglees), (1, manquants)):
        for r in rows:
            t = _coup_d_envoi(r)
            if t is not None:
                tous.append((t, genre, r))
            elif genre == 0:
                bilan.hors_epreuve += 1
            else:
                bilan.manquants += 1
                bilan.sans_fichier += 1
    tous.sort(key=lambda z: (z[0], z[1]))
    for n, (t, manquant, r) in enumerate(tous, 1):
        fen.oublier(t.date() - timedelta(days=1))
        ev = replace(_notre_evenement(r, noms), start_time=t)
        marque = class_marker_from_league(ev.league)
        evm = replace(ev, home=with_class_marker(ev.home, marque),
                      away=with_class_marker(ev.away, marque))
        if manquant and not any(
                (dossier / f"{d.isoformat()}.json").exists()
                for d in (t.date() - timedelta(days=1), t.date(), t.date() + timedelta(days=1))
                if d == t.date() or jours is None or d in jours):
            # Aucun fichier que results-update chargerait pour ce match
            # (journée jamais récupérée, ou pas encore) : aucune règle n'y
            # tourne. Hors des « faux attendus », pas hors de la couverture.
            bilan.manquants += 1
            bilan.sans_fichier += 1
            continue
        cands = fen.candidats(evm)
        if manquant:
            _manquant(bilan, r, ev, cands, fen.lot(t, tol, jours), joues, regles)
        else:
            _epreuve(bilan, r, ev, evm, cands, fen.lot(t, tol), source, tol, regles)
        if progres and n % 1000 == 0:
            progres(n, len(tous))
    bilan.illisibles = sorted(source.illisibles)
    return bilan


def _manquant(bilan, r, ev, cands, lot, joues, regles) -> None:
    bilan.manquants += 1
    liens, _c = bind_results([ev], lot, sport="soccer")
    if liens:
        bilan.par_production += 1
        bilan.departs_production.append(ev.start_time)
        return
    k = r["event_key"]
    for regle in regles:
        best, _raison = choisir(regle, cands)
        if best is not None:
            ex = _exemple(ev, r, best)
            bilan.recup[regle.code].append((ex, len(joues.get(k, []))))
            bilan.choix.setdefault(k, {})[regle.code] = (best["ident"], ex)
            bilan.joues[k] = len(joues.get(k, []))


def _epreuve(bilan, r, ev, evm, cands, lot, source, tol, regles) -> None:
    if not str(r["source"] or "").startswith(SOURCES_API):
        bilan.hors_epreuve += 1
        return
    vrai_res, _lot = _apparier(evm, source, tol)
    ident = ident_resultat(vrai_res) if vrai_res is not None else None
    vrai = next((c for c in cands if c["ident"] == ident
                 and c["x"]["t"] == vrai_res.start_time), None)
    if vrai is None:
        bilan.hors_epreuve += 1
        return
    # Un match sous plusieurs clés (horaire révisé) : une seule épreuve. Deux
    # épreuves sur les mêmes candidats ne sont pas indépendantes, et la borne
    # « 3 sur N » le suppose.
    if vrai["ident"] in bilan._vus:
        bilan.cles_en_double += 1
        return
    bilan._vus.add(vrai["ident"])
    bilan.epreuves += 1
    # Le vrai match CACHÉ : la production d'abord, puis chaque règle.
    caches = _doublons(vrai, cands)
    reste = [c for c in cands if c["ident"] not in caches]
    liens, _c = bind_results([ev], [x for x in lot if ident_resultat(x) not in caches],
                             sport="soccer")
    if liens:
        pris = next((c for c in reste if c["ident"] == ident_resultat(liens[0][1])), None)
        bilan.erreurs["production"].append(
            _exemple(ev, r, pris) if pris is not None else
            {"t": ev.start_time, "nous": f"{ev.home} - {ev.away}",
             "ligue": r["league"] or "?",
             "source": f"{liens[0][1].home} - {liens[0][1].away}",
             "source_ligue": "?", "dt": 0.0, "g": 0.0, "u": 0.0, "entier": 0.0,
             "event_key": r["event_key"]})
        # La production aurait écrit ce sosie : une règle assouplie, qui ne
        # tourne qu'APRÈS elle, n'aurait jamais été consultée.
        return
    bilan.epreuves_regles += 1
    for regle in regles:
        best, _raison = choisir(regle, reste)
        if best is not None:
            bilan.erreurs[regle.code].append(_exemple(ev, r, best))


# ------------------------------------------------------------------ sortie ---

def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.2f} %".replace(".", ",") if d else "—"


def _ligne_exemple(e: dict) -> str:
    t = e["t"].strftime("%Y-%m-%d %H:%M") if e.get("t") else "?"
    return (f"  {t}  {e['nous']}  [{str(e['ligue'])[:26]}]\n"
            f"      → {e['source']}  [{str(e['source_ligue'])[:26]}]  "
            f"écart {e['dt']:.0f} min · noms {e['g']:.0f} · sans classe {e['u']:.0f} "
            f"· en entier {e['entier']:.0f}")


def sures(bilan: Bilan, regles=REGLES) -> list:
    """Les règles à 0 erreur à l'aveugle."""
    return [g for g in regles if not bilan.erreurs[g.code]]


def ensemble(bilan: Bilan, regles) -> tuple:
    """(matchs récupérés, désaccords) quand ces règles tournent ensemble.

    Deux règles qui choisissent deux matchs DIFFÉRENTS de la source pour un
    même match : l'une des deux écrirait un score faux, et rien ne dit
    laquelle. Ensemble, elles refusent — c'est ce que ferait la production."""
    codes = {g.code for g in regles}
    pris, desaccords = [], []
    for k, choix in bilan.choix.items():
        vus = {c: v for c, v in choix.items() if c in codes}
        if not vus:
            continue
        if len({ident for ident, _ex in vus.values()}) == 1:
            pris.append(k)
        else:
            desaccords.append((k, vus))
    return pris, desaccords


def _nombre(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def imprimer(bilan: Bilan, depuis: date, regles=REGLES) -> None:
    n, nr = bilan.epreuves, bilan.epreuves_regles
    print(f"BANC D'ESSAI — règles de rapprochement assouplies, football, matchs à "
          f"partir du {depuis.isoformat()} (UTC)\n")
    print(f"Épreuve à l'aveugle : {n} matchs de la source déjà réglés, le vrai match "
          f"CACHÉ.\nTout ce qu'une règle trouve alors est une erreur qu'elle aurait "
          f"écrite.")
    if bilan.hors_epreuve or bilan.cles_en_double:
        print(f"({bilan.hors_epreuve} hors épreuve : saisis ou importés, ou non "
              f"retrouvés dans les fichiers ; {bilan.cles_en_double} autre(s) clé(s) "
              f"d'un match déjà éprouvé.)")
    if bilan.illisibles:
        print(f"\n⚠️ Fichier(s) du pont ILLISIBLE(S) : "
              f"{', '.join(d.isoformat() for d in bilan.illisibles)}. results-update "
              f"tombe alors en panne sur\n   TOUT le football : les « réglerait déjà » "
              f"ci-dessous ne valent qu'une fois le fichier réparé\n   (voir "
              f"scripts.resultats_manquants).")
    m = bilan.tournent
    print(f"\nMatchs finis sans résultat : {bilan.manquants}"
          + (f" — dont {bilan.par_production} que la production réglerait déjà"
             if bilan.par_production else "")
          + (f", {bilan.sans_fichier} sans fichier du pont" if bilan.sans_fichier else "")
          + f".\nUne règle assouplie tournerait sur les {m} autres.")
    if bilan.par_production:
        print(f"Pour régler les {bilan.par_production} premiers (une relance sans "
              f"--days ne remonte que 3 jours) :\n  .venv/bin/python -m src.main "
              f"results-update --days "
              f"{jours_a_couvrir(bilan.maintenant, bilan.departs_production)} "
              f"--sport soccer")
    if not n:
        print("\nAucune épreuve possible : aucun résultat de la source retrouvé "
              "dans les fichiers du pont.")
        return
    prod = len(bilan.erreurs["production"])
    larg = max(len(g.titre) for g in regles) + 6
    print(f"\n  {'':{larg}}  RÉCUPÈRE   À L'AVEUGLE         FAUX ATTENDUS")
    print(f"  {'':{larg}}   (joués)   erreurs / épreuves  sur {m:<9} VERDICT")
    print(f"  {'production actuelle (référence)':{larg}}  {'—':>9}   {prod:>6} / {n:<9}  "
          f"{'—':<13} —")
    for g in regles:
        rec = bilan.recup[g.code]
        joues = sum(1 for _e, j in rec if j)
        err = len(bilan.erreurs[g.code])
        if not nr:
            attendus, verdict = "?", "non éprouvée"
        elif err:
            attendus, verdict = f"≈ {_nombre(err / nr * m)}", "à écarter"
        else:
            attendus = f"< {_nombre(3 / nr * m)}"
            verdict = "SÛRE" if rec else "sûre, inutile"
        print(f"  {g.code:4} {g.titre:{larg - 5}}  {len(rec):>4} ({joues:>3})"
              f"   {err:>6} / {nr:<9}  {attendus:<13} {verdict}")
    print(f"\n  Les règles ne tournent qu'APRÈS la production : elles sont éprouvées "
          f"sur les {nr} matchs où,\n  le vrai match caché, la production n'a rien "
          f"pris. FAUX ATTENDUS : les résultats faux qu'une\n  règle écrirait sur les "
          f"{m} matchs où elle tournerait, à son taux mesuré. À 0 erreur, la borne\n"
          f"  à 95 % : moins de 3 sur {nr}. SÛRE ne veut donc pas dire « zéro » : au "
          f"pire, la valeur affichée.")
    if prod:
        p = prod / n
        print(f"\n  ⚠️ La PRODUCTION ACTUELLE, quand le vrai match manque à la source, "
              f"prend un sosie dans\n  {prod} cas sur {n} ({_nombre(100 * p)} %). Ces "
              f"résultats faux-là sont DÉJÀ en base, parmi les matchs\n  réglés — le "
              f"banc ne peut pas les désigner. Ordre de grandeur : ≈ "
              f"{_nombre(p / (1 - p) * m)} pour {m} matchs\n  absents non liés. Les cas "
              f"listés plus bas sont des SIMULATIONS : leur résultat en base est juste.")

    utiles = [g for g in sures(bilan, regles) if bilan.recup[g.code] and nr]
    if utiles:
        pris, desaccords = ensemble(bilan, utiles)
        print(f"\n  Les règles SÛRES ensemble ({', '.join(g.code for g in utiles)}) : "
              f"{len(pris)} match(s) récupéré(s)"
              + (f", dont {sum(1 for k in pris if bilan.joues.get(k))} avec un pari "
                 f"joué" if pris else "") + ".")
        if desaccords:
            print(f"  {len(desaccords)} match(s) où elles choisissent des matchs "
                  f"DIFFÉRENTS de la source : refusés (listés plus bas).")
        total = bilan.reglees + bilan.manquants
        if total:
            print(f"  Couverture football (matchs finis) : "
                  f"{_nombre(100 * bilan.reglees / total)} % → "
                  f"{_nombre(100 * (bilan.reglees + len(pris)) / total)} %")
        if desaccords:
            print(f"\n── DÉSACCORDS entre règles sûres ({len(desaccords)}) : l'une des "
                  f"deux écrirait un score faux")
            for _k, vus in desaccords[:EXEMPLES]:
                for code, (_ident, ex) in sorted(vus.items()):
                    print(f"  [{code}]" + _ligne_exemple(ex)[1:])

    fautes = [("production", "production actuelle — SIMULATION")] + [
        (g.code, g.titre) for g in regles]
    for code, titre in fautes:
        err = bilan.erreurs[code]
        if not err:
            continue
        print(f"\n── ERREURS À L'AVEUGLE — {titre} ({len(err)}) : le vrai match caché, "
              f"ce qu'elle a pris à sa place")
        for e in sorted(err, key=lambda e: e["g"])[:EXEMPLES]:
            print(_ligne_exemple(e))
    for g in regles:
        rec = bilan.recup[g.code]
        if not rec:
            continue
        print(f"\n── RÉCUPÉRÉS — {g.code} {g.titre} ({len(rec)}) : les plus "
              f"fragiles d'abord, à vérifier à l'œil")
        # Les plus fragiles : les noms les moins proches ; pour H, les noms
        # entiers les moins proches, puis le plus grand décalage.
        if g.critere == "horaire":
            fragile = lambda z: (z[0]["entier"], -z[0]["dt"])  # noqa: E731
        else:
            fragile = lambda z: z[0]["u" if g.critere == "classe" else "g"]  # noqa: E731
        for e, j in sorted(rec, key=fragile)[:EXEMPLES]:
            print(_ligne_exemple(e) + (f"  · {j} pari(s) joué(s)" if j else ""))

    print("\nQUE FAIRE")
    print("  • Envoie cette sortie. Rien n'est activé : une règle SÛRE ne passe en "
          "production qu'après\n    lecture de ses exemples, avec une étiquette à "
          "part (source « api-football/souple »)\n    pour pouvoir la retrouver et "
          "la retirer d'un coup.\n  • Une règle n'est sûre que telle que le banc "
          "l'a jouée : elle voit TOUS les matchs du pont\n    (reportés et non "
          "commencés compris, ce sont ses rivaux) et leur pays — pas seulement\n"
          "    les matchs terminés que results-update reçoit aujourd'hui.")


def charger_manquants(db: str, depuis: date, maintenant: datetime) -> tuple:
    """(matchs de football finis, sans résultat, portant une détection ;
    noms affichés ; journées que `results-update` chargera)."""
    rows, noms, _reglees = charger_detections(db, depuis)
    fin = maintenant - GRACE
    out = []
    for r in rows:
        t = _coup_d_envoi(r)
        if r["sport"] == "soccer" and not r["has_result"] and t is not None and t <= fin:
            out.append(r)
    return out, noms, jours_reclames_detections(rows, maintenant)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(
        description="Mesure à l'aveugle des règles de rapprochement assouplies "
                    "(football) : ce qu'elles récupèrent, combien elles se "
                    "trompent. Lecture seule.")
    ap.add_argument("--db", default="data/valuebet.db")
    ap.add_argument("--depuis", default="2026-06-01", metavar="AAAA-MM-JJ")
    a = ap.parse_args(argv)
    try:
        depuis = date.fromisoformat(a.depuis)
    except ValueError:
        ap.error(f"--depuis attend AAAA-MM-JJ, reçu {a.depuis!r}")
    load_env_file()
    racine = Path(__file__).resolve().parents[1]
    dossier = Path(os.getenv("SCORES_INGEST_DIR",
                             str(racine / "data" / "scores"))) / "soccer"
    maintenant = datetime.now(timezone.utc)
    reglees, noms, joues = charger_regles(a.db, depuis)
    manquants, noms_det, jours = charger_manquants(a.db, depuis, maintenant)
    noms = {**noms_det, **noms}

    def progres(i, n):
        print(f"  … {i}/{n} matchs rejoués", file=sys.stderr)
    bilan = analyser(reglees, manquants, noms, joues, dossier, progres=progres,
                     jours=jours)
    bilan.maintenant = maintenant
    imprimer(bilan, depuis)


if __name__ == "__main__":
    main()
