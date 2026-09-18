"""Les combinaisons qui ressortent — et pourquoi il ne faut pas les croire.

CE MODULE EST DANGEREUX, ET C'EST POUR ÇA QU'IL EST ÉCRIT COMME ÇA
-------------------------------------------------------------------
Chercher « le meilleur segment » sur six dimensions, c'est tester des
centaines d'hypothèses et garder la plus flatteuse. Sur des données de pari
sportif, où l'écart-type du P&L par pari dépasse largement son espérance,
cette opération produit un gagnant même quand il n'y a rien à gagner. Le
projet l'a déjà mesuré sur ses propres paris joués : **sur 767 paris,
l'espérance était de +794 € pour +1 854 € observés — environ 1 050 € des gains
étaient de la chance**, sur un effectif que n'importe quel barème de volume
qualifierait de confortable.

Trois garde-fous sont donc CÂBLÉS, pas optionnels :

1. **Le classement se fait sur la CLV, pas sur le ROI.** La CLV est environ
   huit fois moins bruitée par pari que le P&L : c'est le seul des deux
   instruments qui sépare deux segments à ces effectifs. Trier par ROI
   reviendrait à trier par chance. Le ROI est AFFICHÉ, jamais utilisé comme
   critère par défaut.
2. **Le nombre de combinaisons examinées est rendu avec le résultat**, et
   l'espérance de faux positifs qui en découle est calculée. Un utilisateur
   qui voit « 312 combinaisons testées » lit le chiffre autrement que celui à
   qui on montre seulement le podium.
3. **Un plancher d'effectif s'applique avant tout calcul**, et ce qui est
   écarté est COMPTÉ. Un filtre silencieux transformerait « on n'a pas
   regardé » en « il n'y avait rien ».

⚠️ CE MODULE NE DÉCIDE RIEN ET NE RECOMMANDE RIEN. Il ne rend pas « la
meilleure stratégie » ; il rend des segments ordonnés, chacun avec son
effectif, sa couverture et son avertissement. La différence n'est pas
cosmétique : c'est celle entre une exploration et une promesse.
"""
from __future__ import annotations

import itertools
import statistics as st

from .metriques import clv_de, statut_de
from .perimetre import (MIN_SEGMENT, bande_delai, libelle_book, libelle_marche,
                        libelle_sport, taille_echantillon)

#: Les dimensions combinables. `(cle, libellé, fonction d'extraction)`.
#: L'extraction rend la valeur CANONIQUE ; le libellé d'affichage est ajouté
#: au moment de rendre, jamais stocké dans la clé de groupement.
def _dimensions(bande_cote, bande_ev):
    return (
        ("sport", "Sport", lambda r: r["sport"] or "?"),
        ("bookmaker", "Bookmaker", lambda r: r["book"] or "?"),
        ("odds", "Cote", lambda r: bande_cote(float(r["odd_taken"]))),
        ("ev", "EV", lambda r: bande_ev(float(r["ev_pct"] or 0.0))),
        ("market", "Marché", lambda r: r["market"] or "?"),
        ("delay", "Délai", lambda r: bande_delai(r["delai_h"])),
    )


#: Nombre maximal de dimensions croisées. Trois, et pas six : au-delà, chaque
#: segment devient si étroit que le plancher d'effectif l'élimine de toute
#: façon, et le nombre de combinaisons testées explose — donc le nombre de
#: faux positifs attendus avec lui.
PROFONDEUR_MAX = 3

#: Les libellés lisibles d'une valeur, par dimension.
_LIBELLES = {
    "sport": libelle_sport,
    "bookmaker": libelle_book,
    "market": libelle_marche,
}


def _precalculer(rows, stake: float) -> None:
    """Range sur chaque ligne son statut, son P&L et sa CLV — UNE seule fois.

    ⚠️ C'est une MÉMOÏSATION, pas une seconde définition : les trois valeurs
    sortent de `statut_de`, `clv.pnl` et `clv_de`, les mêmes fonctions que
    `resume`. Sans elle, une ligne présente dans quarante et un groupements
    serait réglée quarante et une fois — mesuré : plus d'un million d'appels
    à `clv.settle` pour une seule analyse, plusieurs secondes de calcul pour
    un résultat identique.
    """
    from ..clv import pnl as clv_pnl
    for r in rows:
        if "_statut" in r:
            continue
        s = statut_de(r)
        r["_statut"] = s
        r["_pnl"] = clv_pnl(s, float(r["odd_taken"]), stake)
        r["_clv"] = clv_de(r)


