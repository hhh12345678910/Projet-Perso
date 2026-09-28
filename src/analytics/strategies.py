"""Strategy Finder — les configurations qui ont le mieux tenu, et à quel point
on peut y croire.

La question : « pour ce sport, quelles configurations de paris (bookmaker,
marché, pari, EV, cote, délai) ont historiquement donné les meilleurs
résultats, sur assez de paris réglés pour être exploitables ? »

⚠️ CE N'EST PAS UN `ORDER BY roi DESC`. Tester des centaines de combinaisons
et garder la plus flatteuse FABRIQUE un gagnant même quand il n'y a rien à
gagner (voir `segments.py`, dont ce module prolonge le moteur). Cinq
garde-fous sont donc câblés, dans cet ordre :

1. **Volume minimum** — une configuration sous `min_n` paris RÉGLÉS (période
   entière) est écartée avant tout calcul, et comptée.
2. **Découpage chronologique ENTRAÎNEMENT / VALIDATION (70 / 30)** — la
   sélection se fait sur les 70 % les plus anciens des paris réglés ; les 25
   meilleures candidates sont ensuite mesurées sur les 30 % les plus récents,
   jamais vus pendant la sélection. Aucun mélange aléatoire.
3. **Bornes basses, pas moyennes** — le score compare `moyenne − 1 × erreur
   type` : un ROI de +40 % sur 60 paris (erreur type ≈ 12 points) pèse moins
   qu'un +10 % sur 2 000. Le volume n'ajoute ensuite qu'un bonus
   LOGARITHMIQUE : 1 000 paris ne valent pas dix fois 100.
4. **Stabilité** — la période est coupée en quatre sous-périodes de même
   nombre de paris réglés ; la part de sous-périodes à CLV (ou ROI) positive
   entre au score.
5. **Pénalité de complexité** — chaque dimension au-delà du bookmaker coûte
   un demi-point : quatre critères qui ne décrivent que 100 paris sont le
   terrain du sur-ajustement.

LE SCORE EST UN OUTIL DE CLASSEMENT, PAS UN RÉSULTAT. Les chiffres affichés
sont ceux de `metriques.resume` — la définition du CLV, du ROI et du P&L de
tout l'Analytics —, sur le lot même que `service._charger` rend
(population, déduplication, règlement). Ouvrir une configuration dans
l'Analytics avec ses filtres redonne les mêmes nombres.

DÉFINITIONS DU SCORE (points de pourcentage de la mise)
-------------------------------------------------------
    borne(x)   = moyenne(x) − 1 × écart-type(x) / √n      (CLV, et ROI par pari)
    base       = w_clv × borne(CLV) + w_roi × borne(ROI)
                 équilibre (0,5 ; 0,5) · CLV (0,8 ; 0,2) · ROI (0,2 ; 0,8)
    volume     = 0,5 × log2(paris réglés / min_n)          (≥ 0, saturant)
    complexité = 0,5 × (nombre de critères − 1)
    stabilité  = 1 × (part de sous-périodes positives − 0,5)
    sélection  = base(entraînement) + volume − complexité
    final      = ½ base(entraînement) + ½ base(validation)
                 + volume − complexité + stabilité
    (validation insuffisante : base(validation) = base(entraînement) − 2)

CLV et ROI sont sur la même échelle — en théorie l'espérance du ROI VAUT la
CLV quand la clôture est efficiente —, le ROI est seulement bien plus
bruité, ce que la borne basse absorbe.

LIMITES ÉNONCÉES
----------------
  - le filtre de délai de l'Analytics est inclusif aux deux bornes, la bande
    de délai exclut sa borne haute : un pari détecté pile à 3,000 h peut
    changer de côté entre les deux vues (cas de bord, aucun effet mesurable) ;
  - la mise est notionnelle et FIXE (25 € par défaut) : le ROI n'en dépend
    pas, le P&L si.
"""
from __future__ import annotations

import bisect
import itertools
import math
import random
import statistics as st
import threading
import time
import zlib
from datetime import datetime, timezone

from ..clv import pnl as clv_pnl
from .filtres import FiltreInvalide, Filtres
from .metriques import clv_de, resume, statut_de
from .mise import mise_de
from .perimetre import (BANDES_DELAI, bande_delai, canoniser_book, jumeaux_de,
                        libelle_groupe_book, libelle_marche, libelle_pari,
                        libelle_sport, pari_de)
