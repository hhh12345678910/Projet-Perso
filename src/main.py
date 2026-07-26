from __future__ import annotations

import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

import threading
import time

import httpx
import typer
from rich.console import Console
from rich.table import Table

from .config import ScanConfig
from .devig import devig
from .ev import ev_pct, fair_odd, kelly_fraction, kelly_stake
from .matcher import parse_event_key, reconcile_event_keys
from .models import Book, FairLine, MarketType, OddQuote, Outcome, ValueBet
from .scrapers.betano import (
    BetanoAuthError,
    BetanoScraper,
    parse_overview as betano_parse_overview,
    parse_prematch as betano_parse_prematch,
    prematch_file as betano_prematch_file,
)
from .scrapers.betcenter import BetCenterScraper
from .scrapers.betfirst import BetFirstScraper, parse_events_table as betfirst_parse_events_table
from .scrapers.goldenpalace import GoldenPalaceScraper, parse_get_events as goldenpalace_parse_get_events
from .scrapers.ladbrokes import LadbrokesScraper, parse_prematch as ladbrokes_parse_prematch
from .scrapers.pinnacle import PinnacleScraper
from .scrapers.smarkets import SmarketsScraper, iter_all_quotes as smarkets_iter_all_quotes
from .scrapers.sevenelevenbe import SevenElevenScraper, parse_listview as sevenelevenbe_parse_listview
from .scrapers.bingoal import BingoalScraper, parse_listview as bingoal_parse_listview
from .scrapers.scooore import ScoooreScraper, parse_listview as scooore_parse_listview
from .scrapers.meridianbet import MeridianScraper, parse_offer as meridian_parse_offer
from .scrapers.napoleon import NapoleonScraper, parse_by_date as napoleon_parse_by_date
from .scrapers.starcasinosport import StarCasinoSportScraper, parse_get_events as starcasinosport_parse_get_events
from .scrapers.unibet import UnibetScraper, parse_listview as unibet_parse_listview
from .storage import Storage
from .surebet import find_surebets, Surebet
from .middle import find_middles, Middle
from .clv import (
    aggregate as clv_aggregate,
    clv_pct,
    event_started,
    group_by as clv_group_by,
    index_quotes_by_market,
)
from .alerter import TelegramConfig, send_alerts, send_surebet_alerts, send_clv_alerts, send_middle_alerts
from . import teams


app = typer.Typer(add_completion=False)
console = Console()


def _group_quotes(quotes: Iterable[OddQuote]) -> dict[tuple[str, MarketType, float | None], list[OddQuote]]:
    """Group Pinnacle quotes into competing-outcome sets per (event, market, line)."""
    groups: dict[tuple[str, MarketType, float | None], list[OddQuote]] = defaultdict(list)
    for q in quotes:
        groups[(q.event_key, q.market, q.outcome.line)].append(q)
    return groups


def _devig_group(group: list[OddQuote], method: str) -> dict[str, float] | None:
    """Run a devig on one (event, market, line) group's odds. Returns the
    label -> fair probability map, or None if the group is too thin or
    numerically degenerate."""
    if len(group) < 2:
        return None
    try:
        probs = devig([q.decimal_odd for q in group], method=method)
    except Exception:
        return None
    return {q.outcome.label: p for q, p in zip(group, probs)}


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
            ref_book = Book.SMARKETS

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
        ))
    return out


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
    if market == MarketType.TOTALS:
        return outcome
    flipped_label = _OPPOSITE_OUTCOME.get(outcome.label, outcome.label)
    return replace(outcome, label=flipped_label)


def remap_to_reference(
    soft_quotes: list[OddQuote],
    reference_keys: Iterable[str],
) -> list[OddQuote]:
    """Re-key soft-book quotes onto the matching Pinnacle event_key via fuzzy
    matching, so they line up with the fair lines. When the matcher detects
    that the candidate listed the teams in swapped order (e.g. soft book has
    'Senegal vs Nigeria' while Pinnacle has 'Nigeria vs Senegal'), the home
    /away outcome labels are flipped on the way out so the rest of the
    pipeline compares apples to apples. Unmatched quotes are dropped."""
    soft_to_ref = reconcile_event_keys(
        reference_keys=list(reference_keys),
        candidate_keys={q.event_key for q in soft_quotes},
    )
    out: list[OddQuote] = []
    for q in soft_quotes:
        match = soft_to_ref.get(q.event_key)
        if match is None:
            continue
        ref_key, swap = match
        flipped_outcome = _flip_outcome_for_swap(q.outcome, q.market) if swap else q.outcome
        if ref_key == q.event_key and not swap:
            out.append(q)
        else:
            out.append(replace(q, event_key=ref_key, outcome=flipped_outcome))
    return out


# Books used as sharp references, never as something to bet on. They price the
# fair line; they're not soft books whose mistakes we're hunting.
SHARP_BOOKS = frozenset({Book.PINNACLE, Book.SMARKETS})


def align_secondary_sharp(
    pinnacle_q: list[OddQuote],
    secondary_raw: list[OddQuote],
) -> list[OddQuote]:
    """Re-key a second sharp source onto Pinnacle's frame.

    Two jobs at once. Where both price the same event, the quotes must land on
    the same event_key or build_fair_lines would treat them as separate events
    and never blend them. Where Pinnacle is absent, the event keeps its own key
    and becomes the anchor for a fair line Pinnacle can't provide — which is
    the entire point of a secondary sharp source."""
    mapping = reconcile_event_keys(
        reference_keys=[q.event_key for q in pinnacle_q],
        candidate_keys={q.event_key for q in secondary_raw},
    )
    out: list[OddQuote] = []
    for q in secondary_raw:
        match = mapping.get(q.event_key)
        if match is None:
            out.append(q)
            continue
        ref_key, swap = match
        if ref_key == q.event_key and not swap:
            out.append(q)
        else:
            flipped = _flip_outcome_for_swap(q.outcome, q.market) if swap else q.outcome
            out.append(replace(q, event_key=ref_key, outcome=flipped))
    return out


