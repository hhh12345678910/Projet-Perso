"""Ligne juste LIVE, reconstruite à partir des books qui ont déjà repricé.

Le moteur du projet compare une cote soft à une référence sharp. En direct
cette référence n'existe pas : le scraper Pinnacle ignore délibérément les
matchs en cours, donc dès le coup d'envoi il n'y a plus rien contre quoi
mesurer quoi que ce soit.

Ce module fabrique une référence de substitution. Sur un match commencé, les
autres books, eux, pricent en direct — et le daemon collecte déjà leurs cotes à
chaque cycle. En dévigeant ceux qui ont bougé depuis le coup d'envoi, on obtient
une probabilité live approximative, suffisante pour répondre à la seule question
qui compte ici : le book resté figé propose-t-il encore un prix que le match a
rendu absurde ?

Ce n'est PAS une référence sharp, et il ne faut pas l'utiliser comme telle. Les
marges live des books belges tournent autour de 8-12 % contre 5-6 % en prématch,
et la moyenne de plusieurs books soft reste une moyenne de books soft. D'où le
seuil élevé imposé en aval : on cherche des marchés qu'un but a déjà tranchés,
pas des edges à 3 %.
"""
from __future__ import annotations

from .devig import devig

# Un seul book « vivant » ne prouve rien : il peut lui-même être en train de se
# tromper. Deux qui bougent ensemble dans le même sens, c'est le marché.
MIN_CONSENSUS_BOOKS = 2

# Un marché incomplet ne se dévige pas. Un 1X2 dont il manque le nul donne une
# somme d'implicites sous 1 et produirait des probabilités inventées ; un
# overround délirant signale un relevé abîmé. Les deux bornes valident donc le
# relevé avant de s'en servir, sans avoir à savoir combien d'issues ce marché
# est censé porter.
MIN_OVERROUND = 1.0
MAX_OVERROUND = 1.5


def book_probs(odds_by_outcome: dict[str, float], *, method: str = "shin") -> dict[str, float] | None:
    """Probabilités dévigées d'UN book sur UN marché, ou None si inexploitable."""
    labels = [k for k, v in odds_by_outcome.items() if v and v > 1.0]
    if len(labels) < 2:
        return None
    odds = [odds_by_outcome[k] for k in labels]
    implied = sum(1.0 / o for o in odds)
    if not (MIN_OVERROUND < implied <= MAX_OVERROUND):
        return None
    try:
        probs = devig(odds, method=method)
    except (ValueError, ZeroDivisionError):
        return None
    if not probs or any(p <= 0.0 or p >= 1.0 for p in probs):
        return None
    return dict(zip(labels, probs))


def consensus_probs(
    odds_by_book: dict[object, dict[str, float]],
    *,
    method: str = "shin",
    min_books: int = MIN_CONSENSUS_BOOKS,
) -> dict[str, float] | None:
    """Probabilité live moyenne sur les books fournis, ou None si trop peu.

    La moyenne porte sur les PROBABILITÉS dévigées, pas sur les cotes : moyenner
    des cotes mélange des marges différentes et penche vers le book le plus
    gourmand. Chaque book est dévigé chez lui, puis on moyenne des grandeurs
    enfin comparables.

    Seules les issues présentes chez tous les books retenus sont renvoyées — une
    issue vue une seule fois n'a pas de consensus, par définition.
    """
    per_book = []
    for odds in odds_by_book.values():
        probs = book_probs(odds, method=method)
        if probs is not None:
            per_book.append(probs)
    if len(per_book) < min_books:
        return None
    shared = set(per_book[0])
    for probs in per_book[1:]:
        shared &= set(probs)
    if not shared:
        return None
    return {
        label: sum(p[label] for p in per_book) / len(per_book)
        for label in shared
    }


def edge_pct(odd: float, fair_prob: float) -> float:
    """Écart entre une cote figée et la probabilité live, en points d'EV."""
    return (odd * fair_prob - 1.0) * 100.0
