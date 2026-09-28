"""La mise d'une analyse : fixe, ou Kelly fractionné.

⚠️ AUCUNE FORMULE NOUVELLE. La mise Kelly est celle du projet,
`src.ev.kelly_stake(cote, p, bankroll, fraction)` — la même que la « mise
conseillée » des alertes. Elle vaut

    bankroll × fraction × (p × cote − 1) / (cote − 1)
  = bankroll × fraction × EV / (cote − 1)

où `p = 1 / cote_juste` (la cote juste de la DÉTECTION) et `EV` l'avantage
en fraction. L'EV y pèse donc de tout son poids — elle est au numérateur,
linéairement : deux fois plus d'EV, deux fois plus de mise — et la cote la
tempère : à EV égale, une cote haute porte plus de variance, donc moins de
mise. C'est exactement ce que Kelly optimise.

TROIS CHOIX, ÉNONCÉS
--------------------
  - **bankroll FIXE**, sans capitalisation : chaque pari est misé sur la même
    bankroll de départ. Rejouer la capitalisation ferait dépendre la mise
    d'un pari de l'ORDRE des résultats précédents, donc de la période
    choisie — deux analyses du même pari ne donneraient plus la même mise ;
  - **plafond par pari**, en % de la bankroll (3 % par défaut, celui des
    alertes, `TELEGRAM_MAX_STAKE_PCT`) : Kelly entier sur une EV aberrante
    (+400 % à cote 1,5) demanderait 800 % de la bankroll ;
  - le ROI d'une analyse Kelly est `Σ(P&L) / Σ(mises)` : un ROI PONDÉRÉ par
    la mise, donc par l'EV. Il n'est pas comparable terme à terme avec le
    ROI à mise fixe, qui pèse chaque pari pareil — l'interface le dit.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..ev import kelly_stake

MODES = ("flat", "kelly")

#: Les fractions proposées par l'interface. Toute valeur de ]0 ; 1] est
#: acceptée par l'API ; celles-ci sont les usuelles.
FRACTIONS_USUELLES = (1.0, 0.5, 1 / 3, 0.25, 0.2, 0.125, 0.1)

#: Le plafond des alertes (`TELEGRAM_MAX_STAKE_PCT`), recopié comme DÉFAUT
#: seulement : l'analyse porte le sien dans ses filtres, pour rester
#: reproductible quel que soit l'environnement.
PLAFOND_DEFAUT_PCT = 3.0
BANKROLL_DEFAUT = 1000.0
FRACTION_DEFAUT = 0.25


@dataclass(frozen=True)
class MiseKelly:
    """La mise Kelly d'une ligne d'analyse. Un objet et non un nombre : la
    mise dépend de la ligne (sa cote, son EV)."""
    bankroll: float = BANKROLL_DEFAUT
    fraction: float = FRACTION_DEFAUT
    plafond_pct: float = PLAFOND_DEFAUT_PCT

    def pour(self, row) -> float:
        """La mise, en euros, sur cette ligne. 0 si la ligne n'a pas d'avantage
        (EV ≤ 0) ou pas de cote juste exploitable."""
        try:
            cote = float(row["odd_taken"])
            juste = float(row["fair_odd"])
        except (KeyError, TypeError, ValueError):
            return 0.0
        if juste <= 1.0 or cote <= 1.0:
            return 0.0
        brute = kelly_stake(cote, 1.0 / juste, self.bankroll, self.fraction)
        return round(min(brute, self.bankroll * self.plafond_pct / 100.0), 2)


def mise_de(stake, row) -> float:
    """La mise d'une ligne : le nombre lui-même en mise fixe, ou la mise Kelly
    de la ligne. Seule porte d'entrée — aucun appelant ne teste le type."""
    return stake.pour(row) if hasattr(stake, "pour") else float(stake)


def est_variable(stake) -> bool:
    """Vrai si la mise dépend de la ligne (Kelly). En mise fixe, les calculs
    gardent leur forme exacte d'origine (`stake × n`), au bit près."""
    return hasattr(stake, "pour")