def canonicalize_for_surebets(
    pinnacle_q: list[OddQuote],
    soft_raw: list[OddQuote],
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


def fetch_betano_quotes(
    betano_file: str | None = None,
    sport: str | None = None,
    include_live: bool = True,
) -> list[OddQuote]:
    """Parse Betano data from the browser userscript's pushes.

    Two feeds, because Betano exposes two unrelated APIs:
      - the live overview (danae-webapi), all sports in one dump, in-play only;
      - the prematch offer (/fr/api/sport/{slug}/matchs-a-venir), one payload
        per sport, which is where the fixtures the engine actually prices live.

    DataDome scores the requesting IP, so neither can be fetched from the VM —
    both arrive via tools/betano-ingest.user.js. The live path falls back to a
    direct fetch (useful only from a residential IP) when no dump is present."""
    import json as _json
    import time as _time
    from pathlib import Path as _Path

    def _load(path: str, max_age_min: float, label: str) -> dict | None:
        """Load a pushed file, refusing it once it's too old to trust.

        Both feeds only advance while a browser tab is open on betanosports.be
        — nothing on the VM can refresh them. If that tab closes or the machine
        sleeps, the files simply stop changing, and without this check the
        daemon would keep pricing hours-old odds as if they were live, with no
        signal that anything was wrong. Staleness is the silent failure mode
        this whole design has, so it's worth failing loudly on."""
        p = _Path(path)
        try:
            raw = p.read_text()
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as e:
            console.print(f"[yellow]Betano {label} unreadable ({path}):[/yellow] {e}")
            return None
        if max_age_min > 0:
            try:
                age_min = (_time.time() - p.stat().st_mtime) / 60
            except OSError:
                age_min = 0.0
            if age_min > max_age_min:
                console.print(
                    f"[red]Betano {label} périmé ({age_min:.0f} min > {max_age_min:.0f}) "
                    f"— onglet Betano fermé ? Données ignorées.[/red]"
                )
                return None
        try:
            return _json.loads(raw)
        except ValueError as e:
            console.print(f"[yellow]Betano {label} invalide ({path}):[/yellow] {e}")
            return None

    # Live odds go stale in minutes; prematch prices move slowly enough that a
    # much longer window is still usable. One threshold for both would either
    # accept dead in-play odds or throw away good prematch ones.
    live_max_age = float(os.getenv("BETANO_LIVE_MAX_AGE_MIN", "5"))
    prematch_max_age = float(os.getenv("BETANO_PREMATCH_MAX_AGE_MIN", "30"))

    quotes: list[OddQuote] = []

    if include_live:
        if betano_file:
            data = _load(betano_file, live_max_age, "live")
            if data is not None:
                quotes.extend(betano_parse_overview(data))
        else:
            try:
                with BetanoScraper() as bet:
                    quotes.extend(betano_parse_overview(bet.fetch_live_overview()))
            except BetanoAuthError as e:
                console.print(f"[yellow]Betano live skipped:[/yellow] {e}")

    if sport:
        pm = _load(betano_prematch_file(sport), prematch_max_age, f"prématch {sport}")
        if pm is not None:
            unknown: set[str] = set()
            quotes.extend(betano_parse_prematch(pm, unknown_types=unknown))
            if unknown:
                # Surface unmapped codes: the set differs per sport, so a new
                # one means quotes are being dropped silently.
                console.print(
                    f"[yellow]Betano prematch ({sport}): unmapped market codes "
                    f"{sorted(unknown)} — add them to _PREMATCH_MARKET_BY_TYPE.[/yellow]"
                )
    return quotes


# Smarkets needs one request per event, paced 0.5s apart to stay under its
# per-IP cap. Measured on a live install: a refresh took ~26 MINUTES, and
# because it ran inside the scan cycle it blocked that sport entirely — no
# alerts at all for the duration. Freshness here is worth far less than that:
# this source is only consulted for markets Pinnacle doesn't price, which are
# prematch prices that drift slowly.
#
# So the cycle never waits for it. It reads whatever snapshot exists and, when
# that snapshot is stale, kicks off a background refresh for next time. The
# first cycle after startup therefore has no fallback lines, which is the
# correct trade: a missing fallback costs a few events, a blocked cycle costs
# every alert on that sport.
_SMARKETS_CACHE: dict[str, tuple[float, list[OddQuote]]] = {}
_SMARKETS_REFRESHING: set[str] = set()
_SMARKETS_LOCK = threading.Lock()


def _smarkets_refresh(sport: str) -> None:
    """Background worker. Never raises — a failure leaves the previous snapshot
    in place and only resets the clock, so one bad scrape doesn't drop the
    fallback and doesn't trigger an immediate retry storm either."""
    quotes: list[OddQuote] | None = None
    try:
        with SmarketsScraper() as sm:
            quotes = list(smarkets_iter_all_quotes(
                sm, sport, max_events=int(os.getenv("SMARKETS_MAX_EVENTS", "150")),
            ))
    except Exception as e:
        console.print(f"[yellow]Smarkets ({sport}) refresh échoué : {e}[/yellow]")
    finally:
        with _SMARKETS_LOCK:
            previous = _SMARKETS_CACHE.get(sport)
            kept = quotes if quotes is not None else (previous[1] if previous else [])
            _SMARKETS_CACHE[sport] = (time.monotonic(), kept)
            _SMARKETS_REFRESHING.discard(sport)


def fetch_smarkets_quotes(sport: str) -> list[OddQuote]:
    """Smarkets exchange mid-prices, used as a *fallback* sharp reference.

    Returns immediately, always: the last snapshot, refreshed out of band.
    Set USE_SMARKETS=1 to enable, SMARKETS_TTL_S to tune the refresh interval,
    SMARKETS_MAX_EVENTS to bound how much one refresh walks."""
    ttl = float(os.getenv("SMARKETS_TTL_S", "900"))
    now = time.monotonic()
    with _SMARKETS_LOCK:
        cached = _SMARKETS_CACHE.get(sport)
        stale = cached is None or (now - cached[0]) >= ttl
        launch = stale and sport not in _SMARKETS_REFRESHING
        if launch:
            _SMARKETS_REFRESHING.add(sport)
    if launch:
        threading.Thread(
            target=_smarkets_refresh, args=(sport,),
            name=f"smarkets-{sport}", daemon=True,
        ).start()
    return list(cached[1]) if cached is not None else []


def fetch_unibet_quotes(sport: str) -> list[OddQuote]:
    """Fetch + parse every Unibet (Kambi) event by iterating each leaf termKey
    under the sport's group — far better coverage than the bare listView."""
    try:
        with UnibetScraper() as uni:
            data = uni.fetch_all_events(sport)
        return list(unibet_parse_listview(data))
    except httpx.HTTPError as e:
        console.print(f"[yellow]Unibet skipped:[/yellow] {e}")
        return []


def fetch_sevenelevenbe_quotes(sport: str) -> list[OddQuote]:
    """Fetch + parse every 711 (Kambi) event — same coverage strategy as Unibet,
    just a different operator code on the shared Kambi offering API."""
    try:
        with SevenElevenScraper() as se:
            data = se.fetch_all_events(sport)
        return list(sevenelevenbe_parse_listview(data))
    except httpx.HTTPError as e:
        console.print(f"[yellow]711 skipped:[/yellow] {e}")
        return []


def fetch_bingoal_quotes(sport: str) -> list[OddQuote]:
    """Fetch + parse every Bingoal (Kambi) event — same coverage strategy as
    Unibet/711, just a different operator code on the shared Kambi offering API."""
    try:
        with BingoalScraper() as bg:
            data = bg.fetch_all_events(sport)
        return list(bingoal_parse_listview(data))
    except httpx.HTTPError as e:
        console.print(f"[yellow]Bingoal skipped:[/yellow] {e}")
        return []


def fetch_meridian_quotes(sport: str) -> list[OddQuote]:
    """Fetch + parse the MeridianBet prematch offer for a sport (own REST API,
    independent odds — genuinely widens value/surebet coverage)."""
    try:
        with MeridianScraper() as mb:
            data = mb.fetch_all_events(sport)
        return list(meridian_parse_offer(data))
    except httpx.HTTPError as e:
        console.print(f"[yellow]MeridianBet skipped:[/yellow] {e}")
        return []


def fetch_scooore_quotes(sport: str) -> list[OddQuote]:
    """Fetch + parse every Scooore (Kambi) event — same coverage strategy as
    Unibet/711/Bingoal, just a different operator code (bnlbe)."""
    try:
        with ScoooreScraper() as sc:
            data = sc.fetch_all_events(sport)
        return list(scooore_parse_listview(data))
    except httpx.HTTPError as e:
        console.print(f"[yellow]Scooore skipped:[/yellow] {e}")
        return []


def fetch_napoleon_quotes(sport: str) -> list[OddQuote]:
    """Fetch + parse Napoleon's prematch 1X2 offer (Superbet platform, public
    REST, independent odds — genuinely widens value/surebet coverage)."""
    try:
        with NapoleonScraper() as nap:
            data = nap.fetch_by_date(sport)
        return list(napoleon_parse_by_date(data))
    except httpx.HTTPError as e:
        console.print(f"[yellow]Napoleon skipped:[/yellow] {e}")
        return []


def fetch_betfirst_quotes(sport: str) -> list[OddQuote]:
    """Fetch + parse the BetFirst events-table for a sport (paginated)."""
    try:
        with BetFirstScraper() as bf:
            data = bf.fetch_all_events(sport, days_ahead=7, max_market_count=10)
        return list(betfirst_parse_events_table(data))
    except httpx.HTTPError as e:
        console.print(f"[yellow]BetFirst skipped:[/yellow] {e}")
        return []


def fetch_ladbrokes_quotes(sport: str) -> list[OddQuote]:
    """Fetch + parse every Ladbrokes meeting of a sport via the detail-service."""
    try:
        with LadbrokesScraper() as lb:
            data = lb.fetch_all_meetings(sport, max_meetings=80)
        return list(ladbrokes_parse_prematch(data))
    except httpx.HTTPError as e:
        console.print(f"[yellow]Ladbrokes skipped:[/yellow] {e}")
        return []



def fetch_goldenpalace_quotes(sport: str) -> list[OddQuote]:
    """Bulk-fetch every Golden Palace event for a sport via the Altenar widget."""
    try:
        with GoldenPalaceScraper() as gp:
            data = gp.fetch_events(sport)
        return list(goldenpalace_parse_get_events(data))
    except httpx.HTTPError as e:
        console.print(f"[yellow]Golden Palace skipped:[/yellow] {e}")
        return []


def fetch_starcasinosport_quotes(sport: str) -> list[OddQuote]:
    """Bulk-fetch StarCasino Sport via the same Altenar widget."""
    try:
        with StarCasinoSportScraper() as ss:
            data = ss.fetch_events(sport)
        return list(starcasinosport_parse_get_events(data))
    except httpx.HTTPError as e:
        console.print(f"[yellow]StarCasino Sport skipped:[/yellow] {e}")
        return []


def fetch_betcenter_quotes(sport: str) -> list[OddQuote]:
    """Fetch + parse the Betcenter prematch offer (Cashpoint/Merkur platform,
    own odds API on oddsservice.betcenter.be -- independent odds)."""
    try:
        with BetCenterScraper() as bc:
            return bc.fetch_all_quotes(sport)
    except httpx.HTTPError as e:
        console.print(f"[yellow]Betcenter skipped:[/yellow] {e}")
        return []


def _fetch_all_parallel(
    sport: str,
    betano_file: str | None = None,
    *,
    include_file_books: bool = True,
) -> list[OddQuote]:
    """Fetch Pinnacle + all soft books concurrently. Returns the merged list;
    callers split by book to route Pinnacle/Smarkets as sharp references."""
    def _pinnacle() -> list[OddQuote]:
        with PinnacleScraper() as pin:
            return list(pin.fetch_market_quotes(sport))

    tasks: dict[str, Callable[[], list[OddQuote]]] = {
        "Pinnacle":      _pinnacle,
        "Unibet":        lambda: fetch_unibet_quotes(sport),
        # "711":           lambda: fetch_sevenelevenbe_quotes(sport),  # Kambi jumeau d'Unibet -> desactive (anti rate-limit)
        # "Bingoal":       lambda: fetch_bingoal_quotes(sport),  # Kambi jumeau d'Unibet -> desactive (anti rate-limit)
        # "Scooore":       lambda: fetch_scooore_quotes(sport),  # Kambi jumeau d'Unibet -> desactive (anti rate-limit)
        # MeridianBet: scraper prêt mais l'API exige un token (anti-bot
        # TrafficGuard) -> réactiver ici une fois le token capturé.
        # "MeridianBet": lambda: fetch_meridian_quotes(sport),
        # BetFirst: desactive — 403 Forbidden sur l'events-table depuis le VPS,
        # malgre le sessiontoken invite + les en-tetes x-sb-*. L'anti-bot bloque
        # desormais l'IP datacenter, meme probleme que Betano. Reactiver
        # uniquement si le blocage tombe ou via un push navigateur.
        # "BetFirst":      lambda: fetch_betfirst_quotes(sport),
        "Ladbrokes":     lambda: fetch_ladbrokes_quotes(sport),
        "StarCasino":    lambda: fetch_starcasinosport_quotes(sport),
        "Napoleon":      lambda: fetch_napoleon_quotes(sport),
        # Smarkets is a sharp reference, not a soft book — see SHARP_BOOKS.
        **({"Smarkets": lambda: fetch_smarkets_quotes(sport)}
           if os.getenv("USE_SMARKETS", "").strip() not in ("", "0", "false") else {}),
        # "Betcenter":     lambda: fetch_betcenter_quotes(sport),  # desactive: cotes erronees
        # Golden Palace retiré: compte limité, plus exploitable.
    }
    # The live dump mixes every sport, so it's parsed once (on the sport that
    # owns include_file_books) to avoid duplicating it across sport threads.
    # The prematch feed is per-sport and must run every time.
    tasks["Betano"] = lambda: fetch_betano_quotes(
        betano_file=betano_file, sport=sport, include_live=include_file_books,
    )

    all_quotes: list[OddQuote] = []
    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {executor.submit(fn): name for name, fn in tasks.items()}
        for future in as_completed(futures):
            name = futures[future]
            try:
                quotes = future.result()
                all_quotes.extend(quotes)
                if quotes:
                    console.print(f"\\[{sport}]   → {len(quotes):5d} quotes  {name}")
            except Exception as e:
                console.print(f"[yellow]\\[{sport}]   {name} skipped: {e}[/yellow]")
    # Drop handicap quotes at the source: they're excluded from both value bets
    # and surebets, so parsing/storing/matching them is pure wasted CPU (and
    # they're ~45% of Pinnacle's payload). Filtering here lightens the whole
    # downstream pipeline — critical on a small CPU.
    return [q for q in all_quotes if q.market != MarketType.HANDICAP]


@app.command()
def scan(
    sport: str = "soccer",
    min_ev: float = 2.0,
    bankroll: float = 1000.0,
    betano_file: str = typer.Option(
        None,
        "--betano-file",
        help="Path to a JSON dump of Betano's /danae-webapi/.../live/overview/latest "
        "response, captured from your browser. Bypasses the IP-bound cookie check.",
    ),
):
    """Fetch Pinnacle + soft books (Betano, Unibet, 711, Bingoal, BetFirst, Ladbrokes, StarCasino), compute fair lines, print top value bets.

    --sport accepts a comma-separated list (e.g. 'soccer,tennis,basketball').
    The full pipeline runs per sport and results are tagged in their own
    section so per-sport coverage stays visible."""
    sports = [s.strip() for s in sport.split(",") if s.strip()]
    storage = Storage(ScanConfig().db_path)
    teams.init(storage)

    for current_sport in sports:
        cfg = ScanConfig(sport=current_sport, min_ev_pct=min_ev, bankroll=bankroll)
        console.print()
        console.print(f"[bold green]══ {current_sport.upper()} ══[/bold green]")

        console.print(f"[bold]Fetching all books in parallel ({current_sport})...[/bold]")
        all_quotes = _fetch_all_parallel(
            current_sport, betano_file,
            include_file_books=(current_sport == sports[0]),
        )

        quotes         = [q for q in all_quotes if q.book == Book.PINNACLE]
        raw_soft       = [q for q in all_quotes if q.book != Book.PINNACLE]

        fair = build_fair_lines(quotes, cfg.devig_method)
        console.print(f"  → {len(fair)} fair lines (devig={cfg.devig_method}, sharp=Pinnacle)")

        storage.insert_quotes(quotes)

        sport = current_sport  # keep local var name for downstream prints

        ref_keys = {fl.event_key for fl in fair.values()}
        soft_quotes = remap_to_reference(raw_soft, ref_keys)
        console.print(f"  → {len(soft_quotes)} matched to a Pinnacle event")
        storage.insert_quotes(soft_quotes)

        bets = merge_twin_book_value_bets(find_value_bets(soft_quotes, fair, cfg))
        bets.sort(key=lambda b: b.ev_pct, reverse=True)
        console.print(f"[bold]Value bets: {len(bets)}[/bold]")

        for b in bets:
            storage.insert_value_bet(b)

        tg_cfg = TelegramConfig.from_env()
        if tg_cfg is not None:
            candidates = [
                b for b in bets
                if storage.value_bet_notify_count(
                    b.event_key, b.book.value, b.market.value, b.outcome.label, b.outcome.line,
                ) < tg_cfg.valuebet_max_alerts
                and (
                    not tg_cfg.valuebet_dedup
                    or not storage.value_bet_already_notified(
                        b.event_key, b.book.value, b.market.value, b.outcome.label, b.outcome.line,
                        current_ev_pct=b.ev_pct,
                        ev_delta_pct=tg_cfg.valuebet_ev_delta_pct,
                    )
                )
            ]
            sent = send_alerts(candidates, tg_cfg, print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"), sport=current_sport)
            now = datetime.now(timezone.utc)
            for b in sent:
                storage.mark_value_bet_notified(
                    b.event_key, b.book.value, b.market.value, b.outcome.label, b.outcome.line,
                    b.ev_pct, now,
                )
            if sent:
                console.print(f"  → {len(sent)} Telegram alerts sent (EV ≥ {tg_cfg.min_ev_pct:.1f}%)")

        table = Table(title=f"Value bets ({sport}, min_ev={min_ev}%)", show_lines=False)
        table.add_column("event_key", overflow="fold")
        table.add_column("book")
        table.add_column("market")
        table.add_column("outcome")
        table.add_column("odd", justify="right")
        table.add_column("fair", justify="right")
        table.add_column("EV%", justify="right")
        table.add_column("stake%", justify="right")
        for b in bets[:25]:
            line = f" {b.outcome.line}" if b.outcome.line is not None else ""
            table.add_row(
                b.event_key, b.book.value, b.market.value, f"{b.outcome.label}{line}",
                f"{b.odd_taken:.2f}", f"{b.fair_odd:.2f}", f"{b.ev_pct:.2f}", f"{b.kelly_stake_pct:.2f}",
            )
        console.print(table)

        # Cross-book surebet detection. We include Pinnacle's own quotes in
        # the candidate pool — they're rarely the best odd per outcome (tight
        # margin) but on the occasional event where Pinnacle is the only
        # source for a side, or its odd happens to drift, the arb shows up
        # in the table. find_surebets already enforces a distinct-book-per-leg
        # rule, so Pinnacle-vs-Pinnacle "arbs" can't slip through.
        # Canonicalize across all books so events Pinnacle doesn't price still
        # yield surebets when two soft books cover both sides.
        surebets = find_surebets(canonicalize_for_surebets(quotes, raw_soft))
        plausible = [s for s in surebets if not s.suspicious]
        flagged = [s for s in surebets if s.suspicious]
        console.print(
            f"[bold]Surebets: {len(plausible)} plausible[/bold]"
            + (f" (+ {len(flagged)} flagged as suspicious — likely matching bugs)" if flagged else "")
        )

        # Telegram surebet alerts. The candidate pool depends on whether the
        # user opted into seeing suspicious ones; dedup is configurable too,
        # so a user who wants every scan to re-confirm can disable it.
        # Final per-margin filtering happens inside the alerter.
        if tg_cfg is not None and surebets:
            candidates = surebets if tg_cfg.include_suspicious_surebets else plausible
            candidates = [
                s for s in candidates
                if storage.surebet_notify_count(s.event_key, s.market.value, s.line)
                < tg_cfg.surebet_max_alerts
                and (
                    not tg_cfg.surebet_dedup
                    or not storage.surebet_already_notified(
                        s.event_key, s.market.value, s.line,
                        current_margin_pct=s.margin * 100,
                        roi_delta_pct=tg_cfg.surebet_roi_delta_pct,
                    )
                )
            ]
            if candidates:
                sent = send_surebet_alerts(
                    candidates, tg_cfg,
                    print_fn=lambda x: console.print(f"[yellow]{x}[/yellow]"),
                    sport=current_sport,
                )
                now = datetime.now(timezone.utc)
                for s in sent:
                    storage.mark_surebet_notified(
                        s.event_key, s.market.value, s.line, s.margin * 100, now,
                    )
                if sent:
                    console.print(f"  → {len(sent)} surebet alerts sent")
        if plausible:
            st = Table(title=f"Surebets ({sport})", show_lines=False)
            st.add_column("event_key", overflow="fold")
            st.add_column("market")
            st.add_column("line")
            st.add_column("legs", overflow="fold")
            st.add_column("margin%", justify="right")
            st.add_column("ROI%", justify="right")
            for s in plausible[:15]:
                legs_str = " | ".join(
                    f"{label}={odd:.2f} ({book.value})" for label, (odd, book) in s.legs.items()
                )
                st.add_row(
                    s.event_key,
                    s.market.value,
                    str(s.line) if s.line is not None else "-",
                    legs_str,
                    f"{s.margin * 100:.2f}",
                    f"{s.roi * 100:.2f}",
                )
            console.print(st)


@app.command(name="scan-surebets")
def scan_surebets(
    sport: str = "soccer",
    betano_file: str = typer.Option(
        None, "--betano-file",
        help="Optional Betano dump path — same as in `scan`.",
    ),
):
    """Surebet sweep including Pinnacle, designed to run every 5-15 min.
    Pinnacle quotes are fetched and used as the canonical event-key reference,
    then included in the surebet candidate pool — same as the full `scan`.
    Comma-separated --sport lets one cron entry cover every sport you care about."""
    sports = [s.strip() for s in sport.split(",") if s.strip()]
    storage = Storage(ScanConfig().db_path)
    teams.init(storage)
    tg_cfg = TelegramConfig.from_env()

    for current_sport in sports:
        console.print()
        console.print(f"[bold green]══ {current_sport.upper()} (surebets) ══[/bold green]")

        console.print(f"[bold]Fetching all books in parallel ({current_sport})...[/bold]")
        all_quotes = _fetch_all_parallel(
            current_sport, betano_file,
            include_file_books=(current_sport == sports[0]),
        )

        pinnacle_quotes = [q for q in all_quotes if q.book == Book.PINNACLE]
        soft_quotes     = [q for q in all_quotes if q.book != Book.PINNACLE]

        if not all_quotes:
            continue

        # Canonicalize across ALL books so surebets surface even on events
        # Pinnacle doesn't price — soft books anchor each other when Pinnacle is
        # absent. Pinnacle stays in the pool so Pinnacle-leg arbs are detected.
        normalised_quotes = canonicalize_for_surebets(pinnacle_quotes, soft_quotes)
        console.print(f"  → {len(normalised_quotes)} quotes matched to a common event")

        surebets = find_surebets(normalised_quotes)
        plausible = [s for s in surebets if not s.suspicious]
        flagged = [s for s in surebets if s.suspicious]
        console.print(
            f"[bold]Surebets: {len(plausible)} plausible[/bold]"
            + (f" (+ {len(flagged)} suspicious)" if flagged else "")
        )

        if tg_cfg is None or not surebets:
            continue

        candidates = surebets if tg_cfg.include_suspicious_surebets else plausible
        candidates = [
            s for s in candidates
            if storage.surebet_notify_count(s.event_key, s.market.value, s.line)
            < tg_cfg.surebet_max_alerts
            and (
                not tg_cfg.surebet_dedup
                or not storage.surebet_already_notified(
                    s.event_key, s.market.value, s.line,
                    current_margin_pct=s.margin * 100,
                    roi_delta_pct=tg_cfg.surebet_roi_delta_pct,
                )
            )
        ]
        if not candidates:
            continue
        sent = send_surebet_alerts(
            candidates, tg_cfg,
            print_fn=lambda x: console.print(f"[yellow]{x}[/yellow]"),
            sport=current_sport,
        )
        now = datetime.now(timezone.utc)
        for s in sent:
            storage.mark_surebet_notified(
                s.event_key, s.market.value, s.line, s.margin * 100, now,
            )
        if sent:
            console.print(f"  → {len(sent)} surebet alerts sent")


def _daemon_scan_sport(
    current_sport: str,
    storage: Storage,
    tg_cfg: "TelegramConfig | None",
    min_ev: float,
    bankroll: float,
    betano_file: "str | None",
) -> None:
    """Fetch, analyse and alert for one sport. Runs inside a ThreadPoolExecutor
    so all sports execute concurrently every cycle. SQLite WAL mode lets multiple
    threads read simultaneously; concurrent writes serialise via the 10 s timeout
    built into Storage._conn(), so no external locking is needed."""
    vb_to_mark: list[ValueBet] = []
    sb_to_mark: list[Surebet] = []
    mid_to_mark: list[Middle] = []
    now_mark = datetime.now(timezone.utc)

    try:
        console.print(f"\n[bold]{current_sport.upper()}[/bold]")
        # Betano is a soccer-only file-based scraper.
        all_q = _fetch_all_parallel(
            current_sport, betano_file,
            include_file_books=(current_sport == "soccer"),
        )
        pinnacle_q = [q for q in all_q if q.book == Book.PINNACLE]
        secondary_raw = [q for q in all_q if q.book == Book.SMARKETS]
        soft_raw   = [q for q in all_q if q.book not in SHARP_BOOKS]

        if not pinnacle_q and not secondary_raw:
            console.print(f"[yellow]\\[{current_sport}]   No sharp quotes — skipping[/yellow]")
            return

        cfg = ScanConfig(sport=current_sport, min_ev_pct=min_ev, bankroll=bankroll)
        fair = build_fair_lines(
            pinnacle_q, cfg.devig_method,
            secondary_quotes=align_secondary_sharp(pinnacle_q, secondary_raw) or None,
        )
        # Persist the event (with its sport) for every Pinnacle event in the
        # reference frame. Value bets are keyed onto these same event_keys, so
        # this lets clv-report break CLV down per sport instead of "unknown".
        event_rows = []
        for ek in {q.event_key for q in pinnacle_q}:
            parsed = parse_event_key(ek)
            if parsed is not None:
                start, home_norm, away_norm = parsed
                event_rows.append((ek, current_sport, "", home_norm, away_norm, start.isoformat()))
        storage.upsert_events(event_rows)
        storage.insert_quotes(pinnacle_q)

        ref_keys = {fl.event_key for fl in fair.values()}
        soft_q = remap_to_reference(soft_raw, ref_keys)
        storage.insert_quotes(soft_q)

        # ── CLV pre-kickoff alerts ────────────────────────────────────
        if tg_cfg is not None and tg_cfg.clv_window_minutes > 0:
            now_utc = datetime.now(timezone.utc)
            pin_idx: dict[tuple, float] = {}
            for _q in pinnacle_q:
                if "::" in _q.event_key:
                    _d = _q.event_key[:8]
                    _t = _q.event_key.split("::", 1)[1]
                    pin_idx[(_d, _t, _q.market.value, _q.outcome.label, _q.outcome.line)] = _q.decimal_odd

            clv_pending: list[tuple] = []
            for _bet in storage.open_value_bets():
                _parsed = parse_event_key(_bet["event_key"])
                if _parsed is None:
                    continue
                _kickoff, _, _ = _parsed
                _mins = (_kickoff - now_utc).total_seconds() / 60
                if not (0 < _mins <= tg_cfg.clv_window_minutes):
                    continue
                if storage.clv_alert_already_notified(int(_bet["id"])):
                    continue
                if "::" not in _bet["event_key"]:
                    continue
                _d = _bet["event_key"][:8]
                _t = _bet["event_key"].split("::", 1)[1]
                _pin_odd = pin_idx.get((_d, _t, _bet["market"], _bet["outcome_label"], _bet["line"]))
                if _pin_odd is None:
                    continue
                # Only alert on bet odds in the configured range (default
                # 1.5-4.0): below 1.5 the stake-to-reward is poor, above 4.0
                # the variance is too high for a small bankroll.
                _odd_taken = float(_bet["odd_taken"])
                if not (tg_cfg.clv_min_odd <= _odd_taken <= tg_cfg.clv_max_odd):
                    continue
                _clv = (_odd_taken / _pin_odd - 1) * 100
                if _clv < tg_cfg.min_clv_pct:
                    continue
                clv_pending.append((_bet, _clv, _pin_odd, int(_mins)))

            if clv_pending:
                clv_sent = send_clv_alerts(
                    clv_pending, tg_cfg,
                    print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
                    sport=current_sport,
                )
                # Mark only the CLV alerts that actually went out, so a
                # rate-limited/failed send is retried on a later cycle.
                for _bet_row, _clv_pct, _pin_odd, _mins in clv_sent:
                    storage.mark_clv_alert_notified(int(_bet_row["id"]), _clv_pct, _pin_odd, now_utc)
                if clv_sent:
                    console.print(f"\\[{current_sport}]   → {len(clv_sent)} CLV alert(s) sent")

        # ── Value bets ───────────────────────────────────────────────
        bets = merge_twin_book_value_bets(find_value_bets(soft_q, fair, cfg))
        bets.sort(key=lambda b: b.ev_pct, reverse=True)
        for b in bets:
            storage.insert_value_bet(b)
        console.print(f"\\[{current_sport}]   value bets: {len(bets)} total")
        if tg_cfg is not None:
            # The hard alert cap ALWAYS applies (even with the EV-delta dedup
            # turned off); the EV-delta dedup is an extra filter on top.
            vb_candidates = [
                b for b in bets
                if storage.value_bet_notify_count(
                    b.event_key, b.book.value, b.market.value, b.outcome.label, b.outcome.line,
                ) < tg_cfg.valuebet_max_alerts
                and (
                    not tg_cfg.valuebet_dedup
                    or not storage.value_bet_already_notified(
                        b.event_key, b.book.value, b.market.value, b.outcome.label, b.outcome.line,
                        current_ev_pct=b.ev_pct,
                        ev_delta_pct=tg_cfg.valuebet_ev_delta_pct,
                    )
                )
            ]
            sent = send_alerts(
                vb_candidates, tg_cfg,
                print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
                sport=current_sport,
            )
            now_mark = datetime.now(timezone.utc)
            # Mark only what actually sent — deferred/rate-limited bets stay
            # unmarked and get retried next cycle instead of being lost.
            vb_to_mark = sent
            if sent:
                console.print(f"\\[{current_sport}]   → {len(sent)} value bet alert(s) sent")

        # ── Surebets ─────────────────────────────────────────────────
        # Surebets use a wider pool than value bets: events Pinnacle doesn't
        # price still count, as long as two distinct books cover both sides.
        surebet_pool = canonicalize_for_surebets(pinnacle_q, soft_raw)
        surebets = find_surebets(surebet_pool)
        plausible = [s for s in surebets if not s.suspicious]
        console.print(f"\\[{current_sport}]   surebets: {len(plausible)} plausible")
        if tg_cfg is not None and surebets:
            sb_pool = surebets if tg_cfg.include_suspicious_surebets else plausible
            # Same dedup model as value bets: a hard alert cap that ALWAYS
            # applies, plus the optional ROI-delta dedup on top.
            sb_candidates = [
                s for s in sb_pool
                if storage.surebet_notify_count(s.event_key, s.market.value, s.line)
                < tg_cfg.surebet_max_alerts
                and (
                    not tg_cfg.surebet_dedup
                    or not storage.surebet_already_notified(
                        s.event_key, s.market.value, s.line,
                        current_margin_pct=s.margin * 100,
                        roi_delta_pct=tg_cfg.surebet_roi_delta_pct,
                    )
                )
            ]
            if sb_candidates:
                sent_sb = send_surebet_alerts(
                    sb_candidates, tg_cfg,
                    print_fn=lambda x: console.print(f"[yellow]{x}[/yellow]"),
                    sport=current_sport,
                )
                sb_to_mark = sent_sb
                if sent_sb:
                    console.print(f"\\[{current_sport}]   → {len(sent_sb)} surebet alert(s) sent")

        # ── Middles ──────────────────────────────────────────────────
        # Totals middles priced against Pinnacle's devigged ladder. Uses the
        # remapped soft quotes (aligned to the Pinnacle reference keys) so the
        # gap probability lookup hits the same event_key as `fair`.
        if tg_cfg is not None:
            middles = find_middles(
                soft_q, fair,
                min_ev_pct=tg_cfg.min_middle_ev_pct,
                max_gap=tg_cfg.middle_max_gap,
            )
            console.print(f"\\[{current_sport}]   middles: {len(middles)}")
            if middles:
                mid_candidates = [
                    m for m in middles
                    if storage.middle_notify_count(m.event_key, m.low_line, m.high_line)
                    < tg_cfg.middle_max_alerts
                    and (
                        not tg_cfg.middle_dedup
                        or not storage.middle_already_notified(
                            m.event_key, m.low_line, m.high_line,
                            current_ev_pct=m.ev_pct,
                            ev_delta_pct=tg_cfg.middle_ev_delta_pct,
                        )
                    )
                ]
                if mid_candidates:
                    sent_mid = send_middle_alerts(
                        mid_candidates, tg_cfg,
                        print_fn=lambda x: console.print(f"[yellow]{x}[/yellow]"),
                        sport=current_sport,
                    )
                    mid_to_mark = sent_mid
                    if sent_mid:
                        console.print(f"\\[{current_sport}]   → {len(sent_mid)} middle alert(s) sent")

    except Exception as e:
        console.print(f"[red]  {current_sport} error: {e}[/red]")

    # ── Persist dedup marks outside the sport catch so a scraper/analysis
    # failure never prevents already-sent alerts from being recorded. ──────
    try:
        for b in vb_to_mark:
            storage.mark_value_bet_notified(
                b.event_key, b.book.value, b.market.value, b.outcome.label, b.outcome.line,
                b.ev_pct, now_mark,
            )
        for s in sb_to_mark:
            storage.mark_surebet_notified(
                s.event_key, s.market.value, s.line, s.margin * 100, now_mark,
            )
        for m in mid_to_mark:
            storage.mark_middle_notified(
                m.event_key, m.low_line, m.high_line, m.ev_pct, now_mark,
            )
    except Exception as mark_err:
        console.print(
            f"[red]  dedup mark failed for {current_sport} — "
            f"next cycle may re-alert: {mark_err}[/red]"
        )


@app.command()
def daemon(
    sport: str = "soccer,tennis,hockey",
    min_ev: float = typer.Option(
        5.0, "--min-ev",
        help="Minimum EV% to detect/store a value bet. Defaults to 5 to match "
        "the main chat's lower bound; the 5-8% bucket still shows ~+12% CLV. "
        "Below 5% is where the volume that re-saturated Telegram lives.",
    ),
    bankroll: float = 1000.0,
    breather: int = typer.Option(
        10, "--breather",
        help="Seconds to pause between cycle end and next start.",
    ),
    betano_file: str = typer.Option(
        None, "--betano-file",
        help="Path to a Betano JSON dump — same as in `scan`.",
    ),
):
    """Continuous scan: fetch all books in parallel, detect value bets + surebets,
    alert on Telegram only when something new or changed. Loops forever — run
    under systemd (scripts/valuebet-daemon.service) or screen/tmux.

    All sports are scanned concurrently (one thread per sport) so the cycle time
    equals the slowest single-sport fetch, not their sum."""
    sports_list = [s.strip() for s in sport.split(",") if s.strip()]
    storage = Storage(ScanConfig().db_path)
    teams.init(storage)

    cycle = 0
    while True:
        cycle += 1
        t0 = datetime.now(timezone.utc)
        console.print(f"\n[bold green]══ CYCLE {cycle} — {t0.strftime('%H:%M:%S')} UTC ══[/bold green]")
        tg_cfg = TelegramConfig.from_env()

        # Run every sport concurrently — cycle time = max(sport_time) not sum.
        with ThreadPoolExecutor(max_workers=len(sports_list)) as executor:
            futs = {
                executor.submit(
                    _daemon_scan_sport,
                    sp, storage, tg_cfg, min_ev, bankroll, betano_file,
                ): sp
                for sp in sports_list
            }
            for f in as_completed(futs):
                try:
                    f.result()
                except Exception as e:
                    console.print(f"[red]Sport thread {futs[f]} crashed: {e}[/red]")

        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        console.print(f"\n[dim]Cycle {cycle} done in {elapsed:.0f}s — next in {breather}s[/dim]")
        time.sleep(breather)


@app.command(name="alert-test")
def alert_test():
    """Send one dummy alert per Telegram channel to verify all chats are wired up:
    value bet → TELEGRAM_CHAT_ID, surebet → TELEGRAM_SUREBET_CHAT_ID,
    CLV → TELEGRAM_CLV_CHAT_ID (each falls back to the main chat if not set)."""
    cfg = TelegramConfig.from_env()
    if cfg is None:
        console.print(
            "[yellow]TELEGRAM_BOT_TOKEN and/ou TELEGRAM_CHAT_ID non définis "
            "— rien à envoyer.[/yellow]"
        )
        return

    test_ev  = cfg.min_ev_pct + 1.0
    test_roi = cfg.min_surebet_margin_pct / 100 + 0.005

    sample_bet = ValueBet(
        event_key="202606010000::testteamA__vs__testteamB",
        book=Book.UNIBET_BE,
        market=MarketType.H2H,
        outcome=Outcome(label="home"),
        odd_taken=1.86,
        fair_prob=0.5650,
        fair_odd=1.77,
        ev_pct=test_ev,
        kelly_stake_pct=1.50,
        detected_at=datetime.now(timezone.utc),
    )
    # Prematch surebet: kickoff in the future
    sample_surebet_prematch = Surebet(
        event_key="202607010000::testteamA__vs__testteamB",
        market=MarketType.H2H,
        line=None,
        legs={
            "home": (1.95, Book.UNIBET_BE),
            "draw": (3.85, Book.BETFIRST),
            "away": (4.20, Book.LADBROKES_BE),
        },
        margin=test_roi,
    )
    # Live surebet: kickoff in the past
    sample_surebet_live = Surebet(
        event_key="202601010000::testteamA__vs__testteamB",
        market=MarketType.H2H,
        line=None,
        legs={
            "home": (1.95, Book.UNIBET_BE),
            "draw": (3.85, Book.BETFIRST),
            "away": (4.20, Book.LADBROKES_BE),
        },
        margin=test_roi,
    )
    # CLV test: dummy bet row as plain dict (same interface as sqlite3.Row)
    sample_clv_bet: dict = {
        "id": 0,
        "event_key": "202607010000::testteamA__vs__testteamB",
        "book": Book.UNIBET_BE.value,
        "market": MarketType.H2H.value,
        "outcome_label": "home",
        "line": None,
        "odd_taken": 1.86,
        "kelly_pct": 1.50,
    }
    # Premium channel samples: a big value bet (>min_premium_ev, odds in band)
    # and a juicy prematch surebet (>min_premium_surebet).
    premium_ev = max(cfg.min_premium_ev_pct + 1.0, cfg.min_ev_pct + 1.0)
    sample_premium_bet = ValueBet(
        event_key="202607010000::testteamA__vs__testteamB",
        book=Book.UNIBET_BE,
        market=MarketType.H2H,
        outcome=Outcome(label="home"),
        odd_taken=2.40,  # within the 1.5-4.0 premium band
        fair_prob=0.5650,
        fair_odd=1.77,
        ev_pct=premium_ev,
        kelly_stake_pct=1.50,
        detected_at=datetime.now(timezone.utc),
    )
    sample_premium_surebet = Surebet(
        event_key="202607010000::testteamA__vs__testteamB",
        market=MarketType.H2H,
        line=None,
        legs={
            "home": (1.95, Book.UNIBET_BE),
            "draw": (3.85, Book.BETFIRST),
            "away": (4.20, Book.LADBROKES_BE),
        },
        margin=cfg.min_premium_surebet_pct / 100 + 0.005,
    )

    # Critical is the one channel with no odds band, so the sample deliberately
    # carries an absurd EV on a long shot — the shape of the alerts that get
    # filtered out everywhere else and are the whole reason to run this channel.
    sample_critical_bet = ValueBet(
        event_key="202607010000::testteamA__vs__testteamB",
        book=Book.BETANO_BE,
        market=MarketType.H2H,
        outcome=Outcome(label="away"),
        odd_taken=9.80,                    # far outside every other channel's band
        fair_prob=0.50,
        fair_odd=2.00,
        ev_pct=max(cfg.min_critical_ev_pct + 5.0, 40.0),
        kelly_stake_pct=1.50,
        detected_at=datetime.now(timezone.utc),
        league="Suisse - Super League",
    )

    bet_sent = send_alerts(
        [sample_bet], cfg, print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
        sport="soccer",
    )
    critical_sent = send_alerts(
        [sample_critical_bet], cfg,
        print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
        sport="soccer",
    )
    sb_prematch_sent = send_surebet_alerts(
        [sample_surebet_prematch], cfg,
        print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
        sport="soccer",
    )
    sb_live_sent = send_surebet_alerts(
        [sample_surebet_live], cfg,
        print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
        sport="soccer",
    )
    # CLV normal (7%) et CLV élevé (18%) — désormais le même canal, seul le
    # header change (🔥 au-dessus de min_high_clv_pct).
    clv_sent = send_clv_alerts(
        [(sample_clv_bet, 7.0, 1.74, 12)], cfg,
        print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
        sport="soccer",
    )
    clv_high_sent = send_clv_alerts(
        [(sample_clv_bet, 18.0, 1.58, 8)], cfg,
        print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
        sport="soccer",
    )
    # Premium : grosse value (envoyée aussi au canal principal) + surebet prématch.
    premium_bet_sent = send_alerts(
        [sample_premium_bet], cfg,
        print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
        sport="soccer",
    )
    premium_sb_sent = send_surebet_alerts(
        [sample_premium_surebet], cfg,
        print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
        sport="soccer",
    )

    def _status(sent: bool, chat: str, label: str, fallback_note: str = "") -> None:
        if sent:
            console.print(f"[bold]{label} → chat {chat} ✓{fallback_note}[/bold]")
        else:
            console.print(f"[red]{label} NOT sent — check the messages above.[/red]")

    _status(bet_sent, cfg.chat_id, "Value bet")

    sb_chat = cfg.effective_surebet_chat_id
    _status(
        sb_prematch_sent, sb_chat, "Surebet prématch",
        " (même chat — TELEGRAM_SUREBET_CHAT_ID non défini)" if sb_chat == cfg.chat_id else "",
    )

    live_chat = cfg.effective_live_surebet_chat_id
    _status(
        sb_live_sent, live_chat, "Surebet live",
        " (même chat — TELEGRAM_LIVE_SUREBET_CHAT_ID non défini)" if live_chat == sb_chat else "",
    )

    clv_chat = cfg.effective_clv_chat_id
    _status(
        clv_sent, clv_chat, "CLV normal (7%)",
        " (même chat — TELEGRAM_CLV_CHAT_ID non défini)" if clv_chat == cfg.chat_id else "",
    )
    _status(
        clv_high_sent, clv_chat, "CLV élevé (18%, header 🔥)",
        " (même canal CLV)",
    )

    crit_chat = cfg.effective_critical_chat_id
    if crit_chat:
        _status(critical_sent, crit_chat,
                f"Critique (EV {sample_critical_bet.ev_pct:.0f}% @ cote 9.80, hors bandes)")
    else:
        console.print(
            "[dim]Critique : TELEGRAM_CRITICAL_CHAT_ID non défini — les gros EV à "
            "cote élevée ne sont routés nulle part.[/dim]"
        )

    premium_chat = cfg.effective_premium_chat_id
    if premium_chat:
        _status(premium_bet_sent, premium_chat, "Premium value (grosse EV)")
        _status(premium_sb_sent, premium_chat, "Premium surebet prématch")
    else:
        console.print(
            "[dim]Premium: TELEGRAM_PREMIUM_CHAT_ID non défini — canal premium désactivé.[/dim]"
        )


@app.command(name="close-lines")
def close_lines(sport: str = "soccer"):
    """For every detected value bet whose event has kicked off, snapshot the
    last Pinnacle price as the closing line. The closing price comes from our
    own historical capture in the quotes table — Pinnacle removes prematch
    markets from the live API at kickoff, so by the time this command runs
    the only place the real closing line still exists is in the rows scan
    persisted before kickoff. Run after kickoff (e.g. cron a few minutes
    past every hour); the closing snapshot feeds `clv-report`."""
    cfg = ScanConfig(sport=sport)
    storage = Storage(cfg.db_path)
    teams.init(storage)
    open_bets = storage.open_value_bets()
    if not open_bets:
        console.print("[bold]No open value bets to close.[/bold]")
        return

    now = datetime.now(timezone.utc)
    due = [b for b in open_bets if event_started(b["event_key"], now=now)]
    console.print(f"[bold]{len(open_bets)} open bets, {len(due)} past kickoff.[/bold]")
    if not due:
        return

    closed = 0
    missing = 0
    for b in due:
        parsed = parse_event_key(b["event_key"])
        if parsed is None:
            missing += 1
            continue
        kickoff, _, _ = parsed
        row = storage.latest_pinnacle_quote_before(
            event_key=b["event_key"],
            market=b["market"],
            outcome_label=b["outcome_label"],
            line=b["line"],
            before=kickoff,
        )
        if row is None:
            missing += 1
            continue
        pinnacle_odd = float(row["decimal_odd"])
        storage.insert_clv_snapshot(
            value_bet_id=int(b["id"]),
            pinnacle_odd=pinnacle_odd,
            # Inverse of the closing odd is the quickest implied-prob estimate
            # — close to the devigged fair but not normalised; the fair_prob
            # column on the value_bet row is the proper sharp estimate.
            pinnacle_prob=1.0 / pinnacle_odd,
            snapshot_at=now,
            closing=True,
        )
        closed += 1
    console.print(
        f"  → {closed} closing snapshots written; {missing} bets with no "
        f"pre-kickoff Pinnacle quote on file"
    )


_EV_BUCKET_ORDER = ["<5%", "5-8%", "8-15%", "15-35%", "35%+"]


def _ev_bucket(ev: float) -> str:
    """Bucket a detected EV% into the bands that map to the alert channels, so
    clv-report can show whether higher detected EV actually means better CLV."""
    if ev < 5:
        return "<5%"
    if ev < 8:
        return "5-8%"
    if ev < 15:
        return "8-15%"
    if ev < 35:
        return "15-35%"
    return "35%+"


@app.command()
def prune(
    retention_days: int = typer.Option(
        2, "--days",
        help="Delete raw quote rows older than this many days. 2 is ample: "
        "closing lines are captured within hours of kickoff, and the table "
        "grows by tens of millions of rows a day, so a longer window costs "
        "gigabytes for data nothing reads.",
    ),
    vacuum: bool = typer.Option(True, "--vacuum/--no-vacuum",
                                help="Run VACUUM to actually shrink the file on disk."),
):
    """Trim the unbounded quotes history and reclaim disk space."""
    storage = Storage(ScanConfig().db_path)
    db_path = ScanConfig().db_path

    def _size_mb(p: str) -> float:
        try:
            return os.path.getsize(p) / (1024 * 1024)
        except OSError:
            return 0.0

    before = _size_mb(db_path)
    q = storage.prune_quotes(retention_days)
    n = storage.prune_notifications()
    console.print(f"Deleted {q} quote rows (> {retention_days}d) and {n} stale dedup rows.")
    if vacuum:
        # VACUUM rebuilds the database into a temporary copy alongside it, so
        # it needs free space of roughly the file's own size. On a DB that has
        # outgrown the disk — the exact situation that makes pruning urgent —
        # running it blind fills the disk and takes the daemon down with it.
        import shutil as _shutil

        free_mb = _shutil.disk_usage(os.path.dirname(os.path.abspath(db_path)) or ".").free / (1024 * 1024)
        need_mb = _size_mb(db_path) * 1.1
        if free_mb < need_mb:
            console.print(
                f"[yellow]VACUUM ignoré : il faudrait ~{need_mb/1024:.1f} Go libres, "
                f"il en reste {free_mb/1024:.1f} Go.[/yellow]\n"
                "[dim]  Les lignes sont bien supprimées et l'espace libéré sera réutilisé "
                "par les prochaines écritures — le fichier ne grossira plus. Relance avec "
                "--vacuum une fois assez d'espace disponible pour le réduire réellement.[/dim]"
            )
        else:
            console.print("VACUUM en cours (peut prendre un moment)…")
            storage.vacuum()
    after = _size_mb(db_path)
    console.print(f"DB : {before:.0f} Mo → {after:.0f} Mo (récupéré {before - after:.0f} Mo)")


@app.command(name="clv-report")
def clv_report():
    """Aggregate Closing Line Value over every closed value bet. CLV is the
    single most reliable indicator of long-run profitability — if your mean
    CLV is positive and stable, the engine is finding real edges."""
    cfg = ScanConfig()
    storage = Storage(cfg.db_path)
    teams.init(storage)
    rows = [dict(r) for r in storage.all_closed_bets()]
    if not rows:
        console.print("[bold]No closed bets yet — run `close-lines` after kickoffs.[/bold]")
        return

    pairs = [(r["odd_taken"], r["closing_odd"]) for r in rows]
    overall = clv_aggregate(pairs)
    console.print(
        f"[bold]Overall:[/bold] n={overall.n}  mean CLV {overall.mean_clv_pct:+.2f}%  "
        f"median {overall.median_clv_pct:+.2f}%  positive {overall.positive_rate * 100:.1f}%"
    )

    # Normalise the sport label (the LEFT JOIN yields None when the event row
    # was never persisted) and tag each row with its EV bucket.
    for r in rows:
        r["sport"] = r["sport"] or "unknown"
        r["ev_bucket"] = _ev_bucket(float(r["ev_pct"]))

    def _print_clv_table(title: str, dim: str, order: list[str] | None = None) -> None:
        groups = clv_group_by(rows, dim)
        stats = {k: clv_aggregate(v) for k, v in groups.items() if v}
        if not stats:
            return
        t = Table(title=title, show_lines=False)
        t.add_column(dim)
        t.add_column("n", justify="right")
        t.add_column("mean CLV%", justify="right")
        t.add_column("median%", justify="right")
        t.add_column("positive%", justify="right")
        # EV buckets read best in natural order; the rest by mean CLV descending.
        keys = ([k for k in order if k in stats] if order
                else sorted(stats, key=lambda k: stats[k].mean_clv_pct, reverse=True))
        for k in keys:
            s = stats[k]
            t.add_row(
                str(k), str(s.n),
                f"{s.mean_clv_pct:+.2f}", f"{s.median_clv_pct:+.2f}",
                f"{s.positive_rate * 100:.1f}",
            )
        console.print(t)

    _print_clv_table("CLV by book", "book")
    _print_clv_table("CLV by market", "market")
    _print_clv_table("CLV by sport", "sport")
    _print_clv_table("CLV by EV bucket", "ev_bucket", order=_EV_BUCKET_ORDER)


@app.command(name="export-history")
def export_history(
    out: str = typer.Option("history.csv", "--out", help="Chemin du fichier CSV de sortie."),
    bankroll: float = typer.Option(1000.0, "--bankroll", help="Capital de départ pour la simulation."),
):
    """Exporter chaque value bet clôturé (avec ligne de clôture + CLV) en CSV,
    prêt pour Excel : date, match, book, cotes, EV%, clôture, CLV%, mise, et un
    capital simulé sur la CLV. Colonnes Résultat/P&L laissées vides (à remplir
    à la main ou via un futur flux de résultats)."""
    import csv
    storage = Storage(ScanConfig().db_path)
    teams.init(storage)
    rows = [dict(r) for r in storage.all_closed_bets()]
    if not rows:
        console.print("[bold]Aucun pari clôturé — lance `close-lines` après des coups d'envoi.[/bold]")
        return

    # Oldest first so the simulated capital curve reads chronologically.
    rows.sort(key=lambda r: r.get("detected_at") or "")

    def _match(ek: str) -> str:
        parsed = parse_event_key(ek)
        if parsed is None:
            return ek
        _, home, away = parsed
        return f"{home.replace('_', ' ').title()} vs {away.replace('_', ' ').title()}"

    headers = [
        "Date", "Sport", "Match", "Book", "Marché", "Pari", "Cote prise",
        "Cote fair", "EV %", "Cote clôture (Pinnacle)", "CLV %", "Mise % (Kelly)",
        "Capital simulé (CLV)", "Résultat", "P&L réel",
    ]
    capital = bankroll
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            taken = float(r["odd_taken"])
            closing = float(r["closing_odd"]) if r.get("closing_odd") else 0.0
            clv = clv_pct(taken, closing) * 100 if closing else 0.0
            kelly = float(r.get("kelly_pct") or 0.0)
            # Capital simulé : on mise kelly% du capital, gain espéré ≈ CLV.
            stake = capital * kelly / 100.0
            capital += stake * (clv / 100.0)
            line = r.get("line")
            pari = f"{r['outcome_label']}{(' ' + str(line)) if line is not None else ''}"
            w.writerow([
                (r.get("detected_at") or "")[:19],
                r.get("sport") or "",
                _match(r["event_key"]),
                r["book"],
                r["market"],
                pari,
                f"{taken:.2f}",
                f"{float(r['fair_odd']):.2f}",
                f"{float(r['ev_pct']):.2f}",
                f"{closing:.2f}" if closing else "",
                f"{clv:+.2f}" if closing else "",
                f"{kelly:.2f}",
                f"{capital:.2f}",
                "",   # Résultat (à remplir)
                "",   # P&L réel (à remplir)
            ])
    console.print(
        f"[green]✓[/green] {len(rows)} paris exportés vers [bold]{out}[/bold]  "
        f"(capital simulé CLV : {bankroll:.0f}€ → {capital:.0f}€)"
    )



@app.command(name="inspect-betano")
def inspect_betano(path: str):
    """Inspect a saved Betano overview JSON dump (DevTools → Response → save).

    Prints the shape of one event / league / market / selection so the parser
    can be refined for the exact field names that endpoint uses.
    """
    import json as _json
    from pathlib import Path as _Path

    data = _json.loads(_Path(path).read_text())

    def _peek(label: str, container: dict | None, n: int = 1) -> None:
        console.print(f"\n[bold cyan]{label}[/bold cyan] (count={len(container or {})})")
        if not container:
            return
        for i, (k, v) in enumerate(container.items()):
            console.print(f"  key={k!r}  fields={sorted((v or {}).keys()) if isinstance(v, dict) else type(v).__name__}")
            if isinstance(v, dict):
                console.print(f"  sample={_json.dumps(v, indent=2, ensure_ascii=False)[:600]}")
            if i + 1 >= n:
                break

    console.print(f"[bold]Top-level keys[/bold]: {sorted(data.keys())}")
    if "contentVersion" in data:
        console.print(f"contentVersion: {data['contentVersion']}")
    if "sports" in data and isinstance(data["sports"], dict):
        ids = data["sports"].get("allIds")
        if ids:
            console.print(f"sport ids: {ids}")

    _peek("events", data.get("events"))
    _peek("leagues", data.get("leagues"))
    _peek("markets", data.get("markets"), n=2)
    _peek("selections", data.get("selections"), n=2)
    _peek("zones", data.get("zones"))

    quotes = list(betano_parse_overview(data))
    console.print(f"\n[bold]Parser produced {len(quotes)} OddQuote(s).[/bold]")
    for q in quotes[:5]:
        console.print(f"  {q.event_key} | {q.market.value} | {q.outcome.label} @ {q.decimal_odd}")


@app.command(name="betano-coverage")
def betano_coverage(
    path: str = typer.Argument("data/betano.json", help="Betano overview dump to inspect."),
):
    """Summarise what a Betano dump actually covers: sports, and how far ahead.

    The endpoint is named /live/overview, which reads as "in-play only" — but
    the site's homepage lists upcoming fixtures from the same call, so the name
    is about the feed being real-time, not about the events being in progress.
    This prints the breakdown that settles it, instead of inferring coverage
    from the path name."""
    import json as _json
    from pathlib import Path as _Path
    from .scrapers.betano import _FIELDS_EVENT_START, _first, _parse_datetime

    try:
        data = _json.loads(_Path(path).read_text())
    except (OSError, ValueError) as e:
        console.print(f"[red]Unreadable dump {path}: {e}[/red]")
        raise typer.Exit(1)

    events = data.get("events") or {}
    leagues = data.get("leagues") or {}
    if not events:
        console.print(f"[yellow]No events in {path}.[/yellow]")
        return

    # Events carry a sportId; the readable name lives in the top-level `sports`
    # store. Leagues only have {id, name, eventIdList, displayOrder}, so they
    # can't be used for this.
    sports_raw = data.get("sports") or {}
    sport_names: dict[str, str] = {}
    for container in (sports_raw.get("byId"), sports_raw):
        if isinstance(container, dict):
            for sid, s in container.items():
                if isinstance(s, dict) and s.get("name"):
                    sport_names[str(sid)] = str(s["name"])
    league_names = {
        str(lid): str(lg.get("name") or lid)
        for lid, lg in leagues.items()
        if isinstance(lg, dict)
    }

    now = datetime.now(timezone.utc)
    by_sport: dict[str, int] = defaultdict(int)
    live = upcoming = undated = 0
    horizons: dict[str, int] = defaultdict(int)
    latest: datetime | None = None
    top_leagues: dict[str, int] = defaultdict(int)

    for ev in events.values():
        if not isinstance(ev, dict):
            continue
        sid = str(ev.get("sportId") or ev.get("ardSportId") or "")
        by_sport[sport_names.get(sid, f"sportId={sid}" if sid else "?")] += 1
        top_leagues[league_names.get(str(ev.get("leagueId") or ""), "?")] += 1

        start = _parse_datetime(_first(ev, _FIELDS_EVENT_START))
        # Trust the feed's own isLive flag over comparing timestamps: an event
        # can be past its start time and not yet in play (delays), and this is
        # the field the site itself renders from.
        is_live = ev.get("isLive")
        if start is None:
            undated += 1
            continue
        if is_live is True or (is_live is None and start <= now):
            live += 1
        else:
            upcoming += 1
            if latest is None or start > latest:
                latest = start
            days = (start - now).days
            horizons["J+%d" % days if days else "aujourd'hui"] += 1

    console.print(f"[bold]{len(events)} events in {path}[/bold]")
    console.print(f"  déjà commencés (live) : {live}")
    console.print(f"  à venir (prématch)    : {upcoming}")
    if undated:
        console.print(f"  sans date             : {undated}")
    if latest is not None:
        console.print(f"  horizon le plus loin  : {latest:%Y-%m-%d %H:%M} UTC")

    st = Table(title="Par sport", show_lines=False)
    st.add_column("sport")
    st.add_column("events", justify="right")
    for name, n in sorted(by_sport.items(), key=lambda kv: -kv[1]):
        st.add_row(str(name), str(n))
    console.print(st)

    if horizons:
        ht = Table(title="Prématch par échéance", show_lines=False)
        ht.add_column("quand")
        ht.add_column("events", justify="right")
        for k, n in sorted(horizons.items(), key=lambda kv: -kv[1]):
            ht.add_row(k, str(n))
        console.print(ht)

    lt = Table(title="Top 10 compétitions", show_lines=False)
    lt.add_column("compétition")
    lt.add_column("events", justify="right")
    for name, n in sorted(top_leagues.items(), key=lambda kv: -kv[1])[:10]:
        lt.add_row(name, str(n))
    console.print(lt)

    quotes = list(betano_parse_overview(data))
    console.print(f"\n[bold]Parser produced {len(quotes)} OddQuote(s).[/bold]")
    by_market: dict[str, int] = defaultdict(int)
    for q in quotes:
        by_market[q.market.value] += 1
    for m, n in sorted(by_market.items(), key=lambda kv: -kv[1]):
        console.print(f"  {m}: {n}")


@app.command(name="inspect-json")
def inspect_json(
    path: str = typer.Argument(..., help="JSON file to summarise."),
    depth: int = typer.Option(4, "--depth", help="How deep to walk."),
    samples: int = typer.Option(2, "--samples", help="Items to show per list/dict."),
    at: str = typer.Option(
        "", "--path",
        help="Dotted path to focus on, e.g. 'data.blocks.0.events.0'. "
        "Without it, only the first --samples keys of each dict are shown, "
        "which hides the interesting branch in wide payloads.",
    ),
):
    """Print the shape of an arbitrary JSON payload — keys, types, sample values.

    Betano's prematch offer lives on a different API (/fr/api/...) whose shape
    is unrelated to the danae-webapi overview parse_overview() handles. This
    prints enough structure to write a parser against without dumping megabytes
    of odds."""
    import json as _json
    from pathlib import Path as _Path

    try:
        data = _json.loads(_Path(path).read_text())
    except (OSError, ValueError) as e:
        console.print(f"[red]Unreadable {path}: {e}[/red]")
        raise typer.Exit(1)

    # Keys are printed inside brackets, which Rich would swallow as markup —
    # escape everything that comes from the payload.
    from rich.markup import escape as _esc

    def walk(node: object, prefix: str, level: int) -> None:
        if level > depth:
            return
        pad = "  " * level
        p = _esc(prefix)
        if isinstance(node, dict):
            keys = _esc(str(sorted(map(str, node))[:25]))
            console.print(f"{pad}{p} [cyan]dict[/cyan]({len(node)}) keys={keys}")
            for k in list(node)[:samples]:
                walk(node[k], f"[{k}]", level + 1)
        elif isinstance(node, list):
            console.print(f"{pad}{p} [magenta]list[/magenta]({len(node)})")
            for item in node[:samples]:
                walk(item, "[]", level + 1)
        else:
            console.print(f"{pad}{p} {type(node).__name__} = {_esc(repr(node)[:120])}")

    node: object = data
    if at:
        for part in at.split("."):
            if isinstance(node, list):
                try:
                    node = node[int(part)]
                except (ValueError, IndexError):
                    console.print(f"[red]Path stops at '{part}': list has {len(node)} items.[/red]")
                    raise typer.Exit(1)
            elif isinstance(node, dict):
                if part not in node:
                    console.print(
                        f"[red]Path stops at '{part}'.[/red] Available: {sorted(map(str, node))[:30]}"
                    )
                    raise typer.Exit(1)
                node = node[part]
            else:
                console.print(f"[red]Path stops at '{part}': reached a {type(node).__name__}.[/red]")
                raise typer.Exit(1)

    console.print(f"[bold]{path}[/bold]" + (f"  →  {at}" if at else ""))
    walk(node, at or "root", 0)


@app.command(name="betano-prematch-shape")
def betano_prematch_shape(
    path: str = typer.Argument(..., help="A data/samples/*-prematch.json capture."),
):
    """Summarise a prematch capture: sizes, market type codes, selection shape.

    The prematch API (/fr/api/sport/{slug}/matchs-a-venir) uses different market
    type codes from the danae-webapi live feed — the first sample shows 'HTHP',
    which _MARKET_BY_TYPE doesn't know. Rather than discover them one round-trip
    at a time, count every code present with an example name, and show one
    fully-expanded selection."""
    import json as _json
    from pathlib import Path as _Path

    try:
        data = _json.loads(_Path(path).read_text())
    except (OSError, ValueError) as e:
        console.print(f"[red]Unreadable {path}: {e}[/red]")
        raise typer.Exit(1)

    blocks = ((data.get("data") or {}).get("blocks")) or []
    n_events = 0
    market_types: dict[str, int] = defaultdict(int)
    type_example: dict[str, str] = {}
    sel_keys: set[str] = set()
    first_selection: dict | None = None
    first_market_of_type: dict[str, dict] = {}
    sports: dict[str, int] = defaultdict(int)
    horizon_min: str | None = None
    horizon_max: str | None = None

    for b in blocks:
        for ev in (b.get("events") or []):
            n_events += 1
            sports[str(ev.get("sportId") or "?")] += 1
            st = str(ev.get("startTime") or "")
            if st:
                horizon_min = st if horizon_min is None or st < horizon_min else horizon_min
                horizon_max = st if horizon_max is None or st > horizon_max else horizon_max
            for m in (ev.get("markets") or []):
                code = str(m.get("type") or "?")
                market_types[code] += 1
                type_example.setdefault(code, str(m.get("name") or ""))
                first_market_of_type.setdefault(code, m)
                for s in (m.get("selections") or []):
                    sel_keys.update(s.keys())
                    if first_selection is None:
                        first_selection = s

    console.print(f"[bold]{path}[/bold]")
    console.print(f"  compétitions (blocks) : {len(blocks)}")
    console.print(f"  événements            : {n_events}")
    console.print(f"  sports                : {dict(sports)}")
    if horizon_min:
        console.print(f"  fenêtre               : {horizon_min}  →  {horizon_max}")

    mt = Table(title="Codes marché", show_lines=False)
    mt.add_column("type")
    mt.add_column("n", justify="right")
    mt.add_column("exemple de nom", overflow="fold")
    mt.add_column("handicap")
    for code, n in sorted(market_types.items(), key=lambda kv: -kv[1]):
        hc = first_market_of_type.get(code, {}).get("handicap")
        mt.add_row(code, str(n), type_example.get(code, ""), "" if hc is None else str(hc))
    console.print(mt)

    console.print(f"\n[bold]Champs de selection[/bold]: {sorted(sel_keys)}")
    if first_selection is not None:
        console.print(_json.dumps(first_selection, indent=2, ensure_ascii=False)[:400])

    # What the parser actually gets out of it. The market code set differs per
    # sport and only tennis was fully observed, so an unmapped code here means
    # quotes are being dropped for this sport.
    unknown: set[str] = set()
    quotes = list(betano_parse_prematch(data, unknown_types=unknown))
    by_market: dict[str, int] = defaultdict(int)
    for q in quotes:
        by_market[q.market.value] += 1
    console.print(f"\n[bold]Parser: {len(quotes)} OddQuote(s)[/bold]")
    for m, n in sorted(by_market.items(), key=lambda kv: -kv[1]):
        console.print(f"  {m}: {n}")
    if unknown:
        console.print(
            f"[yellow]Codes non mappés (cotes perdues): {sorted(unknown)}[/yellow]\n"
            f"[dim]  → ajouter à _PREMATCH_MARKET_BY_TYPE dans src/scrapers/betano.py[/dim]"
        )
    else:
        console.print("[green]  tous les codes marché sont mappés[/green]")


@app.command(name="books-coverage")
def books_coverage(
    sport: str = typer.Option("soccer", "--sport", help="Comma-separated sports."),
    betano_file: str = typer.Option(
        "data/betano.json", "--betano-file", help="Betano live dump.",
    ),
):
    """Compare what every book actually returns, side by side.

    Answers "is this book's coverage low?" with numbers rather than intuition:
    distinct events, quotes, markets offered, and how far ahead each book
    prices. A book with far fewer events than its peers is either genuinely
    thin or being parsed incompletely — this is what distinguishes the two."""
    for current_sport in [s.strip() for s in sport.split(",") if s.strip()]:
        console.print(f"\n[bold green]══ {current_sport.upper()} ══[/bold green]")
        all_q = _fetch_all_parallel(current_sport, betano_file, include_file_books=True)
        if not all_q:
            console.print("[yellow]Aucune cote récupérée.[/yellow]")
            continue

        now = datetime.now(timezone.utc)
        per_book: dict[Book, list[OddQuote]] = defaultdict(list)
        for q in all_q:
            per_book[q.book].append(q)

        ref_events = {q.event_key for q in per_book.get(Book.PINNACLE, [])}

        table = Table(title=f"Couverture par book ({current_sport})", show_lines=False)
        table.add_column("book")
        table.add_column("events", justify="right")
        table.add_column("cotes", justify="right")
        table.add_column("marchés", overflow="fold")
        table.add_column("à venir", justify="right")
        table.add_column("horizon", justify="right")
        table.add_column("∩ Pinnacle", justify="right")

        rows = []
        for book, quotes in per_book.items():
            events = {q.event_key for q in quotes}
            markets = sorted({q.market.value for q in quotes})
            upcoming = 0
            latest: datetime | None = None
            for ek in events:
                parsed = parse_event_key(ek)
                if parsed is None:
                    continue
                start = parsed[0]
                if start > now:
                    upcoming += 1
                    if latest is None or start > latest:
                        latest = start
            # Overlap with the sharp reference is what actually matters: a book
            # can list thousands of events and still be useless if none of them
            # are ones Pinnacle prices, since there'd be no fair line.
            #
            # Measured AFTER fuzzy matching, not by raw key equality. Keys embed
            # the exact kickoff minute and normalised names, so raw equality
            # counts only the events two books happen to spell and schedule
            # identically — it understates badly, and reading it as the ceiling
            # on a book's usable events is simply wrong.
            if ref_events and book != Book.PINNACLE:
                overlap = len({
                    q.event_key for q in remap_to_reference(quotes, ref_events)
                })
            else:
                overlap = None
            horizon = f"J+{(latest - now).days}" if latest else "-"
            rows.append((
                book.value, len(events), len(quotes), ",".join(markets),
                upcoming, horizon,
                "-" if overlap is None else str(overlap),
            ))

        for r in sorted(rows, key=lambda r: -r[1]):
            table.add_row(r[0], str(r[1]), str(r[2]), r[3], str(r[4]), r[5], r[6])
        console.print(table)
        console.print(
            "[dim]« à venir » = événements pas encore commencés ; "
            "« ∩ Pinnacle » = événements partagés avec la référence sharp "
            "(avant matching flou, donc minoré).[/dim]"
        )


@app.command(name="betano-value-test")
def betano_value_test(
    sport: str = typer.Option("soccer", "--sport", help="Comma-separated sports."),
    min_ev: float = typer.Option(
        1.0, "--min-ev",
        help="Deliberately lower than the daemon's 5.0: this is a diagnostic, "
        "and seeing small edges proves the chain works even on a quiet day.",
    ),
    betano_file: str = typer.Option("data/betano.json", "--betano-file"),
):
    """Dry-run the full pipeline and show only Betano's value bets.

    Sends nothing to Telegram and writes nothing to the database — it answers
    "is Betano actually producing value?" without waiting for the daemon to
    alert. Also reports how many Betano quotes survive each stage, since a zero
    at the end is usually a matching problem rather than an absence of edge."""
    for current_sport in [s.strip() for s in sport.split(",") if s.strip()]:
        console.print(f"\n[bold green]══ {current_sport.upper()} ══[/bold green]")
        cfg = ScanConfig(sport=current_sport, min_ev_pct=min_ev)
        all_q = _fetch_all_parallel(current_sport, betano_file, include_file_books=True)

        pinnacle_q = [q for q in all_q if q.book == Book.PINNACLE]
        betano_raw = [q for q in all_q if q.book == Book.BETANO_BE]
        if not pinnacle_q:
            console.print("[yellow]Pas de cotes Pinnacle — aucune ligne de référence.[/yellow]")
            continue
        if not betano_raw:
            console.print("[yellow]Pas de cotes Betano — onglet fermé ou fichier périmé ?[/yellow]")
            continue

        fair = build_fair_lines(pinnacle_q, cfg.devig_method)
        ref_keys = {fl.event_key for fl in fair.values()}
        betano_matched = remap_to_reference(betano_raw, ref_keys)
        bets = [
            b for b in find_value_bets(betano_matched, fair, cfg)
            if b.book == Book.BETANO_BE
        ]
        bets.sort(key=lambda b: b.ev_pct, reverse=True)

        # The funnel is the diagnostic: each stage that drops everything points
        # at a different cause (stale push, failed matching, no edge).
        console.print(
            f"  {len(betano_raw)} cotes Betano  →  {len(betano_matched)} appariées à un "
            f"événement Pinnacle  →  [bold]{len(bets)} value bets ≥ {min_ev}%[/bold]"
        )
        if betano_raw and not betano_matched:
            console.print(
                "[yellow]  Aucune cote appariée : les événements Betano ne correspondent "
                "à aucun événement Pinnacle (noms d'équipes ou horaires trop éloignés).[/yellow]"
            )
        if not bets:
            continue

        t = Table(title=f"Value bets Betano ({current_sport})", show_lines=False)
        t.add_column("event", overflow="fold")
        t.add_column("marché")
        t.add_column("issue")
        t.add_column("cote", justify="right")
        t.add_column("juste", justify="right")
        t.add_column("EV%", justify="right")
        for b in bets[:25]:
            line = f" {b.outcome.line}" if b.outcome.line is not None else ""
            t.add_row(
                b.event_key, b.market.value, f"{b.outcome.label}{line}",
                f"{b.odd_taken:.2f}", f"{b.fair_odd:.2f}", f"{b.ev_pct:.2f}",
            )
        console.print(t)


@app.command()
def doctor(hours: int = typer.Option(24, "--hours", help="Lookback window.")):
    """One-shot health check: services, feeds, config, and what each book produced.

    Reads only local state (systemd, files, SQLite) — no scraping — so it's
    safe to run any time and answers the question that actually comes up:
    "everything looks running, so why no alerts from book X?" """
    import sqlite3 as _sq
    import subprocess as _sp
    from pathlib import Path as _P

    project = _P(__file__).resolve().parent.parent
    now = datetime.now(timezone.utc)
    problems: list[str] = []

    # The daemon gets its config from .env via scan-daemon.sh, but a hand-run
    # command doesn't — so without this the check reports "Telegram not
    # configured" on a perfectly working setup. A diagnostic that invents
    # failures is worse than none, so read the same file the daemon does.
    # Existing environment wins, so an explicit override still works.
    env_file = project / ".env"
    if env_file.exists():
        for raw in env_file.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if v[:1] == v[-1:] and v[:1] in ("'", '"') and len(v) >= 2:
                v = v[1:-1]
            os.environ.setdefault(k, v)

    # ── services ─────────────────────────────────────────────────────────
    st = Table(title="Services", show_lines=False)
    st.add_column("unit")
    st.add_column("état")
    for unit in ("betano-ingest", "valuebet-daemon"):
        try:
            out = _sp.run(["systemctl", "is-active", unit],
                          capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception as e:
            out = f"inconnu ({e})"
        # systemctl prints nothing when the unit doesn't exist at all, which
        # would otherwise render as a blank cell rather than a problem.
        out = out or "introuvable"
        good = out == "active"
        st.add_row(unit, ("[green]" if good else "[red]") + out + ("[/green]" if good else "[/red]"))
        if not good:
            problems.append(f"{unit} n'est pas actif → sudo systemctl restart {unit}")
    console.print(st)

    # ── pushed feeds ─────────────────────────────────────────────────────
    ft = Table(title="Flux poussés par le navigateur", show_lines=False)
    ft.add_column("fichier")
    ft.add_column("taille", justify="right")
    ft.add_column("âge", justify="right")
    ft.add_column("verdict")

    live_max = float(os.getenv("BETANO_LIVE_MAX_AGE_MIN", "5"))
    pm_max = float(os.getenv("BETANO_PREMATCH_MAX_AGE_MIN", "30"))
    feeds = [(project / "data" / "betano.json", live_max, "live")]
    # Only feeds for sports actually scanned. A leftover file from a sport
    # since removed is expected to be stale — flagging it as a problem trains
    # the eye to skip this section, which is exactly when a real staleness
    # would be missed.
    scanned = {
        s.strip() for s in
        os.getenv("SPORT_LIST", "soccer,tennis,hockey").split(",") if s.strip()
    }
    pm_dir = project / "data" / "prematch"
    if pm_dir.is_dir():
        for f in sorted(pm_dir.glob("*.json")):
            if f.stem in scanned:
                feeds.append((f, pm_max, f"prématch {f.stem}"))
            else:
                console.print(
                    f"[dim]  (data/prematch/{f.name} ignoré — {f.stem} n'est pas "
                    f"dans SPORT_LIST ; supprimable)[/dim]"
                )

    for path, max_age, label in feeds:
        if not path.exists():
            ft.add_row(label, "-", "-", "[red]absent[/red]")
            problems.append(f"{label} absent — l'onglet Betano a-t-il déjà tourné ?")
            continue
        age_min = (now.timestamp() - path.stat().st_mtime) / 60
        stale = age_min > max_age
        ft.add_row(
            label, f"{path.stat().st_size / 1024:.0f} Ko", f"{age_min:.0f} min",
            "[red]PÉRIMÉ[/red]" if stale else "[green]frais[/green]",
        )
        if stale:
            problems.append(
                f"{label} périmé ({age_min:.0f} min > {max_age:.0f}) — onglet Betano fermé ?"
            )
    console.print(ft)

    # ── telegram ─────────────────────────────────────────────────────────
    tg = TelegramConfig.from_env()
    if tg is None:
        console.print("[red]Telegram non configuré[/red] — aucune alerte ne partira.")
        problems.append("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID manquants")
    else:
        ct = Table(title="Canaux Telegram", show_lines=False)
        ct.add_column("canal")
        ct.add_column("configuré")
        ct.add_column("condition")
        ct.add_row("principal", "oui" if tg.chat_id else "[red]non[/red]",
                   f"EV {tg.min_ev_pct}–{tg.main_max_ev_pct}%, cotes "
                   f"{tg.main_min_odd}–{tg.main_max_odd}")
        ct.add_row("premium", "oui" if tg.effective_premium_chat_id else "[yellow]non[/yellow]",
                   f"EV ≥ {tg.min_premium_ev_pct}% cotes {tg.premium_min_odd}–{tg.premium_max_odd}"
                   f"  ou  EV ≥ {tg.premium_hi_min_ev}% cotes "
                   f"{tg.premium_hi_min_odd}–{tg.premium_hi_max_odd}")
        ct.add_row("critique", "oui" if tg.effective_critical_chat_id else "[yellow]non[/yellow]",
                   f"EV ≥ {tg.min_critical_ev_pct}% — [bold]aucune limite de cote[/bold], "
                   f"prématch uniquement")
        console.print(ct)
        console.print(
            f"[dim]  dédoublonnage : max {tg.valuebet_max_alerts} alertes par pari ; "
            f"alertes prématch supprimées à moins de {tg.min_minutes_to_kickoff} min du "
            f"coup d'envoi[/dim]"
        )

    # ── what each book actually produced ─────────────────────────────────
    db = project / ScanConfig().db_path
    if not db.exists():
        console.print(f"[yellow]Base absente ({db}).[/yellow]")
    else:
        since = (now - timedelta(hours=hours)).isoformat()
        # A short timeout rather than the default: the daemon writes constantly,
        # and a diagnostic that blocks on its lock is useless.
        con = _sq.connect(str(db), timeout=5.0)
        con.row_factory = _sq.Row

        def _rows(sql: str, args: tuple = ()) -> list:
            """Missing tables and lock contention must degrade to an empty
            section, not kill the whole report — the services and feed checks
            above are often the ones you actually came for."""
            try:
                return list(con.execute(sql, args))
            except _sq.Error as e:
                console.print(f"[yellow]  (lecture base impossible : {e})[/yellow]")
                return []

        try:
            size_mb = db.stat().st_size / 1048576
            console.print(f"Base : [bold]{size_mb:,.0f} Mo[/bold] ({db})")
            if size_mb > 2000:
                problems.append(
                    f"Base à {size_mb/1024:.1f} Go — lance `python -m src.main prune` "
                    f"(et vérifie que valuebet-prune.timer est actif)."
                )

            bt = Table(title=f"Par book sur {hours} h", show_lines=False)
            bt.add_column("book")
            bt.add_column("cotes (échantillon récent)", justify="right")
            bt.add_column("value bets", justify="right")
            bt.add_column("alertes envoyées", justify="right")
            bt.add_column("meilleur EV%", justify="right")

            # Bounded on purpose. quotes grows by tens of millions of rows a
            # day (every book's every price, every cycle), so an unbounded
            # aggregate over 24h scans far too much and the whole check hangs —
            # which is exactly what it did. Reading the most recent slice via
            # the fetched_at index answers "is this book producing?" just as
            # well, in constant time.
            quotes = {r["book"]: r["n"] for r in _rows(
                "SELECT book, COUNT(*) n FROM ("
                "  SELECT book FROM quotes WHERE fetched_at >= ?"
                "  ORDER BY fetched_at DESC LIMIT 200000"
                ") GROUP BY book", (since,))}
            vbs = {r["book"]: (r["n"], r["mx"]) for r in _rows(
                "SELECT book, COUNT(*) n, MAX(ev_pct) mx FROM value_bets "
                "WHERE detected_at >= ? GROUP BY book", (since,))}
            notified = {r["book"]: r["n"] for r in _rows(
                "SELECT book, COUNT(*) n FROM notified_value_bets "
                "WHERE notified_at >= ? GROUP BY book", (since,))}

            for book in sorted(set(quotes) | set(vbs) | set(notified)):
                n_vb, best = vbs.get(book, (0, None))
                bt.add_row(
                    book, str(quotes.get(book, 0)), str(n_vb),
                    str(notified.get(book, 0)),
                    f"{best:.1f}" if best is not None else "-",
                )
            console.print(bt)

            # The common confusion: quotes stored but no alerts. Each stage
            # below is a different cause, so name the one that actually applies.
            b = Book.BETANO_BE.value
            if quotes.get(b) and not vbs.get(b):
                problems.append(
                    "Betano fournit des cotes mais aucun value bet détecté — "
                    "soit ses événements ne matchent pas Pinnacle, soit ses prix "
                    "ne dépassent pas le seuil. Lance : betano-value-test --min-ev 0.5"
                )
            elif vbs.get(b) and not notified.get(b):
                n_vb, best = vbs[b]
                thr = tg.min_ev_pct if tg else 0
                problems.append(
                    f"Betano a {n_vb} value bets (meilleur {best:.1f}%) mais 0 alerte : "
                    + (f"aucun n'atteint le seuil Telegram de {thr}%."
                       if best is not None and best < thr
                       else "probablement le dédoublonnage (déjà alerté).")
                )
            elif not quotes.get(b):
                problems.append("Aucune cote Betano stockée sur la période.")

            # High-EV bets that never routed anywhere. Extreme EV only clears a
            # channel via `critique`, and that one is prematch-only — a bet
            # detected after kickoff is dropped even when the channel exists,
            # which is invisible from the summary table alone.
            if tg is not None:
                extreme = _rows(
                    "SELECT event_key, book, odd_taken, ev_pct FROM value_bets "
                    "WHERE detected_at >= ? AND ev_pct >= ? ORDER BY ev_pct DESC LIMIT 8",
                    (since, tg.min_critical_ev_pct),
                )
                if extreme:
                    et = Table(
                        title=f"EV ≥ {tg.min_critical_ev_pct}% (voie critique)",
                        show_lines=False,
                    )
                    et.add_column("book")
                    et.add_column("cote", justify="right")
                    et.add_column("EV%", justify="right")
                    et.add_column("routage")
                    for r in extreme:
                        parsed = parse_event_key(r["event_key"])
                        live = parsed is not None and parsed[0] <= now
                        if not tg.effective_critical_chat_id:
                            verdict = "[yellow]canal critique non configuré[/yellow]"
                        elif live:
                            verdict = "[yellow]live — critique est prématch only[/yellow]"
                        else:
                            verdict = "[green]→ critique[/green]"
                        et.add_row(r["book"], f"{r['odd_taken']:.2f}",
                                   f"{r['ev_pct']:.0f}", verdict)
                    console.print(et)
        finally:
            con.close()

    console.print()
    if problems:
        console.print("[bold red]À regarder :[/bold red]")
        for p in problems:
            console.print(f"  • {p}")
    else:
        console.print("[bold green]Tout est en ordre.[/bold green]")


@app.command(name="smarkets-impact")
def smarkets_impact(sport: str = typer.Option("soccer", "--sport")):
    """Measure what the fallback sharp source actually adds.

    The only question worth asking about it: how many markets does it price
    that Pinnacle doesn't, and do those turn into value bets that would
    otherwise be impossible? A source that merely duplicates Pinnacle costs a
    scrape and changes nothing."""
    if os.getenv("USE_SMARKETS", "").strip() in ("", "0", "false"):
        console.print("[yellow]USE_SMARKETS n'est pas activé — rien à mesurer.[/yellow]")
        raise typer.Exit(1)

    for current_sport in [s.strip() for s in sport.split(",") if s.strip()]:
        console.print(f"\n[bold green]══ {current_sport.upper()} ══[/bold green]")
        cfg = ScanConfig(sport=current_sport)
        all_q = _fetch_all_parallel(current_sport, "data/betano.json", include_file_books=True)
        pinnacle_q = [q for q in all_q if q.book == Book.PINNACLE]
        secondary_raw = [q for q in all_q if q.book == Book.SMARKETS]
        soft_raw = [q for q in all_q if q.book not in SHARP_BOOKS]

        if not secondary_raw:
            console.print("[yellow]Smarkets n'a renvoyé aucune cote.[/yellow]")
            continue

        aligned = align_secondary_sharp(pinnacle_q, secondary_raw)
        pin_events = {q.event_key for q in pinnacle_q}
        sec_events = {q.event_key for q in aligned}

        console.print(f"  Pinnacle : {len(pin_events)} événements")
        console.print(f"  Smarkets : {len(sec_events)} événements "
                      f"({len(sec_events & pin_events)} en commun)")
        console.print(f"  [bold]exclusifs à Smarkets : {len(sec_events - pin_events)}[/bold]")

        # With and without, so the delta is the fallback's actual contribution
        # rather than a number that merely looks large.
        fair_pin = build_fair_lines(pinnacle_q, cfg.devig_method)
        fair_both = build_fair_lines(pinnacle_q, cfg.devig_method, secondary_quotes=aligned)
        soft_pin = remap_to_reference(soft_raw, {f.event_key for f in fair_pin.values()})
        soft_both = remap_to_reference(soft_raw, {f.event_key for f in fair_both.values()})
        bets_pin = find_value_bets(soft_pin, fair_pin, cfg)
        bets_both = find_value_bets(soft_both, fair_both, cfg)

        console.print(f"\n  lignes justes : {len(fair_pin)} → [bold]{len(fair_both)}[/bold]")
        console.print(f"  value bets    : {len(bets_pin)} → [bold]{len(bets_both)}[/bold] "
                      f"(+{len(bets_both) - len(bets_pin)})")

        added = [b for b in bets_both if b.reference_book is Book.SMARKETS]
        if not added:
            console.print("[yellow]  Aucun value bet ne repose sur Smarkets — "
                          "il ne fait que doubler Pinnacle ici.[/yellow]")
            continue
        t = Table(title="Value bets rendus possibles par Smarkets", show_lines=False)
        t.add_column("event", overflow="fold")
        t.add_column("book")
        t.add_column("cote", justify="right")
        t.add_column("EV%", justify="right")
        for b in sorted(added, key=lambda x: -x.ev_pct)[:15]:
            t.add_row(b.event_key, b.book.value, f"{b.odd_taken:.2f}", f"{b.ev_pct:.2f}")
        console.print(t)


@app.command()
def selftest():
    """Sanity check on math primitives."""
    from .devig import devig as _devig
    probs = _devig([2.10, 3.40, 3.80], method="shin")
    console.print(f"Shin devig of (2.10, 3.40, 3.80) → {probs}, sum={sum(probs):.6f}")
    console.print(f"EV at 2.10 vs fair_prob {probs[0]:.4f} = {ev_pct(2.10, probs[0]):.3f}%")
    console.print(f"Kelly fraction = {kelly_fraction(2.10, probs[0]):.4f}")
    console.print(f"Quarter Kelly stake on €1000 bankroll = €{kelly_stake(2.10, probs[0], 1000.0):.2f}")


if __name__ == "__main__":
    app()