def resume_rapide(rows, stake: float) -> dict:
    """Le même bloc que `metriques.resume`, à partir des valeurs pré-calculées.

    ⚠️ CETTE FONCTION EXISTE POUR UNE SEULE RAISON : LA VITESSE. Elle applique
    la formule du projet — `ROI = 100 × Σ(P&L) / (mise × nombre de RÉGLÉS)` —
    au lieu de la recalculer depuis les scores. Son accord exact avec
    `metriques.resume` est VERROUILLÉ PAR UN TEST qui compare les deux sur des
    lots tirés au hasard. Si elle divergeait un jour, c'est le test qui le
    dirait, pas un chiffre bizarre dans une interface.

    Les clés rendues sont un SOUS-ENSEMBLE de celles de `resume` : seules
    celles dont un segment a besoin.
    """
    n = len(rows)
    gains = [r["_pnl"] for r in rows if r["_pnl"] is not None]
    clvs = [r["_clv"] for r in rows if r["_clv"] is not None]
    mise = stake * len(gains)
    return {
        "opportunities": n,
        "settled": len(gains),
        "settlement_rate": round(100.0 * len(gains) / n, 2) if n else None,
        "clv_n": len(clvs),
        "clv_coverage": round(100.0 * len(clvs) / n, 2) if n else None,
        "clv": round(st.mean(clvs), 2) if clvs else None,
        "roi": round(100.0 * sum(gains) / mise, 2) if mise else None,
        "pnl": round(sum(gains), 2) if gains else None,
        "stake_total": round(mise, 2),
        "played": sum(1 for r in rows if r["played"]),
        "sample": taille_echantillon(n),
        "sample_settled": taille_echantillon(len(gains)),
        "sample_clv": taille_echantillon(len(clvs)),
    }


def _valeur_lisible(dim: str, valeur) -> str:
    fn = _LIBELLES.get(dim)
    return fn(valeur) if fn else str(valeur)


def chercher(rows, stake: float, bande_cote, bande_ev, *,
             min_n: int = MIN_SEGMENT, trier_par: str = "clv",
             limite: int = 25, profondeur: int = PROFONDEUR_MAX) -> dict:
    """Les segments qui passent le plancher, ordonnés — et le compte des tests.

    `trier_par` vaut « clv » (défaut, et recommandé) ou « roi ». Le second est
    disponible parce que l'utilisateur a le droit de le demander, pas parce
    qu'il est conseillé : l'avertissement rendu le dit explicitement.
    """
    if trier_par not in ("clv", "roi"):
        from .filtres import FiltreInvalide
        raise FiltreInvalide(
            f"trier_par doit valoir clv ou roi — reçu : {trier_par!r}")
    profondeur = max(1, min(int(profondeur), PROFONDEUR_MAX))
    min_n = max(1, int(min_n))

    _precalculer(rows, stake)
    dims = _dimensions(bande_cote, bande_ev)

    # ⚠️ UN SEUL PARCOURS DES LIGNES, quelle que soit la profondeur. On
    # construit la clé la plus fine une fois, puis on remonte vers les
    # combinaisons plus courtes en fusionnant des LISTES DE RÉFÉRENCES — pas
    # en reparcourant les données. Le coût reste linéaire.
    combinaisons = []
    for k in range(1, profondeur + 1):
        combinaisons.extend(itertools.combinations(range(len(dims)), k))

    testees = 0
    ecartes_effectif = 0
    # ⚠️ DEUX DESCRIPTIONS DU MÊME LOT NE SONT PAS DEUX RÉSULTATS.
    #
    # Observé au premier essai : « Cote > 6.0 » et « Cote > 6.0 × Délai
    # 6-12 h » occupaient deux places du podium avec des chiffres identiques,
    # parce que TOUTES les cotes > 6 de l'échantillon tombaient dans cette
    # bande de délai. Le second critère n'apprend rien ; il donne juste
    # l'impression que deux pistes convergent. On garde donc la description la
    # plus COURTE d'un même ensemble de paris — l'ensemble est identifié par
    # ses identifiants, pas par ses agrégats, pour qu'une égalité fortuite de
    # CLV ne fasse jamais fusionner deux lots distincts.
    # ⚠️ LES COORDONNÉES DE CHAQUE LIGNE SONT CALCULÉES UNE FOIS, PAS 41.
    # Mesuré sur 40 000 opportunités : appeler les six extracteurs à chaque
    # combinaison coûtait 7,7 s pour un résultat identique — les bandes de
    # cote, d'EV et de délai étaient recalculées quarante et une fois par
    # ligne. Une seule passe, puis un simple indexage du tuple.
    coords = [tuple(d[2](r) for d in dims) for r in rows]

    par_ensemble: dict = {}
    redondants = 0
    for indices in combinaisons:
        groupes: dict = {}
        for r, co in zip(rows, coords):
            cle = tuple(co[i] for i in indices)
            groupes.setdefault(cle, []).append(r)
        for cle, lot in groupes.items():
            testees += 1
            if len(lot) < min_n:
                ecartes_effectif += 1
                continue
            bloc = resume_rapide(lot, stake)
            if bloc[trier_par] is None:
                continue
            signature = frozenset(r["id"] for r in lot)
            precedent = par_ensemble.get(signature)
            if precedent is not None:
                redondants += 1
                if precedent["profondeur"] <= len(indices):
                    continue
            par_ensemble[signature] = {
                "criteres": [
                    {"dimension": dims[i][0], "label": dims[i][1],
                     "value": v, "display": _valeur_lisible(dims[i][0], v)}
                    for i, v in zip(indices, cle)],
                "profondeur": len(indices),
                **bloc,
            }

    trouves = list(par_ensemble.values())
    trouves.sort(key=lambda s: (s[trier_par] is None, -(s[trier_par] or 0)))
    garde = trouves[:limite]

    return {
        "segments": garde,
        "trier_par": trier_par,
        "min_n": min_n,
        "profondeur": profondeur,
        "combinaisons_testees": testees,
        "retenus": len(trouves),
        "ecartes_effectif": ecartes_effectif,
        #: Segments décrivant un lot DÉJÀ décrit plus simplement. Comptés et
        #: rendus : une déduplication muette ferait croire que moins de
        #: combinaisons ont été testées qu'en réalité, donc que le risque de
        #: data mining est plus faible qu'il ne l'est.
        "redondants_fusionnes": redondants,
        # ⚠️ RENDU MÊME QUAND TOUT VA BIEN : la troncature doit se voir.
        "tronque": max(0, len(trouves) - len(garde)),
        "warnings": _avertissements(testees, len(trouves), trier_par, min_n),
    }


