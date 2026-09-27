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

Trois mesures par règle :
* À L'AVEUGLE — le vrai match caché : combien de fois elle en prend un autre ;
* AUTRE CHOIX — le vrai match présent : combien de fois elle en choisit un
  autre ;
* RÉCUPÈRE — les matchs aujourd'hui sans résultat qu'elle réglerait, APRÈS la
  production (jamais à sa place), dont tes paris joués.

Une règle n'est SÛRE qu'à 0 erreur à l'aveugle et 0 autre choix. Zéro erreur
sur N épreuves ne prouve pas un taux nul : au seuil de 95 %, il reste sous
3/N (la « règle de trois »).

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
Toutes exigent un match réglable (terminé, score à 90 min prouvé) et un sens
de lecture sûr (`scores._orientation`).

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
                                         _pour_flou, charger_detections)
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
            res = [r for d in (jour - timedelta(days=1), jour, jour + timedelta(days=1))
                   for r in self.source.resultats(d)]
            res.sort(key=lambda r: r.start_time)
            self._cache[jour] = {
                "fx": fx, "index": index,
                "heures": [t for t, _i in par_heure],
                "ordre": [i for _t, i in par_heure],
                "resultats": {ident_resultat(r): r for r in res},
                "res": res, "res_heures": [r.start_time for r in res]}
        return self._cache[jour]

    def oublier(self, avant: date) -> None:
        self.source.oublier(avant)
        for d in [d for d in self._cache if d < avant]:
            del self._cache[d]
        for cle in [c for c in self._cache_entiers if c[1] < avant]:
            del self._cache_entiers[cle]

    def lot(self, t: datetime, tol: int) -> list:
        """Les résultats contre lesquels `results-update` rapproche un match
        de l'instant `t` — le lot des trois journées (`_resultats_production`),
        réduit d'emblée à la tolérance horaire, que `match_event` appliquerait
        de toute façon : même liaison, sans parcourir quatre mille résultats
        par match."""
        p = self._prep(t.date())
        pas = timedelta(minutes=tol)
        return p["res"][bisect_left(p["res_heures"], t - pas):
                        bisect_right(p["res_heures"], t + pas)]

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
        "res": resultats.get(ident),
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
        self.epreuves = 0
        self.hors_epreuve = 0          # saisis/importés, ou non retrouvés
        self.manquants = 0
        self.par_production = 0        # manquants que la production lierait déjà
        self.reglees = 0               # résultats de football en base
        self.erreurs = defaultdict(list)    # code → [exemple]
        self.autres = defaultdict(list)     # code → [exemple]
        self.recup = defaultdict(list)      # code → [(exemple, paris joués)]
        self.recup_par_match: dict = {}     # event_key → {codes}


def _exemple(ev, r, c) -> dict:
    x = c["x"]
    return {"t": ev.start_time, "nous": f"{ev.home} - {ev.away}",
            "ligue": r["league"] or "?", "source": f"{x['th']} - {x['ta']}",
            "source_ligue": x["ligue"], "dt": c["dt"], "g": c["g"], "u": c["u"],
            "entier": c["entier"], "event_key": r["event_key"]}


def analyser(reglees: list, manquants: list, noms: dict, joues: dict,
             dossier: Path, regles=REGLES, progres=None) -> Bilan:
    """Rejoue chaque match dans l'ordre chronologique : trois journées de la
    source en mémoire à la fois, comme `resultats_manquants`."""
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
    tous.sort(key=lambda z: (z[0], z[1]))
    for n, (t, manquant, r) in enumerate(tous, 1):
        fen.oublier(t.date() - timedelta(days=1))
        ev = replace(_notre_evenement(r, noms), start_time=t)
        marque = class_marker_from_league(ev.league)
        evm = replace(ev, home=with_class_marker(ev.home, marque),
                      away=with_class_marker(ev.away, marque))
        cands = fen.candidats(evm)
        lot = fen.lot(t, tol)
        if manquant:
            _manquant(bilan, r, ev, cands, lot, joues, regles)
        else:
            _epreuve(bilan, r, ev, evm, cands, lot, source, tol, regles)
        if progres and n % 1000 == 0:
            progres(n, len(tous))
    return bilan


def _manquant(bilan, r, ev, cands, lot, joues, regles) -> None:
    bilan.manquants += 1
    liens, _c = bind_results([ev], lot, sport="soccer")
    if liens:
        bilan.par_production += 1
        return
    for regle in regles:
        best, _raison = choisir(regle, cands)
        if best is not None:
            bilan.recup[regle.code].append(
                (_exemple(ev, r, best), len(joues.get(r["event_key"], []))))
            bilan.recup_par_match.setdefault(r["event_key"], set()).add(regle.code)


def _epreuve(bilan, r, ev, evm, cands, lot, source, tol, regles) -> None:
    if not str(r["source"] or "").startswith(SOURCES_API):
        bilan.hors_epreuve += 1
        return
    vrai_res, _lot = _apparier(evm, source, tol)
    ident = ident_resultat(vrai_res) if vrai_res is not None else None
    vrai = next((c for c in cands if c["ident"] == ident), None)
    if vrai is None:
        bilan.hors_epreuve += 1
        return
    bilan.epreuves += 1
    # Le vrai match présent : la règle, seule, en choisit-elle un autre ?
    for regle in regles:
        best, _raison = choisir(regle, cands)
        if best is not None and best["ident"] != vrai["ident"]:
            bilan.autres[regle.code].append(_exemple(ev, r, best))
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
        return
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
    """Les règles à 0 erreur à l'aveugle et 0 autre choix."""
    return [g for g in regles
            if not bilan.erreurs[g.code] and not bilan.autres[g.code]]


