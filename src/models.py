from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class MarketType(str, Enum):
    H2H = "h2h"               # 1X2 or moneyline (2-way or 3-way)
    TOTALS = "totals"         # over/under
    HANDICAP = "handicap"     # asian / european handicap
    BTTS = "btts"             # both teams to score
    # Première mi-temps. Types DISTINCTS, et non un champ `period` porté à
    # côté du marché : le devig groupe sur (event_key, market, line), donc un
    # `total 1.5` de mi-temps et un `total 1.5` de match plein ne doivent
    # jamais pouvoir tomber dans le même groupe. Avec des types séparés, la
    # séparation est acquise par construction, et tout code écrit avant eux
    # (`market == MarketType.TOTALS`) continue de ne voir que le match plein.
    # Un chemin non mis à jour IGNORE donc les mi-temps au lieu de les
    # confondre — l'inverse d'un champ oublié dans une clé, qui produirait la
    # panne silencieuse du §21.3.
    #
    # Football seulement : chez Pinnacle `period 1` vaut mi-temps au football
    # mais PREMIER SET au tennis. Les réunir sous un même nom rejouerait la
    # confusion jeux/sets du §19.2.
    H2H_H1 = "h2h_h1"         # 1X2 / moneyline, première mi-temps
    TOTALS_H1 = "totals_h1"   # over/under, première mi-temps


class Book(str, Enum):
    PINNACLE = "pinnacle"
    BETANO_BE = "betano_be"
    UNIBET_BE = "unibet_be"
    LADBROKES_BE = "ladbrokes_be"
    CIRCUS_BE = "circus_be"
    BETFIRST = "betfirst"
    GOLDEN_PALACE = "golden_palace"
    STARCASINO_SPORT = "starcasino_sport"
    SEVEN_ELEVEN_BE = "seven_eleven_be"  # 711.be — Kambi platform, same as Unibet
    BINGOAL_BE = "bingoal_be"            # Bingoal.be — Kambi platform, same as Unibet
    SCOOORE_BE = "scooore_be"            # Scooore.be (Loterie Nationale) — Kambi, same as Unibet
    MERIDIAN_BE = "meridian_be"          # Meridianbet.be — own platform, independent odds
    NAPOLEON_BE = "napoleon_be"          # Napoleonsports.be — Superbet platform, independent odds
    BETCENTER = "betcenter"              # Betcenter.be - Cashpoint/Merkur, cotes independantes
    SMARKETS = "smarkets"        # London exchange, used as a secondary sharp reference
    MAGICBETTING = "magicbetting"  # Magicbetting.be — plateforme Digitain, prix
                                   # réellement indépendants des Kambi et Altenar
    ELITESPORTS = "elitesports"    # Elitesports.be — marque blanche FM Gaming,
                                   # API REST publique sans authentification ni
                                   # anti-bot, et l'IP de datacenter est acceptée


BETANO_OPERATOR_ID = "22"
BETANO_LANGUAGE_ID_FR = "9"


@dataclass(frozen=True)
class Event:
    sport: str
    league: str
    home: str
    away: str
    start_time: datetime
    source_id: str
    book: Book


#: Les marchés de mi-temps. Aucune alerte ne doit en sortir (§21.14) : ils
#: sont collectés, devigués, stockés et suivis en CLV, mais muets.
HALF_TIME_MARKETS = frozenset({MarketType.H2H_H1, MarketType.TOTALS_H1})

#: Les marchés de type over/under, toutes périodes confondues. À utiliser
#: partout où un test portait sur `MarketType.TOTALS` pour une raison de
#: SÉMANTIQUE (labels over/under symétriques), et non de périmètre.
TOTALS_LIKE = frozenset({MarketType.TOTALS, MarketType.TOTALS_H1})

#: Le marché de match plein correspondant. Sert à l'affichage et aux
#: regroupements, jamais à l'appariement — apparier une mi-temps à un match
#: plein est précisément ce que ces types empêchent.
_BASE_MARKET = {
    MarketType.H2H_H1: MarketType.H2H,
    MarketType.TOTALS_H1: MarketType.TOTALS,
}


def is_half_time(market: "MarketType") -> bool:
    """Vrai pour un marché de première mi-temps."""
    return market in HALF_TIME_MARKETS


def base_market(market: "MarketType") -> "MarketType":
    """Le marché de match plein correspondant, ou le marché lui-même."""
    return _BASE_MARKET.get(market, market)


@dataclass(frozen=True)
class Outcome:
    label: str            # e.g. "home", "draw", "away", "over_2.5"
    line: Optional[float] = None


@dataclass(frozen=True)
class OddQuote:
    event_key: str        # normalized key from matcher
    book: Book
    market: MarketType
    outcome: Outcome
    decimal_odd: float
    fetched_at: datetime
    source_event_id: str
    # Human-readable competition ("Suisse - Super League"), when the source
    # provides one. The event_key only carries normalised team names, so this
    # is the only way an alert can name the competition.
    league: Optional[str] = None
    # Clé d'origine, avant réalignement sur la référence. Le tennis tolère
    # jusqu'à 3 h d'écart entre l'heure annoncée par Pinnacle et celle du book,
    # parce qu'un match commence quand le précédent libère le court. Après
    # réalignement, la cote porte l'heure de Pinnacle : si celle-ci est la plus
    # tardive, le match paraît à venir alors qu'il est déjà en cours. Garder
    # l'heure du book permet de retenir la plus précoce des deux.
    book_event_key: Optional[str] = None
    # Score de similarité retenu par le rapprochement flou (0-100), ou 100 pour
    # une égalité exacte de clé. Il était calculé puis jeté : un appariement à
    # 86 et un appariement à 100 n'inspirent pourtant pas la même confiance, et
    # « mauvais matching » est l'une des causes soupçonnées de faux positifs.
    match_score: Optional[float] = None
    # Cette cote vient-elle d'un flux LIVE ? Seul Betano en expose un, et il
    # est fusionné avec son prématch dans la même liste. Sans ce drapeau, une
    # cote live sur un match commencé est indiscernable d'un book qui a oublié
    # de suspendre son marché prématch — or le premier cas est normal et le
    # second est une erreur exploitable.
    from_live_feed: bool = False


@dataclass
class FairLine:
    event_key: str
    market: MarketType
    outcomes: dict[str, float] = field(default_factory=dict)   # label -> fair prob
    method: str = "shin"
    reference_book: Book = Book.PINNACLE
    computed_at: Optional[datetime] = None


@dataclass
class ValueBet:
    event_key: str
    book: Book
    market: MarketType
    outcome: Outcome
    odd_taken: float
    fair_prob: float
    fair_odd: float
    ev_pct: float
    kelly_stake_pct: float
    detected_at: datetime
    league: Optional[str] = None
    # Which sharp source produced the fair line this was measured against.
    # Pinnacle unless it didn't price the market and a fallback stood in — a
    # bet valued against a thinner reference deserves less confidence, and
    # without this the two are indistinguishable after the fact.
    reference_book: Optional[Book] = None
    # Voir OddQuote.book_event_key : l'heure annoncée par le book, quand elle
    # diffère de celle de la référence. Sert à ne pas alerter sur un match déjà
    # commencé selon le book mais pas encore selon Pinnacle.
    book_event_key: Optional[str] = None
    # Twin books offering the exact same price for this bet (e.g. Unibet & 711
    # share the Kambi feed). Listed alongside `book` in the alert so the same
    # opportunity fires once, naming every book where you can take it.
    also_books: tuple[Book, ...] = ()
    # Voir OddQuote.match_score.
    match_score: Optional[float] = None
