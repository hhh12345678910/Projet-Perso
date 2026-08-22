"""Le cœur du calcul : ligne juste, puis espérance positive.

Extrait de `main.py` (5 691 lignes) sans une virgule de changement de
comportement — mêmes fonctions, mêmes signatures, mêmes constantes, mêmes
commentaires. Le découpage sert un but précis : ces fonctions sont **communes
au prématch et au futur moteur LIVE**. Les laisser dans un module qui importe
vingt scrapers, Typer et la boucle du daemon obligerait le LIVE à traîner tout
ça, et surtout à démarrer un second processus sur le même fichier.

⚠️ `main.py` réimporte tout ce module. `from src.main import find_value_bets`
continue donc de fonctionner à l'identique — c'est la forme qu'emploient 67
fichiers de test et c'est la garantie que le découpage n'a rien cassé.

Ce qui est ICI est pur : des cotes entrent, des probabilités et des paris
sortent. Aucun appel réseau, aucune écriture en base, aucun envoi Telegram.
C'est ce qui rend le module partageable — et testable sans rien simuler.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

from .config import ScanConfig
from .devig import devig
from .ev import ev_pct, fair_odd, kelly_fraction
from .matcher import parse_event_key
from .models import Book, FairLine, MarketType, OddQuote, ValueBet
from .ui import console


def _group_quotes(quotes: Iterable[OddQuote]) -> dict[tuple[str, MarketType, float | None], list[OddQuote]]:
    """Group Pinnacle quotes into competing-outcome sets per (event, market, line)."""
    groups: dict[tuple[str, MarketType, float | None], list[OddQuote]] = defaultdict(list)
    for q in quotes:
        groups[(q.event_key, q.market, q.outcome.line)].append(q)
    return groups


# Marge maximale tolérée sur une ligne de RÉFÉRENCE, par nombre d'issues.
#
# Une référence sharp cote serré : Pinnacle tourne autour de 2 à 3 % sur un
# deux-voies, 5 à 7 % sur un 1X2, et un exchange encore moins. Bien au-delà,
# ce n'est plus un prix de marché.
#
# ⚠️ Mesuré le 16/08 sur Hodd — Strommen : Smarkets cotait over 6,5 à 1,98 et
# under 6,5 à 1,07, soit 144 % de marge, alors que sa propre ligne 5,5 donnait
# over à 10,66. Une cote « over » qui REDESCEND en montant la ligne n'est pas
# un prix : c'est une offre isolée que personne n'a prise, ce qu'un carnet
# d'ordres affiche quand même. Pinnacle s'arrêtant à 4,5 sur ce match, le repli
# secondaire s'est déclenché précisément là où l'exchange est illiquide, et a
# pris la pire ligne du carnet pour référence — d'où une « juste » à 3,53 sur
# un événement à 2 %, et des EV de +260 % sur cinq matchs.
#
# Le contrôle porte sur la MARGE et non sur la liquidité : le carnet ne nous
# dit pas les volumes, mais une marge aberrante trahit la même chose, sans rien
# demander de plus.
_MAX_OVERROUND = {2: 1.20, 3: 1.30}
_MAX_OVERROUND_DEFAULT = 1.40
_thin_reference_seen: set = set()


def _overround_ok(group: list[OddQuote]) -> bool:
    """La somme des probabilités implicites est-elle celle d'un vrai marché ?"""
    try:
        book_sum = sum(1.0 / q.decimal_odd for q in group if q.decimal_odd > 1.0)
    except ZeroDivisionError:
        return False
    if book_sum <= 1.0:
        # Sous 100 %, c'est un surebet interne à la référence — anormal aussi,
        # mais on laisse passer : c'est le signal que cherche find_surebets, et
        # le couper ici le ferait disparaître ailleurs.
        return True
    return book_sum <= _MAX_OVERROUND.get(len(group), _MAX_OVERROUND_DEFAULT)