def _nombre(x: float) -> str:
    return f"{x:.1f}".replace(".", ",")


def imprimer(bilan: Bilan, depuis: date, regles=REGLES) -> None:
    n = bilan.epreuves
    print(f"BANC D'ESSAI — règles de rapprochement assouplies, football, matchs à "
          f"partir du {depuis.isoformat()} (UTC)\n")
    print(f"Épreuve à l'aveugle : {n} matchs déjà réglés par la source, le vrai "
          f"match CACHÉ.\nTout ce qu'une règle trouve alors est une erreur "
          f"qu'elle aurait écrite.")
    if bilan.hors_epreuve:
        print(f"({bilan.hors_epreuve} match(s) hors épreuve : saisis ou importés, "
              f"ou non retrouvés dans les fichiers.)")
    print(f"\nMatchs finis sans résultat : {bilan.manquants}"
          + (f" — dont {bilan.par_production} que la production réglerait déjà "
             f"(relancer results-update)" if bilan.par_production else ""))
    if not n:
        print("\nAucune épreuve possible : aucun résultat de la source retrouvé "
              "dans les fichiers du pont.")
        return
    # Les matchs sur lesquels une règle assouplie tournerait : les manquants
    # que la production ne lie pas. Son taux à l'aveugle s'y applique.
    m = bilan.manquants - bilan.par_production
    prod = len(bilan.erreurs["production"])
    larg = max(len(g.titre) for g in regles) + 6
    print(f"\n  {'':{larg}}  RÉCUPÈRE    À L'AVEUGLE       AUTRE   FAUX ATTENDUS")
    print(f"  {'':{larg}}   (joués)   erreurs/épreuves  CHOIX   sur {m:<6}      VERDICT")
    print(f"  {'production actuelle (référence)':{larg}}  {'—':>9}   {prod:>6} / {n:<7}  "
          f"{'—':>5}   {'—':<13} —")
    for g in regles:
        rec = bilan.recup[g.code]
        joues = sum(1 for _e, j in rec if j)
        err, aut = len(bilan.erreurs[g.code]), len(bilan.autres[g.code])
        if err or aut:
            verdict = "à écarter"
            attendus = f"≈ {_nombre(err / n * m)}"
        else:
            verdict = "SÛRE" if rec else "sûre, inutile"
            attendus = f"< {_nombre(3 / n * m)}"
        print(f"  {g.code:4} {g.titre:{larg - 5}}  {len(rec):>4} ({joues:>3})"
              f"   {err:>6} / {n:<7}  {aut:>5}   {attendus:<13} {verdict}")
    print(f"\n  FAUX ATTENDUS : les résultats faux que la règle écrirait sur les {m} "
          f"matchs où elle tournerait,\n  à son taux mesuré à l'aveugle. À 0 erreur, "
          f"la borne à 95 % : moins de 3 sur {n} épreuves.")
    if prod:
        print(f"\n  ⚠️ La PRODUCTION ACTUELLE prend un sosie dans {prod} cas sur {n} "
              f"quand le vrai match manque\n  à la source : c'est le taux auquel elle "
              f"écrit DÉJÀ des résultats faux sur les matchs absents\n  (de l'ordre "
              f"de {_nombre(prod / n * m)} sur {m}). Ses cas sont listés plus bas.")

    utiles = [g for g in sures(bilan, regles) if bilan.recup[g.code]]
    if utiles:
        codes = {g.code for g in utiles}
        ensemble = [k for k, v in bilan.recup_par_match.items() if v & codes]
        print(f"\n  Les règles SÛRES ensemble ({', '.join(g.code for g in utiles)}) : "
              f"{len(ensemble)} match(s) récupéré(s).")
        total = bilan.reglees + bilan.manquants
        if total:
            print(f"  Couverture football (matchs finis) : "
                  f"{_nombre(100 * bilan.reglees / total)} % → "
                  f"{_nombre(100 * (bilan.reglees + len(ensemble)) / total)} %")

    fautes = [("production", "production actuelle")] + [(g.code, g.titre) for g in regles]
    for code, titre in fautes:
        err = bilan.erreurs[code]
        if not err:
            continue
        print(f"\n── ERREURS À L'AVEUGLE — {titre} ({len(err)}) : le vrai match caché, "
              f"ce qu'elle a pris à sa place")
        for e in sorted(err, key=lambda e: e["g"])[:EXEMPLES]:
            print(_ligne_exemple(e))
    for g in regles:
        aut = bilan.autres[g.code]
        if not aut:
            continue
        print(f"\n── AUTRE CHOIX — {g.code} {g.titre} ({len(aut)}) : le vrai match "
              f"était là, elle en a pris un autre")
        for e in aut[:EXEMPLES]:
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
          "la retirer d'un coup.")


def charger_manquants(db: str, depuis: date, maintenant: datetime) -> tuple:
    """(matchs de football finis, sans résultat, portant une détection ;
    noms affichés)."""
    rows, noms, _reglees = charger_detections(db, depuis)
    fin = maintenant - GRACE
    out = []
    for r in rows:
        t = _coup_d_envoi(r)
        if r["sport"] == "soccer" and not r["has_result"] and t is not None and t <= fin:
            out.append(r)
    return out, noms


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
    reglees, noms, joues = charger_regles(a.db, depuis)
    manquants, noms_det = charger_manquants(a.db, depuis, datetime.now(timezone.utc))
    noms = {**noms_det, **noms}

    def progres(i, n):
        print(f"  … {i}/{n} matchs rejoués", file=sys.stderr)
    bilan = analyser(reglees, manquants, noms, joues, dossier, progres=progres)
    imprimer(bilan, depuis)


if __name__ == "__main__":
    main()