def _avertissements(testees: int, retenus: int, trier_par: str,
                    min_n: int) -> list:
    """Ce qu'il faut lire AVANT le podium, jamais après.

    Le calcul de faux positifs attendus n'est pas une figure de style : il
    répond à la seule question qui compte devant un classement, « combien de
    ces lignes seraient là si rien n'était vrai ? »"""
    out = [
        f"DATA MINING — {testees} combinaisons ont été examinées et {retenus} "
        f"passent le plancher de {min_n} opportunités. Chercher le meilleur "
        f"segment parmi des centaines FABRIQUE un gagnant même quand il n'y a "
        f"rien à gagner : le premier du classement est la combinaison la plus "
        f"chanceuse autant que la meilleure.",
    ]
    if testees:
        # Au seuil usuel de 5 %, sur des tests indépendants. Ils ne le sont
        # pas tout à fait — les segments se recoupent — donc ce nombre est un
        # ORDRE DE GRANDEUR, et il est présenté comme tel.
        attendus = testees * 0.05
        out.append(
            f"Ordre de grandeur : à un seuil de 5 %, environ {attendus:.0f} "
            f"de ces {testees} combinaisons paraîtraient « remarquables » par "
            f"pur hasard. Les segments se recoupant, ce chiffre est indicatif "
            f"et non un test exact.")
    if trier_par == "roi":
        out.append(
            "Classement par ROI DEMANDÉ : c'est le plus bruité des deux "
            "instruments — l'écart-type du P&L par pari dépasse largement son "
            "espérance. Sur les paris joués du projet, environ 1 050 € des "
            "1 854 € observés relevaient de la chance, pour une espérance de "
            "+794 €. La CLV sépare mieux, et à effectif égal.")
    else:
        out.append(
            "Classement par CLV : c'est l'instrument le moins bruité "
            "(≈ 8 fois moins que le P&L par pari). Un bon CLV dit que le prix "
            "pris était meilleur que la clôture — il ne garantit aucun gain "
            "réalisé sur la période observée.")
    out.append(
        "Un segment n'est une piste qu'une fois VÉRIFIÉ hors de l'échantillon "
        "qui l'a fait apparaître. Rien ici ne constitue cette vérification.")
    return out
