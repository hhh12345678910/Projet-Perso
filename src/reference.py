"""Ramener des cotes hétérogènes à un cadre commun avant toute comparaison.

Extrait de `main.py` sans changement de comportement. Tout ce qui est ici
répond à la même question : deux books parlent-ils vraiment du MÊME pari ?

Trois réponses cohabitent, et elles sont distinctes :

- **les books jumeaux** — Unibet, 711, Bingoal et Scooore partagent un seul
  flux (Kambi) et cotent à l'identique. Le même value bet sur les quatre est
  UNE opportunité, pas quatre ;
- **le remappage vers la référence** — deux sources peuvent nommer le même
  match dans l'ordre inverse ; l'issue doit alors être retournée avec lui, sans
  quoi on valorise « home » contre la probabilité de « away » ;
- **la canonicalisation pour surebet** — un surebet se calcule entre deux prix
  du même marché, donc dans le cadre d'un seul et même identifiant d'événement.

⚠️ Module PUR : aucune requête, aucune écriture, aucune alerte. C'est ce qui
le rend partageable avec le futur moteur LIVE — qui aura exactement le même
problème d'identité entre sources, en pire, puisqu'il devra le résoudre pendant
que le match avance.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Iterable

from .matcher import reconcile_event_keys, tolerance_for, wide_tolerance_for
from .models import Book, MarketType, OddQuote, Outcome, TOTALS_LIKE, ValueBet





# Books that share a single odds feed (Kambi): Unibet and 711 price identically,
# so the same value bet on both is one opportunity, not two. UNIBET is the
# canonical book kept for storage/dedup; 711 rides along in `also_books`.
_TWIN_BOOK_GROUPS: tuple[tuple[Book, ...], ...] = (
    (Book.UNIBET_BE, Book.SEVEN_ELEVEN_BE, Book.BINGOAL_BE, Book.SCOOORE_BE),
)
_TWIN_PRIMARY = {grp: grp[0] for grp in _TWIN_BOOK_GROUPS}
_TWIN_OF = {b: grp for grp in _TWIN_BOOK_GROUPS for b in grp}


def merge_twin_book_value_bets(bets: list[ValueBet]) -> list[ValueBet]:
    """Collapse identical value bets coming from twin books (same Kambi feed,
    same price) into a single alert that names every book. Non-twin bets and
    twin bets that don't have a same-priced sibling pass through untouched."""
    twins: dict[tuple, list[ValueBet]] = defaultdict(list)
    out: list[ValueBet] = []
    for b in bets:
        if b.book in _TWIN_OF:
            key = (b.event_key, b.market, b.outcome.label, b.outcome.line,
                   round(b.odd_taken, 4), _TWIN_OF[b.book])
            twins[key].append(b)
        else:
            out.append(b)

    for key, group in twins.items():
        twin_group = key[5]
        primary_book = _TWIN_PRIMARY[twin_group]
        books_present = {b.book for b in group}
        # Keep the primary book's record if present, else the first seen.
        base = next((b for b in group if b.book == primary_book), group[0])
        extras = tuple(b for b in twin_group if b in books_present and b != base.book)
        out.append(replace(base, also_books=extras))
    return out


_OPPOSITE_OUTCOME = {"home": "away", "away": "home"}


def _flip_outcome_for_swap(outcome: Outcome, market: MarketType) -> Outcome:
    """When the matcher had to swap home/away to align a soft-book event_key
    with the Pinnacle reference, any outcome labels carried by quotes from
    that event are now pointing at the wrong team in the reference frame.
    Flip home↔away (draw stays); the totals over/under labels are
    team-symmetric so they pass through unchanged."""
    # TOTALS_LIKE, pas TOTALS : un `totals_h1` a les mêmes labels
    # over/under symétriques, et tomberait sinon dans la branche home↔away.
    if market in TOTALS_LIKE:
        return outcome
    flipped_label = _OPPOSITE_OUTCOME.get(outcome.label, outcome.label)
    return replace(outcome, label=flipped_label)


def remap_to_reference(
    soft_quotes: list[OddQuote],
    reference_keys: Iterable[str],
    sport: str | None = None,
    *,
    wide_tolerance_minutes: int | None = None,
) -> list[OddQuote]:
    """Re-key soft-book quotes onto the matching Pinnacle event_key via fuzzy
    matching, so they line up with the fair lines. When the matcher detects
    that the candidate listed the teams in swapped order (e.g. soft book has
    'Senegal vs Nigeria' while Pinnacle has 'Nigeria vs Senegal'), the home
    /away outcome labels are flipped on the way out so the rest of the
    pipeline compares apples to apples. Unmatched quotes are dropped."""
    scores: dict[str, float] = {}
    soft_to_ref = reconcile_event_keys(
        reference_keys=list(reference_keys),
        candidate_keys={q.event_key for q in soft_quotes},
        time_tolerance_minutes=tolerance_for(sport),
        scores=scores,
        wide_tolerance_minutes=wide_tolerance_minutes,
    )
    out: list[OddQuote] = []
    for q in soft_quotes:
        match = soft_to_ref.get(q.event_key)
        if match is None:
            continue
        ref_key, swap = match
        score = scores.get(q.event_key)
        flipped_outcome = _flip_outcome_for_swap(q.outcome, q.market) if swap else q.outcome
        if ref_key == q.event_key and not swap:
            out.append(replace(q, match_score=score))
        else:
            # book_event_key retient l'heure annoncée par le book : après
            # réalignement, event_key porte celle de la référence, qui peut
            # être postérieure de plusieurs heures au tennis.
            out.append(replace(q, event_key=ref_key, outcome=flipped_outcome,
                               book_event_key=q.book_event_key or q.event_key,
                               match_score=score))
    return out