def _devig_group(group: list[OddQuote], method: str) -> dict[str, float] | None:
    """Run a devig on one (event, market, line) group's odds. Returns the
    label -> fair probability map, or None if the group is too thin or
    numerically degenerate."""
    if len(group) < 2:
        return None
    # Écarter AVANT de déviger. Le devig normalise à 100 % quoi qu'on lui
    # donne : nourri d'une ligne à 144 % de marge, il rend une probabilité
    # d'apparence normale, et l'aberration devient indétectable en aval. C'est
    # exactement la panne du §11 — une entrée fausse, une sortie plausible,
    # aucune erreur.
    if not _overround_ok(group):
        # Signalé une fois par (book, marché, ligne) et non par événement : la
        # clé reste minuscule sur un daemon qui tourne des semaines, et c'est
        # le MOTIF qui intéresse — « Smarkets déraille sur les totaux 6,5 » se
        # lit une fois, pas trois cents.
        key = (group[0].book, group[0].market, group[0].outcome.line)
        if key not in _thin_reference_seen:
            _thin_reference_seen.add(key)
            book_sum = sum(1.0 / q.decimal_odd for q in group if q.decimal_odd > 1.0)
            console.print(
                f"[yellow]Référence écartée : {group[0].book.value} "
                f"{group[0].market.value} ligne {group[0].outcome.line} à "
                f"{book_sum * 100:.0f}% de marge ("
                + ", ".join(f"{q.outcome.label}@{q.decimal_odd:.2f}" for q in group)
                + ")[/yellow]"
            )
        return None
    try:
        probs = devig([q.decimal_odd for q in group], method=method)
    except Exception:
        return None
    return {q.outcome.label: p for q, p in zip(group, probs)}


def build_event_rows(event_keys: Iterable[str], sport: str,
                     league_by_event: dict[str, str]) -> list[tuple]:
    """Les lignes `events` d'un cadre de référence.

    Extrait de la boucle du daemon pour être testable : la règle qu'elle porte
    — un pari ne doit jamais exister sans son match — s'était perdue en
    silence, et rien ne l'aurait rattrapée."""
    rows = []
    for ek in event_keys:
        parsed = parse_event_key(ek)
        if parsed is None:
            continue
        start, home_norm, away_norm = parsed
        rows.append((ek, sport, league_by_event.get(ek, ""),
                     home_norm, away_norm, start.isoformat()))
    return rows


def build_fair_lines(
    pinnacle_quotes: list[OddQuote],
    method: str,
    *,
    secondary_quotes: list[OddQuote] | None = None,
) -> dict[tuple[str, MarketType, float | None], FairLine]:
    """Build fair lines from Pinnacle, with a secondary sharp source used
    strictly as a fallback.

    Where Pinnacle prices a market, its devigged line is used alone. It is the
    reference precisely because it is the most accurate source available;
    averaging it with anything less accurate can only move the estimate away
    from the truth, and would quietly change what "fair" means on the events
    that matter most.

    The secondary is therefore only consulted for markets Pinnacle does not
    price at all. Those fair lines are tagged with their actual source
    (reference_book), so a bet valued against the fallback is distinguishable
    from one valued against Pinnacle."""
    primary_groups = _group_quotes(pinnacle_quotes)
    secondary_groups = _group_quotes(secondary_quotes or [])

    fair: dict[tuple[str, MarketType, float | None], FairLine] = {}
    now = datetime.now(timezone.utc)
    for key in primary_groups.keys() | secondary_groups.keys():
        event_key_, market, line = key
        pin_probs = _devig_group(primary_groups.get(key, []), method)
        sec_probs = _devig_group(secondary_groups.get(key, []), method)

        # Pinnacle wins outright whenever it prices the market — no blending,
        # and no borrowing a label from the secondary either: if Pinnacle lists
        # a 2-way market, a "draw" from an exchange's 3-way one doesn't belong
        # in the same fair line.
        if pin_probs:
            outcomes = pin_probs
            ref_book = Book.PINNACLE
        else:
            outcomes = sec_probs  # type: ignore[assignment]
            # Read the source off the quotes rather than naming one: nothing
            # feeds this today, and hardcoding a particular book would be wrong
            # the moment something else does.
            group = secondary_groups.get(key) or []
            ref_book = group[0].book if group else Book.PINNACLE

        if not outcomes:
            continue
        fair[key] = FairLine(
            event_key=event_key_,
            market=market,
            outcomes=outcomes,
            method=method,
            reference_book=ref_book,
            computed_at=now,
        )
    return fair


