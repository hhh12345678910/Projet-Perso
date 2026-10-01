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
ci-dessous, c'est-à-dire si la PÉRIODE du marché d'origine a été identifiée
— prolongation incluse ou temps réglementaire — par son identifiant, dans le
scraper, qui n'émet plus rien d'autre pour ce sport. Tout le reste est écarté ICI, compté
et nommé dans le journal : un book qui n'y figure pas n'est pas « sans
hockey », il est « non vérifié ».

Pour en ajouter un : lancer `scripts/sonde_hockey.py` sur la VM, lire ses
marchés, restreindre son scraper aux identifiants dont on SAIT la période
(prolongation incluse → `h2h`/`totals`, temps réglementaire →
`h2h_reg`/`totals_reg`), puis l'inscrire ici.
"""
from __future__ import annotations

import os
import threading
import time
from collections import Counter

from .models import Book, MarketType, OddQuote

SPORT = "hockey"

#: Le hockey ne passe qu'une fois par intervalle, pas à chaque cycle : il est
#: là pour MESURER une CLV, et la clôture se lit très bien à deux minutes près.
#: Sans ce frein, il ajouterait à chaque cycle ses requêtes Pinnacle — or les
#: appels Pinnacle sont sérialisés entre sports, et c'est ce quota qui
#: bloquait le football en juillet (§2). Réglable : HOCKEY_INTERVAL_SEC.
INTERVALLE_DEFAUT_S = 120.0

#: Book → marchés admis pour le hockey, relevés par `scripts/sonde_hockey.py`
#: le 01/10. DEUX familles, jamais mélangées (types distincts) :
#:
#:   - prolongation et tirs au but INCLUS (`h2h`, `totals`), comparés à la
#:     période 0 de Pinnacle ;
#:   - TEMPS RÉGLEMENTAIRE (`h2h_reg`, `totals_reg`), comparés à sa période 6.
#:
#: Chaque entrée suppose que le scraper du book ne produit, pour le hockey,
#: QUE ces marchés-là, chacun sous le bon type (voir les tables `HOCKEY_*` de
#: chaque scraper). MeridianBet n'y est pas : son parseur ne distingue pas les
#: périodes, et il est de toute façon coupé par BOOKS_DISABLED.
H2H, TOTALS = MarketType.H2H, MarketType.TOTALS
H2H_REG, TOTALS_REG = MarketType.H2H_REG, MarketType.TOTALS_REG
MARCHES_VERIFIES: dict[Book, frozenset] = {
    Book.PINNACLE: frozenset({H2H, TOTALS, H2H_REG, TOTALS_REG}),
    # 478 vainqueur, 19388 total « PROL. ET TAB INCL. ».
    Book.LADBROKES_BE: frozenset({H2H, TOTALS}),
    # Altenar : 406 vainqueur, 412 total (prol. + TAB incl.), 1 = 1X2 réglementaire.
    Book.GOLDEN_PALACE: frozenset({H2H, TOTALS, H2H_REG}),
    Book.STARCASINO_SPORT: frozenset({H2H, TOTALS, H2H_REG}),
    # MW2W vainqueur à deux issues, MW3W 1X2 réglementaire.
    Book.BETFIRST: frozenset({H2H, H2H_REG}),
    # Kambi : « Match Odds - Regular Time » et « Total Goals - Regular Time ».
    Book.UNIBET_BE: frozenset({H2H_REG, TOTALS_REG}),
    # Marché 640 « Full Time », trois issues.
    Book.NAPOLEON_BE: frozenset({H2H_REG}),
    # groupId 1, trois issues.
    Book.VIVATBET: frozenset({H2H_REG}),
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


_DERNIER_PASSAGE: list[float] = []
_VERROU = threading.Lock()


def intervalle_s() -> float:
    try:
        return max(0.0, float(os.getenv("HOCKEY_INTERVAL_SEC", INTERVALLE_DEFAUT_S)))
    except ValueError:
        return INTERVALLE_DEFAUT_S


def doit_passer(maintenant: "float | None" = None) -> bool:
    """Vrai si le hockey doit être scanné à ce cycle — et le note. Faux tant
    que l'intervalle depuis le dernier passage n'est pas écoulé."""
    t = time.monotonic() if maintenant is None else maintenant
    with _VERROU:
        if _DERNIER_PASSAGE and t - _DERNIER_PASSAGE[0] < intervalle_s():
            return False
        _DERNIER_PASSAGE[:] = [t]
        return True


_FOND: list[threading.Thread] = []


def lancer_en_fond(fonction, *args, on_error=None) -> bool:
    """Lance un passage hockey dans un fil détaché, SANS que le cycle
    l'attende. Rien n'est lancé si le passage précédent tourne encore, ni si
    l'intervalle n'est pas écoulé (`doit_passer`, appelé par `fonction`).
    Renvoie True si un fil a été lancé."""
    with _VERROU:
        if _FOND and _FOND[0].is_alive():
            return False

    def corps():
        try:
            fonction(*args)
        except Exception as e:                                  # noqa: BLE001
            if on_error is not None:
                on_error(e)

    fil = threading.Thread(target=corps, name="hockey", daemon=True)
    with _VERROU:
        _FOND[:] = [fil]
    fil.start()
    return True