from .populations import LIBELLE as LIBELLE_POPULATION
from .populations import Population

OBJECTIFS = {
    "balanced": ("Équilibre CLV / ROI", 0.5, 0.5),
    "clv": ("Priorité CLV", 0.8, 0.2),
    "roi": ("Priorité ROI", 0.2, 0.8),
}

#: Quatre critères au plus, bookmaker compris.
PROFONDEUR_MAX = 4
#: Candidates mesurées sur la validation, et cartes affichées.
RETENUES = 25
AFFICHEES = 5
#: Part chronologique de l'entraînement.
PART_ENTRAINEMENT = 0.70
SOUS_PERIODES = 4
#: z de la borne basse : ≈ 84 % unilatéral. Assez pour punir le petit
#: échantillon, pas au point de ne plus rien classer.
Z_BORNE = 1.0
#: Effectif minimal pour qu'une mesure entre au score ou à la stabilité.
MIN_MESURE = 10
MIN_BLOC = 5
#: Bootstrap : les cartes seulement (c'est le plus coûteux), graine fixée
#: par configuration pour que deux recherches identiques rendent le même IC.
BOOTSTRAP_TIRAGES = 200
NIVEAU_IC = 90

#: Cache court : une même recherche relancée dans les minutes qui suivent.
CACHE_TTL_S = 300
_CACHE: dict = {}
_VERROU_CACHE = threading.Lock()


# ── Les dimensions ────────────────────────────────────────────────────

def _dimensions(bande_cote, bande_ev):
    """(clé, libellé, extraction, affichage). L'extraction rend la valeur
    CANONIQUE, ou None si elle est inexploitable — une ligne sans valeur n'est
    jamais rangée dans une configuration qui porte sur cette dimension."""
    def ev(r):
        return bande_ev(float(r["ev_pct"])) if r["ev_pct"] is not None else None

    def cote(r):
        v = bande_cote(float(r["odd_taken"]))
        return None if v == "?" else v

    def delai(r):
        v = bande_delai(r["delai_h"])
        return None if v == "?" else v

    return (
        ("market", "Marché", lambda r: r["market"] or None, libelle_marche),
        ("outcome", "Pari", lambda r: pari_de(r["outcome_label"]) or None, libelle_pari),
        ("ev", "EV", ev, str),
        ("odds", "Cote", cote, str),
        ("delay", "Délai", delai, str),
    )