def _kickoff(q: OddQuote) -> datetime | None:
    """Heure de coup d'envoi la plus précoce parmi celles connues.

    Deux sources peuvent diverger de plusieurs heures au tennis. Retenir la
    plus précoce fait qu'un match déjà commencé selon l'une des deux est
    traité comme commencé : c'est le sens sûr de l'erreur, puisque la ligne de
    référence est prématch et cesse d'avoir un sens au coup d'envoi."""
    starts = []
    for key in (q.event_key, q.book_event_key):
        if not key:
            continue
        parsed = parse_event_key(key)
        if parsed is not None:
            starts.append(parsed[0])
    return min(starts) if starts else None


def find_value_bets(
    candidate_quotes: list[OddQuote],
    fair_lines: dict[tuple[str, MarketType, float | None], FairLine],
    cfg: ScanConfig,
) -> list[ValueBet]:
    # Pre-count distinct outcome labels per (event, market, line, book).
    # If a soft book offers fewer outcomes than Pinnacle's fair line (e.g.
    # hockey 2-way OT-included on Pinnacle vs 3-way regulation 1X2 on soft
    # books), the markets are structurally incompatible and must be skipped.
    from collections import defaultdict
    _soft_labels: dict[tuple, set[str]] = defaultdict(set)
    for q in candidate_quotes:
        if q.book != Book.PINNACLE:
            _soft_labels[(q.event_key, q.market, q.outcome.line, q.book)].add(q.outcome.label)
    soft_outcome_counts = {k: len(v) for k, v in _soft_labels.items()}

    out: list[ValueBet] = []
    now = datetime.now(timezone.utc)
    for q in candidate_quotes:
        if q.book == Book.PINNACLE:
            continue
        # Événements déjà commencés : voir ScanConfig.scan_live_value_bets. La
        # ligne de référence est prématch, donc figée au coup d'envoi ; l'écart
        # mesuré ensuite ne dit rien. Un event_key illisible passe quand même —
        # mieux vaut un pari de trop qu'un rejet silencieux sur un défaut de
        # format.
        if not cfg.scan_live_value_bets:
            start = _kickoff(q)
            if start is not None and start <= now:
                continue
        # Handicap markets are excluded for now: line semantics vary across
        # books (Pinnacle signs each side, e.g. home -1.0 / away +1.0, while
        # soft books carry both sides at the same |line|). That mismatch pairs
        # non-complementary lines in the devig and surfaces phantom value bets
        # (huge "EV" on away -1.0 etc.). Surebets already skip handicaps for the
        # same reason; mirror that here until per-book line conventions are
        # normalised.
        if q.market == MarketType.HANDICAP:
            continue
        fl = fair_lines.get((q.event_key, q.market, q.outcome.line))
        if fl is None:
            continue
        # Skip if the soft book doesn't offer the same number of outcomes as
        # the Pinnacle fair line (market structure mismatch).
        n_soft = soft_outcome_counts.get((q.event_key, q.market, q.outcome.line, q.book), 0)
        if n_soft != len(fl.outcomes):
            continue
        p = fl.outcomes.get(q.outcome.label)
        if p is None or p <= 0 or p >= 1:
            continue
        ev = ev_pct(q.decimal_odd, p)
        if ev < cfg.min_ev_pct or ev > cfg.max_ev_pct:
            continue
        out.append(ValueBet(
            event_key=q.event_key,
            book=q.book,
            market=q.market,
            outcome=q.outcome,
            odd_taken=q.decimal_odd,
            fair_prob=p,
            fair_odd=fair_odd(p),
            ev_pct=ev,
            kelly_stake_pct=kelly_fraction(q.decimal_odd, p) * cfg.kelly_fraction * 100.0,
            detected_at=now,
            league=q.league,
            reference_book=fl.reference_book,
            book_event_key=q.book_event_key,
            match_score=q.match_score,
        ))
    return out
