"""Quelle méthode de mise est VRAIMENT la meilleure ? Validation hors échantillon.

Le piège : noter une méthode sur les paris qui ont servi à la choisir. Une
règle taillée sur le passé paraît toujours bonne sur ce passé — c'est
l'ajustement au bruit. La seule mesure honnête est de CHOISIR sur une période
et de JUGER sur la suivante, qu'elle n'a jamais vue.

LE PROTOCOLE (« walk-forward ») : les paris réglés, triés par coup d'envoi,
sont coupés en `k` blocs consécutifs. Pour chaque bloc à partir du deuxième,
chaque méthode est réglée sur TOUS les blocs précédents puis appliquée au bloc
courant. Les blocs de test mis bout à bout forment une courbe que personne n'a
vue au moment du choix.

LES MÉTHODES, toutes à mise moyenne ≈ `mise` pour être comparables :

  - **fixe**              : `mise` partout ;
  - **2 paliers 30/60**   : règle posée d'avance (CLV double à 15 % d'EV) ;
  - **paliers auto (EV)** : ∝ EV médiane de chaque bande (`paliers_auto`) ;
  - **paliers CLV**       : ∝ CLV moyenne de chaque bande, mesurée sur le
                            passé — l'edge observé, pas l'EV annoncée ;
  - **optimisé ROI**      : parmi ~1 000 grilles de montants ronds, celle qui
                            a le mieux rapporté sur le passé. C'est le
                            candidat le plus exposé à l'ajustement au bruit : le
                            voir perdre hors échantillon est un résultat.

Chaque méthode est jugée À CAPITAL ÉGAL sur chaque bloc de test (ses mises
ramenées au total de la mise fixe) : elle ne peut gagner qu'en RÉPARTISSANT
mieux, jamais en misant plus.
"""
from __future__ import annotations

import itertools
import statistics as st

from src.clv import pnl as clv_pnl

#: Montants ronds autorisés pour la grille « optimisé ROI ».
MONTANTS = (15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 70)
#: Seuils des bandes du projet (le premier couvre tout ce qui est dessous).
SEUILS = (0.0, 8.0, 15.0, 35.0)
#: Tolérance sur la mise moyenne d'une grille (en €).
TOLERANCE_MOY = 3.0


def bande(ev: float) -> int:
    i = 0
    for j, s in enumerate(SEUILS):
        if ev >= s:
            i = j
    return i


def mise_par_bande(ev: float, montants) -> float:
    return montants[bande(ev)]


def _rendement(p) -> float:
    """Gain pour 1 € misé (statut et cote du pari)."""
    return clv_pnl(p["statut"], p["cote"], 1.0) or 0.0


def _ramener(montants_bruts, comptes, mise, arrondi):
    """Ramène des poids par bande à une mise moyenne `mise`, bornés à ½–2×,
    puis arrondis comme en production."""
    n = sum(comptes)
    if not n:
        return None
    bas, haut = 0.5 * mise, 2.0 * mise
    poids = [max(p, 1e-9) for p in montants_bruts]
    echelle = mise / (sum(p * c for p, c in zip(poids, comptes)) / n)
    for _ in range(50):
        brutes = [min(haut, max(bas, p * echelle)) for p in poids]
        moy = sum(b * c for b, c in zip(brutes, comptes)) / n
        if abs(moy - mise) < 1e-6:
            break
        echelle *= mise / moy
    return tuple(arrondi(b) for b in brutes)


def regle_ev(passe, mise, arrondi):
    """Mises ∝ EV médiane de chaque bande (comme `paliers_auto`)."""
    groupes = [[p["ev"] for p in passe if bande(p["ev"]) == i] for i in range(len(SEUILS))]
    comptes = [len(g) for g in groupes]
    tout = [p["ev"] for p in passe] or [1.0]
    poids = [st.median(g) if g else st.median(tout) for g in groupes]
    return _ramener(poids, comptes, mise, arrondi)


def regle_clv(passe, mise, arrondi, mini: int = 20):
    """Mises ∝ CLV moyenne MESURÉE de chaque bande sur le passé. Une bande
    avec moins de `mini` clôtures prend la CLV moyenne de tout le passé ; une
    CLV ≤ 0 donne le poids minimal (la borne basse ½ × mise)."""
    tous = [p["clv"] for p in passe if p["clv"] is not None]
    globale = st.mean(tous) if tous else 1.0
    poids, comptes = [], []
    for i in range(len(SEUILS)):
        lot = [p for p in passe if bande(p["ev"]) == i]
        clvs = [p["clv"] for p in lot if p["clv"] is not None]
        poids.append(max(st.mean(clvs) if len(clvs) >= mini else globale, 0.01))
        comptes.append(len(lot))
    return _ramener(poids, comptes, mise, arrondi)


def regle_optimisee(passe, mise, arrondi=None):
    """La grille de montants ronds (croissants, mise moyenne ≈ `mise`) qui a
    le MIEUX rapporté sur le passé, à capital égal."""
    n_b = [0] * len(SEUILS)
    r_b = [0.0] * len(SEUILS)
    for p in passe:
        i = bande(p["ev"])
        n_b[i] += 1
        r_b[i] += _rendement(p)
    n = sum(n_b)
    if not n:
        return None
    meilleur, score = None, None
    for grille in itertools.combinations_with_replacement(MONTANTS, len(SEUILS)):
        capital = sum(s * c for s, c in zip(grille, n_b))
        if abs(capital / n - mise) > TOLERANCE_MOY:
            continue
        pl = sum(s * r for s, r in zip(grille, r_b)) * (mise * n / capital)
        if score is None or pl > score:
            meilleur, score = grille, pl
    return meilleur


def valider(paris, mise: float, arrondi, k: int = 4, regles_fixes=None) -> dict:
    """Walk-forward sur `k` blocs. `paris` : dicts {ev, cote, statut, clv},
    triés par coup d'envoi. Rend, par méthode, la série des gains HORS
    ÉCHANTILLON (à capital égal), les mises, et la règle choisie à chaque bloc."""
    if len(paris) < 2 * k:
        raise ValueError(f"trop peu de paris pour {k} blocs ({len(paris)})")
    bornes = [round(i * len(paris) / k) for i in range(k + 1)]
    blocs = [paris[bornes[i]:bornes[i + 1]] for i in range(k)]

    constructeurs = {
        "fixe": lambda passe: (mise,) * len(SEUILS),
        "2 paliers 30/60": lambda passe: (30, 30, 60, 60),
        "paliers auto (EV)": lambda passe: regle_ev(passe, mise, arrondi),
        "paliers CLV": lambda passe: regle_clv(passe, mise, arrondi),
        "optimisé ROI": lambda passe: regle_optimisee(passe, mise),
    }
    for nom, grille in (regles_fixes or {}).items():
        constructeurs[nom] = (lambda g: (lambda passe: g))(tuple(grille))

    out = {nom: {"gains": [], "mises": [], "regles": []} for nom in constructeurs}
    for i in range(1, k):
        passe = [p for b in blocs[:i] for p in b]
        test = blocs[i]
        capital_fixe = mise * len(test)
        for nom, construire in constructeurs.items():
            grille = construire(passe)
            if grille is None:
                grille = (mise,) * len(SEUILS)
            mises = [mise_par_bande(p["ev"], grille) for p in test]
            echelle = capital_fixe / sum(mises) if sum(mises) else 1.0
            out[nom]["regles"].append(tuple(grille))
            for p, m in zip(test, mises):
                out[nom]["mises"].append(m * echelle)
                out[nom]["gains"].append(_rendement(p) * m * echelle)
    return out
