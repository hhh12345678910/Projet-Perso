"""Le hockey : ce qui peut être comparé à Pinnacle, et ce qui ne le peut pas.

LE PIÈGE
--------
Pinnacle, la référence, price le hockey PROLONGATION ET TIRS AU BUT INCLUS :
un vainqueur à deux issues, et des totaux qui comptent la prolongation (le
tir au but vaut un but pour le vainqueur). Les books belges publient AUSSI,
et souvent en marché principal, un 1X2 sur le TEMPS RÉGLEMENTAIRE (trois
issues) et parfois des totaux sans prolongation.

Comparer un prix « temps réglementaire » à une ligne juste « prolongation
incluse » fabrique de la valeur qui n'existe pas — sans la moindre erreur :

  - un 1X2 dont le nul manque ce cycle-là (suspendu, cote ≤ 1) ne garde que
    domicile/extérieur, passe le contrôle du nombre d'issues, et ressort à
    +15 à +30 % d'EV fictive ;
  - un total réglementaire a deux issues comme celui de Pinnacle : le
    contrôle est aveugle. Il montre de la fausse valeur sur les Over 4.5 et
    6.5 mais pas sur les 5.5 (un nul après 60 minutes a un total pair, la
    prolongation ajoute un but) : l'erreur paraît plausible à l'œil.

Et la CLV ne le verrait pas : la clôture est lue sur le même marché Pinnacle
que la détection, donc une erreur de marché donne une fausse EV ET une fausse
CLV du même montant. La base ne garde pas l'identifiant du marché d'origine :
on ne peut pas trier après coup. Les correspondances doivent être justes
AVANT de collecter.

LA RÈGLE
--------
Pour le hockey, une cote n'entre que si son book ET son marché figurent
ci-dessous, c'est-à-dire si le marché d'origine a été identifié comme
« prolongation incluse » — par son identifiant, dans le scraper, qui
n'émet plus rien d'autre pour ce sport. Tout le reste est écarté ICI, compté
et nommé dans le journal : un book qui n'y figure pas n'est pas « sans
hockey », il est « non vérifié ».

Pour en ajouter un : lancer `scripts/sonde_hockey.py` sur la VM, lire ses
marchés, restreindre son scraper aux identifiants « prolongation incluse »
pour le hockey, puis l'inscrire ici.
"""
from __future__ import annotations

from collections import Counter

from .models import Book, MarketType, OddQuote

SPORT = "hockey"

#: Book → marchés admis pour le hockey. Chaque entrée suppose que le scraper
#: du book ne produit, pour le hockey, QUE ses marchés « prolongation
#: incluse » (voir le commentaire de chacun).
MARCHES_VERIFIES: dict[Book, frozenset] = {
    Book.PINNACLE: frozenset({MarketType.H2H, MarketType.TOTALS}),
    # Ladbrokes : betId 478 « VAINQUEUR », deux issues — un vainqueur sans nul
    # au hockey inclut forcément la prolongation et les tirs au but. Le total
    # 19388 est annoncé « incl. OT » sans dire si le tir au but compte pour un
    # but : il reste écarté jusqu'à la sonde (voir `ladbrokes.HOCKEY_BET_IDS`).
    Book.LADBROKES_BE: frozenset({MarketType.H2H}),
    # Altenar (Golden Palace, StarCasino) : typeId 406 « Vainqueur (prol. + TAB
    # incl.) » et 412 « Total de buts (prol. + TAB incl.) », relevés sur des
    # HAR réels ; le 1X2 réglementaire (typeId 1) et le total 18 sont écartés
    # par le parseur (voir `goldenpalace.HOCKEY_TYPE_IDS`).
    Book.GOLDEN_PALACE: frozenset({MarketType.H2H, MarketType.TOTALS}),
    Book.STARCASINO_SPORT: frozenset({MarketType.H2H, MarketType.TOTALS}),
}


def filtrer(quotes: list[OddQuote]) -> tuple[list[OddQuote], Counter]:
    """Les cotes de hockey admissibles, et le compte de celles écartées par
    book — pour que le journal dise ce qui manque au lieu de le taire."""
    gardees: list[OddQuote] = []
    ecartees: Counter = Counter()
    for q in quotes:
        if q.market in MARCHES_VERIFIES.get(q.book, ()):
            gardees.append(q)
        else:
            ecartees[q.book.value] += 1
    return gardees, ecartees