def align_reference_source(
    quotes: list[OddQuote],
    reference_keys: Iterable[str],
    sport: str | None = None,
) -> list[OddQuote]:
    """Aligner une source sharp SECONDAIRE sur les clés de la référence, sans
    jamais rien perdre.

    `remap_to_reference` **jette** ce qu'elle n'apparie pas, ce qui convient à
    un book soft : une cote qu'on ne sait pas rattacher à une ligne juste ne
    peut servir à rien. Pour une source de repli c'est exactement l'inverse —
    les non-appariés sont les matchs que la référence principale ne price pas,
    c'est-à-dire toute sa raison d'être.

    Les deux moitiés ont donc chacune leur usage :
      - appariés   -> re-clés sur l'événement Pinnacle, donc comparables dans
                      `odds_history` et couverts par la ligne Pinnacle ;
      - autres     -> gardés tels quels, et c'est d'eux que naissent les
                      lignes de référence en repli.

    Sans cet alignement, une cote Smarkets porte la clé issue de SES noms et de
    SON horaire ; sur un match que Pinnacle price aussi, les deux clés diffèrent
    et les courbes ne se rejoignent jamais. Mesuré avant correctif : 5 points
    d'historique pour Smarkets contre 1 200 à 2 900 pour les autres books."""
    aligned = remap_to_reference(
        quotes, reference_keys, sport,
        # Fenêtre élargie réservée à ce chemin. Mesuré sur Smarkets : six
        # matchs de tennis aux noms STRICTEMENT identiques étaient rejetés sur
        # le seul horaire, et chacun fabriquait ensuite une ligne de repli sur
        # un match que Pinnacle price — l'inverse exact de la règle « Pinnacle
        # d'abord ». Le rapprochement des books soft n'est pas touché : il est
        # mesuré, il fonctionne, et l'élargir serait une décision séparée.
        wide_tolerance_minutes=wide_tolerance_for(sport),
    )
    matched_src = {(q.book_event_key or q.event_key) for q in aligned}
    unmatched = [q for q in quotes if q.event_key not in matched_src]
    return aligned + unmatched




# Books used as sharp references, never as something to bet on: they price the
# fair line rather than being where a mispricing is hunted. Smarkets is listed
# because the exchange scraper still exists — it was wired in as a fallback
# reference and removed again after a refresh was measured taking 26 minutes
# and stalling scan cycles. Should it ever come back, it must land here and not
# in the soft-book pool.
SHARP_BOOKS = frozenset({Book.PINNACLE, Book.SMARKETS})


def canonicalize_for_surebets(
    pinnacle_q: list[OddQuote],
    soft_raw: list[OddQuote],
    sport: str | None = None,
) -> list[OddQuote]:
    """Re-key every quote (Pinnacle + soft books) onto a unified canonical key
    set so surebets can be found across books even on events Pinnacle does NOT
    price.

    Unlike remap_to_reference (which anchors on Pinnacle and drops anything
    Pinnacle doesn't list), this lets soft books anchor each other: Pinnacle
    keys seed the canonical set when present (cleanest team names), then each
    soft book is reconciled one at a time against the growing reference. The
    first book to price an event Pinnacle lacks becomes that event's anchor,
    and later books fuzzy-match onto it. Quotes that match adopt the anchor key
    (home/away flipped when the match was swapped); unmatched events seed new
    anchors so the next book can still align with them.

    This is for surebet detection only — value bets still need a Pinnacle fair
    line, so they keep using remap_to_reference."""
    canonical: list[OddQuote] = list(pinnacle_q)  # Pinnacle keeps its own keys
    ref_keys: set[str] = {q.event_key for q in pinnacle_q}

    # Reconcile each book as a unit so a book never matches against itself.
    by_book: dict[Book, list[OddQuote]] = defaultdict(list)
    for q in soft_raw:
        by_book[q.book].append(q)

    for _book, quotes in by_book.items():
        mapping = reconcile_event_keys(
            reference_keys=list(ref_keys),
            candidate_keys={q.event_key for q in quotes},
            time_tolerance_minutes=tolerance_for(sport),
        )
        new_anchor_keys: set[str] = set()
        for q in quotes:
            match = mapping.get(q.event_key)
            if match is None:
                # No match anywhere yet — this event becomes its own anchor so
                # subsequent books can align onto it.
                canonical.append(q)
                new_anchor_keys.add(q.event_key)
                continue
            ref_key, swap = match
            flipped = _flip_outcome_for_swap(q.outcome, q.market) if swap else q.outcome
            if ref_key == q.event_key and not swap:
                canonical.append(q)
            else:
                canonical.append(replace(q, event_key=ref_key, outcome=flipped))
        ref_keys |= new_anchor_keys
    return canonical