def _instant(brut) -> "datetime | None":
    if not brut:
        return None
    try:
        d = datetime.fromisoformat(str(brut).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ── Les agrégats rapides (sélection) ──────────────────────────────────

class _Acc:
    """Sommes d'un sous-lot. Mêmes formules que `metriques._cellule` : ROI =
    Σ P&L / Σ mises des RÉGLÉS, CLV = moyenne de `clv_de`. Servent au SCORE ;
    les chiffres AFFICHÉS repassent par `resume`."""
    __slots__ = ("n", "regles", "pnl", "mise", "r2", "clv_n", "clv_s", "clv_s2", "clv_pos")

    def __init__(self):
        self.n = self.regles = self.clv_n = self.clv_pos = 0
        self.pnl = self.mise = self.r2 = self.clv_s = self.clv_s2 = 0.0

    def ajouter(self, p) -> None:
        self.n += 1
        if p.pnl is not None:
            self.regles += 1
            self.pnl += p.pnl
            self.mise += p.mise
            if p.mise:
                r = 100.0 * p.pnl / p.mise
                self.r2 += r * r
        if p.clv is not None:
            self.clv_n += 1
            self.clv_s += p.clv
            self.clv_s2 += p.clv * p.clv
            self.clv_pos += p.clv > 0

    def roi(self):
        return 100.0 * self.pnl / self.mise if self.mise else None

    def clv(self):
        return self.clv_s / self.clv_n if self.clv_n else None

    def _borne(self, moy, s2, n):
        if moy is None or n < MIN_MESURE:
            return None
        var = max(0.0, (s2 - n * moy * moy) / (n - 1))
        return moy - Z_BORNE * math.sqrt(var / n)

    def borne_clv(self):
        return self._borne(self.clv(), self.clv_s2, self.clv_n)

    def borne_roi(self):
        # Le ROI par pari est pnl/mise ; en mise fixe sa moyenne EST le ROI.
        if not self.regles or self.roi() is None:
            return None
        return self._borne(self.roi(), self.r2, self.regles)


class _Ligne:
    """Ce qu'on garde d'une ligne pour la recherche : calculé UNE fois."""
    __slots__ = ("row", "statut", "mise", "pnl", "clv", "quand", "train", "bloc", "coords", "book")

    def __init__(self, row, stake):
        self.row = row
        self.statut = statut_de(row)
        self.mise = mise_de(stake, row)
        self.pnl = clv_pnl(self.statut, float(row["odd_taken"]), self.mise)
        self.clv = clv_de(row)
        self.quand = _instant(row["detected_at"])
        self.train = True
        self.bloc = None
        self.coords = ()
        self.book = canoniser_book(row["book"]) if row["book"] else None


def _decoupage(lignes) -> dict:
    """La coupure 70 / 30 et les sous-périodes, sur les paris RÉGLÉS datés.

    ⚠️ CHRONOLOGIQUE, JAMAIS ALÉATOIRE. La validation doit être la partie la
    plus récente : c'est la seule qui ressemble à « ce qui vient après ». Les
    bornes sont prises sur les RÉGLÉS, pour que la validation porte des paris
    dont le ROI se mesure — le lot entier (réglés ou non) est ensuite rangé
    de part et d'autre selon sa date de détection."""
    dates = sorted(l.quand for l in lignes if l.pnl is not None and l.quand)
    if len(dates) < 2 * MIN_MESURE:
        for l in lignes:
            l.train, l.bloc = True, None
        return {"cutoff": None, "bornes": []}
    coupure = dates[min(len(dates) - 1, int(len(dates) * PART_ENTRAINEMENT))]
    bornes = [dates[min(len(dates) - 1, int(len(dates) * k / SOUS_PERIODES))]
              for k in range(1, SOUS_PERIODES)]
    for l in lignes:
        if l.quand is None:
            l.train, l.bloc = True, None
            continue
        l.train = l.quand < coupure
        l.bloc = bisect.bisect_right(bornes, l.quand)
    return {"cutoff": coupure, "bornes": bornes, "debut": dates[0], "fin": dates[-1]}


def _agreger(membres) -> dict:
    tout, train, val = _Acc(), _Acc(), _Acc()
    blocs = [_Acc() for _ in range(SOUS_PERIODES)]
    for p in membres:
        tout.ajouter(p)
        (train if p.train else val).ajouter(p)
        if p.bloc is not None:
            blocs[p.bloc].ajouter(p)
    return {"tout": tout, "train": train, "val": val, "blocs": blocs}


def _base(acc: _Acc, w_clv: float, w_roi: float):
    bc, br = acc.borne_clv(), acc.borne_roi()
    if w_clv and bc is None or w_roi and br is None:
        return None
    return w_clv * (bc or 0.0) + w_roi * (br or 0.0)


def _parts_positives(blocs) -> tuple:
    clv = [b.clv() for b in blocs if b.clv_n >= MIN_BLOC]
    roi = [b.roi() for b in blocs if b.regles >= MIN_BLOC and b.roi() is not None]
    part = (lambda xs: sum(1 for x in xs if x > 0) / len(xs) if xs else None)
    return part(clv), part(roi), len(clv)


# ── Le moteur ─────────────────────────────────────────────────────────

def chercher(rows, stake, bande_cote, bande_ev, *, min_n: int = 100,
             objectif: str = "balanced") -> dict:
    """Le cœur, sans base ni HTTP : testable sur n'importe quel lot."""
    if objectif not in OBJECTIFS:
        raise FiltreInvalide(
            f"objective doit valoir {' | '.join(OBJECTIFS)} — reçu : {objectif!r}")
    min_n = max(1, int(min_n))
    _libelle, w_clv, w_roi = OBJECTIFS[objectif]
    dims = _dimensions(bande_cote, bande_ev)

    lignes = [_Ligne(r, stake) for r in rows]
    lignes = [l for l in lignes if l.book]
    decoupe = _decoupage(lignes)
    for l in lignes:
        l.coords = tuple(d[2](l.row) for d in dims)

    # ── 1. Toutes les configurations, en UNE passe par combinaison ─────
    combinaisons = [c for k in range(0, PROFONDEUR_MAX)
                    for c in itertools.combinations(range(len(dims)), k)]
    testees = ecartees = redondantes = 0
    par_ensemble: dict = {}
    for indices in combinaisons:
        groupes: dict = {}
        for l in lignes:
            valeurs = tuple(l.coords[i] for i in indices)
            if None in valeurs:
                continue
            groupes.setdefault((l.book, *valeurs), []).append(l)
        for cle, membres in groupes.items():
            testees += 1
            regles = sum(1 for m in membres if m.pnl is not None)
            if regles < min_n:
                ecartees += 1
                continue
            signature = frozenset(id(m) for m in membres)
            precedent = par_ensemble.get(signature)
            if precedent is not None:
                # DEUX DESCRIPTIONS DU MÊME LOT NE SONT PAS DEUX RÉSULTATS : on
                # garde la plus COURTE (voir `segments.py`, même règle).
                redondantes += 1
                if precedent["profondeur"] <= len(indices) + 1:
                    continue
            par_ensemble[signature] = {
                "cle": cle, "indices": indices, "profondeur": len(indices) + 1,
                "membres": membres,
            }

    # ── 2. Score d'ENTRAÎNEMENT, sur tout ce qui passe le plancher ─────
    candidates = []
    for c in par_ensemble.values():
        agg = _agreger(c["membres"])
        c["agg"] = agg
        # Exclusions (§21) : sans ROI, ou sans CLV quand l'objectif la lit.
        if agg["tout"].roi() is None:
            continue
        if w_clv and agg["tout"].clv_n < MIN_MESURE:
            continue
        base_t = _base(agg["train"], w_clv, w_roi)
        if base_t is None:
            continue
        volume = 0.5 * math.log2(max(1.0, agg["tout"].regles / min_n))
        complexite = 0.5 * (c["profondeur"] - 1)
        c.update(base_t=base_t, volume=volume, complexite=complexite,
                 score_selection=base_t + volume - complexite)
        candidates.append(c)
    candidates.sort(key=lambda c: -c["score_selection"])
    retenues = candidates[:RETENUES]

    # ── 3. Validation, stabilité, score final ──────────────────────────
    min_val = max(MIN_MESURE, round(0.1 * min_n))
    for c in retenues:
        agg = c["agg"]
        val = agg["val"]
        suffisante = val.regles >= min_val and val.clv_n >= MIN_MESURE
        base_v = _base(val, w_clv, w_roi) if suffisante else None
        if base_v is None:
            base_v, suffisante = c["base_t"] - 2.0, False
        part_clv, part_roi, mesures = _parts_positives(agg["blocs"])
        part = part_roi if objectif == "roi" else part_clv
        stabilite = (part - 0.5) if part is not None and mesures >= 2 else 0.0
        c.update(suffisante=suffisante, base_v=base_v, part_clv=part_clv,
                 part_roi=part_roi, blocs_mesures=mesures,
                 score=0.5 * c["base_t"] + 0.5 * base_v + c["volume"]
                 - c["complexite"] + stabilite)
    retenues.sort(key=lambda c: -c["score"])

    strategies = [_rendre(c, rang, dims, stake, min_n, decoupe, objectif,
                          bootstrap=rang <= AFFICHEES)
                  for rang, c in enumerate(retenues, start=1)]
    return {
        "split": _split_public(lignes, decoupe),
        "counts": {"tested": testees, "eligible": len(par_ensemble),
                   "below_minimum": ecartees, "redundant": redondantes,
                   "validated": len(retenues), "shown": min(AFFICHEES, len(retenues))},
        "strategies": strategies,
        "by_bookmaker": _comparer(candidates, retenues, lambda c: c["cle"][0],
                                  libelle_groupe_book, dims),
        "by_market": _comparer(
            [c for c in candidates if 0 in c["indices"]], retenues,
            lambda c: c["cle"][1 + c["indices"].index(0)], libelle_marche, dims),
        "warnings": _avertissements(testees, len(par_ensemble), len(retenues), min_n),
    }


# ── Rendu d'une configuration ─────────────────────────────────────────

def _criteres(c, dims) -> list:
    out = [{"dimension": "bookmaker", "label": "Bookmaker", "value": c["cle"][0],
            "display": libelle_groupe_book(c["cle"][0])}]
    for i, v in zip(c["indices"], c["cle"][1:]):
        cle, lib, _x, affiche = dims[i]
        out.append({"dimension": cle, "label": lib, "value": v, "display": affiche(v)})
    return out


def _titre(criteres) -> str:
    noms = [c["display"] for c in criteres if c["dimension"] in ("bookmaker", "market")]
    return " · ".join(noms)


def _sous_resume(rows, stake) -> dict:
    s = resume(rows, stake)
    return {k: s[k] for k in ("opportunities", "settled", "clv", "clv_n", "roi",
                              "pnl", "stake_total")}


def _ic(valeurs, graine, ratio=None) -> "list | None":
    """IC percentile par bootstrap. `ratio` : (P&L, mise) par pari pour le ROI
    — le ROI d'un lot est Σ P&L / Σ mises, pas une moyenne de ROI."""
    n = len(valeurs if ratio is None else ratio)
    if n < MIN_MESURE:
        return None
    rnd = random.Random(graine)
    tirages = []
    for _ in range(BOOTSTRAP_TIRAGES):
        if ratio is None:
            ech = rnd.choices(valeurs, k=n)
            tirages.append(sum(ech) / n)
        else:
            ech = rnd.choices(ratio, k=n)
            mise = sum(m for _p, m in ech)
            if mise:
                tirages.append(100.0 * sum(p for p, _m in ech) / mise)
    if not tirages:
        return None
    tirages.sort()
    a = (100 - NIVEAU_IC) / 200.0
    lo, hi = tirages[int(a * (len(tirages) - 1))], tirages[int((1 - a) * (len(tirages) - 1))]
    return [round(lo, 2), round(hi, 2)]


def _ic_normal(moy, s2, n) -> "list | None":
    if moy is None or n < MIN_MESURE:
        return None
    sd = math.sqrt(max(0.0, (s2 - n * moy * moy) / (n - 1)))
    d = 1.645 * sd / math.sqrt(n)
    return [round(moy - d, 2), round(moy + d, 2)]


def _robustesse(s, val, suffisante, part_clv, mesures, ic_clv, min_n, objectif) -> dict:
    """Forte / Moyenne / Faible — jamais sur le ROI seul."""
    raisons, faible, forte = [], False, True
    clv, n = s["clv"], s["settled"]
    if clv is None or clv <= 0:
        faible = True
        raisons.append("CLV nulle ou négative sur la période entière.")
    if not suffisante:
        forte = False
        raisons.append("Validation hors-échantillon trop courte pour confirmer.")
    else:
        vc = val["clv"]
        if vc is None or vc <= 0:
            faible = True
            raisons.append("La CLV ne reste pas positive sur la validation.")
        elif clv and clv > 0 and vc < 0.25 * clv:
            faible = True
            raisons.append("Forte baisse de la CLV entre entraînement et validation.")
        elif clv and clv > 0 and vc < 0.5 * clv:
            forte = False
            raisons.append("La CLV baisse nettement sur la validation.")
        else:
            raisons.append("La CLV reste positive sur la validation.")
        if objectif != "clv" and (val["roi"] is None or val["roi"] < 0):
            forte = False
            raisons.append("ROI négatif sur la validation.")
    if part_clv is not None and mesures >= 2:
        if part_clv < 0.5:
            faible = True
            raisons.append("CLV positive sur moins de la moitié des sous-périodes.")
        elif part_clv < 0.75:
            forte = False
            raisons.append("CLV positive sur une partie seulement des sous-périodes.")
        else:
            raisons.append("CLV positive sur la plupart des sous-périodes.")
    else:
        forte = False
        raisons.append("Trop peu de sous-périodes mesurables pour juger la stabilité.")
    if ic_clv is None or ic_clv[0] is None or ic_clv[0] <= 0:
        forte = False
        raisons.append("L'intervalle de la CLV inclut zéro.")
    if n < 2 * min_n:
        forte = False
        raisons.append("Volume inférieur au double du minimum demandé.")
    niveau = "weak" if faible else ("strong" if forte else "medium")
    return {"level": niveau,
            "label": {"strong": "Forte", "medium": "Moyenne", "weak": "Faible"}[niveau],
            "reasons": raisons}


def _echantillon(n, min_n) -> dict:
    if n < 1.25 * min_n:
        return {"level": "limited", "label": "Échantillon limité"}
    if n >= max(1000, 4 * min_n):
        return {"level": "large", "label": "Échantillon important"}
    return {"level": "standard", "label": "Échantillon standard"}


def _pct(v) -> str:
    return "—" if v is None else f"{v:+.1f} %".replace(".", ",")


def _pourquoi(s, tr, val, suffisante, part_clv, mesures, cutoff, echantillon) -> list:
    """Des phrases tirées UNIQUEMENT des chiffres. Aucune causalité, aucune
    promesse : « a présenté », jamais « va gagner »."""
    out = [f"Cette configuration a présenté un CLV moyen de {_pct(s['clv'])} sur "
           f"{s['clv_n']} paris mesurés et un ROI de {_pct(s['roi'])} sur "
           f"{s['settled']} paris réglés."]
    quand = f" (détectés à partir du {cutoff:%d/%m})" if cutoff else ""
    if suffisante:
        suite = (" : il reste positif, ce qui réduit le risque qu'il ne tienne qu'à une "
                 "période particulière." if (val["clv"] or 0) > 0 else
                 " : il ne reste pas positif, ce qui invite à la prudence.")
        out.append(f"Sur la période de validation{quand}, jamais utilisée pour la "
                   f"sélectionner, son CLV est de {_pct(val['clv'])} sur "
                   f"{val['settled']} paris réglés{suite}")
    else:
        out.append(f"La période de validation{quand} ne compte que {val['settled']} "
                   f"paris réglés : trop peu pour confirmer le résultat hors échantillon.")
    if part_clv is not None and mesures >= 2:
        positives = round(part_clv * mesures)
        out.append(f"Son CLV a été positif sur {positives} des {mesures} sous-périodes "
                   f"mesurées.")
    clv, roi = s["clv"], s["roi"]
    if clv is not None and roi is not None:
        if clv > 0 > roi:
            out.append("Le ROI reste négatif malgré un CLV positif : sur cet effectif, le "
                       "résultat financier est encore dominé par la variance.")
        elif roi > 0 >= clv:
            out.append("Le ROI est positif sans CLV positif : un gain sans avantage de prix "
                       "mesurable relève plus probablement de la variance.")
    if echantillon["level"] == "limited":
        out.append("L'échantillon est proche du minimum demandé : ces chiffres restent "
                   "fragiles.")
    return out


def _filtres_analytics(criteres) -> dict:
    """De quoi rejouer la configuration dans l'Analytics — mêmes filtres, donc
    mêmes chiffres. Le groupe Kambi est rendu DÉPLIÉ (ses quatre books)."""
    f = {"bookmakers": [], "markets": [], "outcomes": [], "ev_bands": [],
         "odds_bands": [], "delay_min": None, "delay_max": None}
    for c in criteres:
        d, v = c["dimension"], c["value"]
        if d == "bookmaker":
            f["bookmakers"] = list(jumeaux_de(v))
        elif d == "market":
            f["markets"] = [v]
        elif d == "outcome":
            f["outcomes"] = [v]
        elif d == "ev":
            f["ev_bands"] = [v]
        elif d == "odds":
            f["odds_bands"] = [v]
        elif d == "delay":
            for lib, lo, hi in BANDES_DELAI:
                if lib == v:
                    f["delay_min"], f["delay_max"] = lo, hi
    return f


def _rendre(c, rang, dims, stake, min_n, decoupe, objectif, *, bootstrap) -> dict:
    from .service import _bande_temps, _cumuler, _decouper

    membres = c["membres"]
    rows = [m.row for m in membres]
    criteres = _criteres(c, dims)
    s = resume(rows, stake)
    tr = _sous_resume([m.row for m in membres if m.train], stake)
    val = _sous_resume([m.row for m in membres if not m.train], stake)
    val["sufficient"] = c["suffisante"]
    ident = "|".join(f"{x['dimension']}={x['value']}" for x in criteres)

    tout = c["agg"]["tout"]
    if bootstrap:
        graine = zlib.crc32(ident.encode())
        ic_clv = _ic([m.clv for m in membres if m.clv is not None], graine)
        ic_roi = _ic(None, graine + 1, ratio=[(m.pnl, m.mise) for m in membres
                                              if m.pnl is not None and m.mise])
        methode = "bootstrap"
    else:
        ic_clv = _ic_normal(tout.clv(), tout.clv_s2, tout.clv_n)
        ic_roi = _ic_normal(tout.roi(), tout.r2, tout.regles)
        methode = "normal"

    blocs = []
    bornes = decoupe.get("bornes") or []
    for i, b in enumerate(c["agg"]["blocs"]):
        if not b.n:
            continue
        blocs.append({"index": i + 1,
                      "from": _iso(bornes[i - 1] if i else decoupe.get("debut")),
                      "to": _iso(bornes[i] if i < len(bornes) else decoupe.get("fin")),
                      "settled": b.regles, "clv": _r(b.clv()), "clv_n": b.clv_n,
                      "roi": _r(b.roi())})
    echantillon = _echantillon(s["settled"], min_n)
    serie = _cumuler(_decouper(rows, stake, lambda r: _bande_temps(r, "semaine"), tri=True))
    garder = ("key", "label", "opportunities", "settled", "clv", "clv_n", "roi", "pnl",
              "pnl_cumul", "clv_cumul")
    return {
        "id": ident,
        "rank": rang,
        "depth": c["profondeur"],
        "title": _titre(criteres),
        "criteria": criteres,
        "summary": {k: s[k] for k in (
            "opportunities", "settled", "stake_total", "pnl", "roi", "clv", "clv_n",
            "clv_coverage", "clv_median", "clv_positive_rate", "ev_mean", "odds_mean",
            "won", "lost", "void")},
        "train": tr,
        "validation": val,
        "delta": {"clv": _diff(val["clv"], tr["clv"]), "roi": _diff(val["roi"], tr["roi"])},
        "ci": {"level": NIVEAU_IC, "method": methode, "clv": ic_clv, "roi": ic_roi},
        "stability": {"blocks": blocs, "clv_positive_share": _r(c["part_clv"], 4),
                      "roi_positive_share": _r(c["part_roi"], 4),
                      "measured_blocks": c["blocs_mesures"]},
        "robustness": _robustesse(s, val, c["suffisante"], c["part_clv"],
                                  c["blocs_mesures"], ic_clv, min_n, objectif),
        "sample": echantillon,
        "score": round(c["score"], 3),
        "why": _pourquoi(s, tr, val, c["suffisante"], c["part_clv"],
                         c["blocs_mesures"], decoupe.get("cutoff"), echantillon),
        "series": [{k: t.get(k) for k in garder} for t in serie],
        "analytics_filters": _filtres_analytics(criteres),
    }


def _comparer(candidates, retenues, cle, libelle, dims) -> list:
    """La meilleure configuration de chaque bookmaker (ou marché), CHOISIE SUR
    L'ENTRAÎNEMENT comme les autres, avec sa validation."""
    rangs = {id(c): r for r, c in enumerate(retenues, start=1)}
    meilleures: dict = {}
    for c in candidates:                       # déjà triées par score de sélection
        meilleures.setdefault(cle(c), c)
    out = []
    for k, c in meilleures.items():
        tout, val = c["agg"]["tout"], c["agg"]["val"]
        out.append({
            "key": k, "display": libelle(k), "strategy_id": "|".join(
                f"{x['dimension']}={x['value']}" for x in _criteres(c, dims)),
            "rank": rangs.get(id(c)), "title": _titre(_criteres(c, dims)),
            "settled": tout.regles, "clv": _r(tout.clv()), "roi": _r(tout.roi()),
            "validation_clv": _r(val.clv()), "validation_roi": _r(val.roi()),
            "validation_settled": val.regles,
        })
    out.sort(key=lambda x: (x["rank"] is None, x["rank"] or 0, -(x["settled"] or 0)))
    return out


def _split_public(lignes, decoupe) -> dict:
    coupure = decoupe.get("cutoff")
    regles = [l for l in lignes if l.pnl is not None]
    bornes = decoupe.get("bornes") or []
    blocs = []
    if coupure is not None:
        limites = [decoupe["debut"], *bornes, decoupe["fin"]]
        blocs = [{"index": i + 1, "from": _iso(limites[i]), "to": _iso(limites[i + 1])}
                 for i in range(len(limites) - 1)]
    return {
        "cutoff": _iso(coupure),
        "train": {"from": _iso(decoupe.get("debut")), "to": _iso(coupure),
                  "settled": sum(1 for l in regles if l.train)},
        "validation": {"from": _iso(coupure), "to": _iso(decoupe.get("fin")),
                       "settled": sum(1 for l in regles if not l.train)},
        "blocks": blocs,
    }


def _avertissements(testees, eligibles, retenues, min_n) -> list:
    out = [
        f"{testees} configurations ont été examinées ; {eligibles} atteignent "
        f"{min_n} paris réglés. Chercher la meilleure parmi des centaines FABRIQUE "
        f"un gagnant même sans avantage réel : à un seuil de 5 %, environ "
        f"{round(testees * 0.05)} paraîtraient « remarquables » par pur hasard "
        f"(ordre de grandeur, les configurations se recoupant).",
        f"La sélection s'est faite sur les 70 % les plus anciens des paris réglés ; "
        f"les {retenues} candidates retenues ont ensuite été mesurées sur les 30 % "
        f"les plus récents, jamais utilisés pour les choisir. C'est cette validation "
        f"qui distingue une piste d'un accident de l'historique.",
        "Ces chiffres décrivent l'historique. Ils ne garantissent aucun résultat futur.",
    ]
    return out


def _iso(d) -> "str | None":
    return d.isoformat() if d else None


def _r(v, n=2):
    return None if v is None else round(v, n)


def _diff(a, b):
    return None if a is None or b is None else round(a - b, 2)


# ── Point d'entrée : filtres, base, cache ─────────────────────────────

METHODE = (
    "Valuebet analyse différentes combinaisons de bookmaker, marché, type de pari, "
    "EV, cote et délai (quatre critères au plus, le bookmaker toujours compris). Les "
    "configurations sont filtrées par volume minimum de paris réglés, puis choisies "
    "sur les 70 % les plus anciens de la période en comparant la borne basse de leur "
    "CLV et de leur ROI — un résultat obtenu sur peu de paris pèse donc moins. Les "
    "meilleures sont ensuite mesurées sur les 30 % les plus récents, jamais utilisés "
    "pour les choisir, et classées en tenant compte de cette validation, de leur "
    "stabilité sur quatre sous-périodes et d'une pénalité pour chaque critère ajouté.")


def trouver(db_path, *, sport=None, date_from=None, date_to=None,
            population="settled", min_n: int = 100, objectif: str = "balanced",
            stake: float = 25.0) -> dict:
    """La recherche complète : le lot de l'Analytics, puis `chercher`."""
    from .service import _bande_cote, _bande_ev, _charger

    if objectif not in OBJECTIFS:
        raise FiltreInvalide(
            f"objective doit valoir {' | '.join(OBJECTIFS)} — reçu : {objectif!r}")
    try:
        min_n = int(min_n)
    except (TypeError, ValueError):
        raise FiltreInvalide(f"min_n doit être un entier — reçu : {min_n!r}")
    if min_n < 1:
        raise FiltreInvalide(f"min_n doit être au moins 1 — reçu : {min_n}")
    sport = (sport or "").strip().lower() or None
    filtres = Filtres(sports=(sport,) if sport else (), date_from=date_from,
                      date_to=date_to, population=str(population or "settled").lower(),
                      stake=stake).valider()

    cle = (str(db_path), sport, filtres.date_from, filtres.date_to,
           filtres.population.value, min_n, objectif, filtres.stake)
    maintenant = time.monotonic()
    with _VERROU_CACHE:
        vu = _CACHE.get(cle)
        if vu and maintenant - vu[0] < CACHE_TTL_S:
            return {**vu[1], "cached": True}

    lignes, _info = _charger(db_path, filtres)
    res = chercher(lignes, filtres.mise(), _bande_cote, _bande_ev,
                   min_n=min_n, objectif=objectif)
    pop = filtres.population
    sortie = {
        "params": {
            "sport": sport, "sport_label": libelle_sport(sport) if sport else "Tous les sports",
            "date_from": filtres.date_from, "date_to": filtres.date_to,
            "population": pop.value,
            "population_label": LIBELLE_POPULATION.get(pop, pop.value),
            "min_n": min_n, "objective": objectif,
            "objective_label": OBJECTIFS[objectif][0],
            "max_depth": PROFONDEUR_MAX, "stake": filtres.stake,
        },
        "lot": {"opportunities": len(lignes),
                "settled": sum(1 for r in lignes if statut_de(r) is not None),
                "clv_n": sum(1 for r in lignes if clv_de(r) is not None)},
        **res,
        "method": METHODE,
        "cached": False,
    }
    with _VERROU_CACHE:
        if len(_CACHE) > 32:
            _CACHE.clear()
        _CACHE[cle] = (maintenant, sortie)
    return sortie


__all__ = ["chercher", "trouver", "OBJECTIFS", "PROFONDEUR_MAX", "Population"]
