from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from typing import Iterable

import httpx
import typer
from rich.console import Console
from rich.table import Table

from .config import ScanConfig
from .devig import devig
from .ev import ev_pct, fair_odd, kelly_fraction, kelly_stake
from .matcher import parse_event_key, reconcile_event_keys
from .models import Book, FairLine, MarketType, OddQuote, Outcome, ValueBet
from .scrapers.betano import BetanoAuthError, BetanoScraper, parse_overview as betano_parse_overview
from .scrapers.betfirst import BetFirstScraper, parse_events_table as betfirst_parse_events_table
from .scrapers.goldenpalace import GoldenPalaceScraper, parse_get_events as goldenpalace_parse_get_events
from .scrapers.ladbrokes import LadbrokesScraper, parse_prematch as ladbrokes_parse_prematch
from .scrapers.magicbetting import load_file as magicbetting_load_file, parse_events as magicbetting_parse_events
from .scrapers.pinnacle import PinnacleScraper
from .scrapers.smarkets import SmarketsScraper, iter_all_quotes as smarkets_iter_quotes
from .scrapers.starcasinosport import StarCasinoSportScraper, parse_get_events as starcasinosport_parse_get_events
from .scrapers.unibet import UnibetScraper, parse_listview as unibet_parse_listview
from .storage import Storage
from .surebet import find_surebets
from .clv import (
    aggregate as clv_aggregate,
    clv_pct,
    event_started,
    group_by as clv_group_by,
    index_quotes_by_market,
)
from .alerter import TelegramConfig, send_alerts, send_surebet_alerts
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
    primary_weight: float = 0.7,
) -> dict[tuple[str, MarketType, float | None], FairLine]:
    """Build fair lines from Pinnacle, optionally blending with a secondary
    sharp source (Smarkets exchange) to cross-validate. Pinnacle keeps the
    higher weight by default because its volume and stability are higher;
    Smarkets fills in events Pinnacle doesn't price and gently pulls the
    estimate where the two disagree."""
    primary_groups = _group_quotes(pinnacle_quotes)
    secondary_groups = _group_quotes(secondary_quotes or [])

    fair: dict[tuple[str, MarketType, float | None], FairLine] = {}
    now = datetime.now(timezone.utc)
    for key in primary_groups.keys() | secondary_groups.keys():
        event_key_, market, line = key
        pin_probs = _devig_group(primary_groups.get(key, []), method)
        sec_probs = _devig_group(secondary_groups.get(key, []), method)

        if pin_probs and sec_probs:
            # Weighted blend on outcomes that both sources price; fall back to
            # the available source when only one carries a given label.
            blended: dict[str, float] = {}
            for label in set(pin_probs) | set(sec_probs):
                p, s = pin_probs.get(label), sec_probs.get(label)
                if p is not None and s is not None:
                    blended[label] = primary_weight * p + (1 - primary_weight) * s
                else:
                    blended[label] = p if p is not None else s
            # Renormalise so the row still sums to 1 after the blend.
            total = sum(blended.values())
            if total > 0:
                blended = {k: v / total for k, v in blended.items()}
            outcomes = blended
            ref_book = Book.PINNACLE
        elif pin_probs:
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
    out: list[ValueBet] = []
    now = datetime.now(timezone.utc)
    for q in candidate_quotes:
        if q.book == Book.PINNACLE:
            continue
        fl = fair_lines.get((q.event_key, q.market, q.outcome.line))
        if fl is None:
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
        ))
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


def fetch_betano_quotes(betano_file: str | None = None) -> list[OddQuote]:
    """Parse Betano data. If `betano_file` is given, load the JSON from disk
    (the response body the user captured in their browser) instead of doing a
    live fetch — Cloudflare+DataDome bind cookies to the browser IP, so the
    live path only works from that machine. Returns [] with a warning if no
    file is given and the cookie is missing/expired."""
    if betano_file:
        import json as _json
        from pathlib import Path as _Path
        try:
            data = _json.loads(_Path(betano_file).read_text())
        except (OSError, ValueError) as e:
            console.print(f"[yellow]Betano file unreadable:[/yellow] {e}")
            return []
        return list(betano_parse_overview(data))
    try:
        with BetanoScraper() as bet:
            data = bet.fetch_live_overview()
        return list(betano_parse_overview(data))
    except BetanoAuthError as e:
        console.print(f"[yellow]Betano skipped:[/yellow] {e}")
        return []


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


def fetch_betfirst_quotes(sport: str) -> list[OddQuote]:
    """Fetch + parse the BetFirst events-table for a sport (paginated)."""
    try:
        with BetFirstScraper() as bf:
            data = bf.fetch_all_events(sport, days_ahead=2, max_market_count=10)
        return list(betfirst_parse_events_table(data))
    except httpx.HTTPError as e:
        console.print(f"[yellow]BetFirst skipped:[/yellow] {e}")
        return []


def fetch_ladbrokes_quotes(sport: str) -> list[OddQuote]:
    """Fetch + parse every Ladbrokes meeting of a sport via the detail-service."""
    try:
        with LadbrokesScraper() as lb:
            data = lb.fetch_all_meetings(sport, max_meetings=40)
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


def fetch_smarkets_quotes(sport: str, max_events: int = 200) -> list[OddQuote]:
    """Snapshot Smarkets exchange prices for a sport. Used as a secondary
    sharp reference (Pinnacle stays primary); a failure here only weakens
    the fair-line blend, never aborts the scan."""
    try:
        with SmarketsScraper() as sm:
            return list(smarkets_iter_quotes(sm, sport, max_events=max_events))
    except httpx.HTTPError as e:
        console.print(f"[yellow]Smarkets skipped:[/yellow] {e}")
        return []


def fetch_magicbetting_quotes(magicbetting_file: str | None) -> list[OddQuote]:
    """Magic Betting sits behind Cloudflare from datacenter IPs, so live fetch
    is impossible from cloud. Reads a saved response body via the file flag."""
    if not magicbetting_file:
        return []
    try:
        data = magicbetting_load_file(magicbetting_file)
    except (OSError, ValueError) as e:
        console.print(f"[yellow]Magic Betting file unreadable:[/yellow] {e}")
        return []
    return list(magicbetting_parse_events(data))


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
    magicbetting_file: str = typer.Option(
        None,
        "--magicbetting-file",
        help="Path to a Magic Betting capture (raw XOR-encoded body or already-decoded JSON). "
        "Same workaround as --betano-file: Cloudflare blocks live fetch from datacenters.",
    ),
):
    """Fetch Pinnacle + soft books (Betano, Unibet, BetFirst, Ladbrokes, Golden Palace, StarCasino, Magic Betting), compute fair lines, print top value bets.

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

        console.print(f"[bold]Fetching Pinnacle {current_sport} markets...[/bold]")
        with PinnacleScraper() as pin:
            quotes = list(pin.fetch_market_quotes(current_sport))
        console.print(f"  → {len(quotes)} Pinnacle quotes")

        # Smarkets exchange as a secondary sharp reference (margin-free
        # peer-to-peer prices). Pinnacle stays primary in the blend.
        smarkets_quotes = fetch_smarkets_quotes(current_sport)
        console.print(f"  → {len(smarkets_quotes)} Smarkets quotes")

        # Match Smarkets event keys onto Pinnacle's so the blend per
        # (event, market, line) actually lands on the same row.
        if smarkets_quotes:
            smarkets_quotes = remap_to_reference(
                smarkets_quotes, {q.event_key for q in quotes}
            )

        fair = build_fair_lines(
            quotes, cfg.devig_method, secondary_quotes=smarkets_quotes,
        )
        console.print(f"  → {len(fair)} fair lines (devig={cfg.devig_method}, sharp=Pinnacle+Smarkets)")

        for q in quotes:
            storage.insert_quote(q)

        console.print("[bold]Fetching soft books...[/bold]")
        # Betano/Magic Betting are file-mode books; only consume them on the
        # first sport iteration so we don't double-parse the same dump.
        betano_quotes = (
            fetch_betano_quotes(betano_file=betano_file)
            if current_sport == sports[0] else []
        )
        console.print(f"  → {len(betano_quotes)} Betano quotes")
        unibet_quotes = fetch_unibet_quotes(current_sport)
        console.print(f"  → {len(unibet_quotes)} Unibet quotes")
        betfirst_quotes = fetch_betfirst_quotes(current_sport)
        console.print(f"  → {len(betfirst_quotes)} BetFirst quotes")
        ladbrokes_quotes = fetch_ladbrokes_quotes(current_sport)
        console.print(f"  → {len(ladbrokes_quotes)} Ladbrokes quotes")
        goldenpalace_quotes = fetch_goldenpalace_quotes(current_sport)
        console.print(f"  → {len(goldenpalace_quotes)} Golden Palace quotes")
        starcasinosport_quotes = fetch_starcasinosport_quotes(current_sport)
        console.print(f"  → {len(starcasinosport_quotes)} StarCasino Sport quotes")
        magicbetting_quotes = (
            fetch_magicbetting_quotes(magicbetting_file)
            if current_sport == sports[0] else []
        )
        console.print(f"  → {len(magicbetting_quotes)} Magic Betting quotes")
        sport = current_sport  # keep local var name for downstream prints

        ref_keys = {fl.event_key for fl in fair.values()}
        soft_quotes = remap_to_reference(
            betano_quotes + unibet_quotes + betfirst_quotes + ladbrokes_quotes
            + goldenpalace_quotes + starcasinosport_quotes + magicbetting_quotes,
            ref_keys,
        )
        console.print(f"  → {len(soft_quotes)} matched to a Pinnacle event")
        for q in soft_quotes:
            storage.insert_quote(q)

        bets = find_value_bets(soft_quotes, fair, cfg)
        bets.sort(key=lambda b: b.ev_pct, reverse=True)
        console.print(f"[bold]Value bets: {len(bets)}[/bold]")

        # Persist every detected value bet so close-lines / clv-report can track
        # whether the engine actually beats the closing line over time.
        newly_detected: list[ValueBet] = []
        for b in bets:
            before = storage.find_value_bet_id(
                b.event_key, b.book.value, b.market.value, b.outcome.label, b.outcome.line
            )
            storage.insert_value_bet(b)
            if before is None:
                newly_detected.append(b)
        if newly_detected:
            console.print(f"  → {len(newly_detected)} new bets persisted for CLV tracking")

        # Telegram value-bet notifications. By default only fresh detections
        # fire so the same opportunity on consecutive scans doesn't spam the
        # chat — flip TELEGRAM_VALUEBET_DEDUP=0 in .env to re-notify on
        # every cycle the same way the surebet path already supports.
        tg_cfg = TelegramConfig.from_env()
        if tg_cfg is not None:
            candidates = newly_detected if tg_cfg.valuebet_dedup else bets
            sent = send_alerts(candidates, tg_cfg, print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"), sport=current_sport)
            if sent:
                console.print(f"  → {sent} Telegram alerts sent (EV ≥ {tg_cfg.min_ev_pct:.1f}%)")

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
        surebets = find_surebets(soft_quotes + quotes)
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
            if tg_cfg.surebet_dedup:
                candidates = [
                    s for s in candidates
                    if not storage.surebet_already_notified(
                        s.event_key, s.market.value, s.line
                    )
                ]
            if candidates:
                sent = send_surebet_alerts(
                    candidates, tg_cfg,
                    print_fn=lambda x: console.print(f"[yellow]{x}[/yellow]"),
                    sport=current_sport,
                )
                # Track every candidate (even sub-margin) so the table is a
                # complete history we can audit later — only useful for the
                # dedup path, but cheap enough to do unconditionally.
                now = datetime.now(timezone.utc)
                for s in candidates:
                    storage.mark_surebet_notified(
                        s.event_key, s.market.value, s.line, s.margin * 100, now,
                    )
                if sent:
                    console.print(f"  → {sent} surebet alerts sent")
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
    magicbetting_file: str = typer.Option(
        None, "--magicbetting-file",
        help="Optional Magic Betting dump path — same as in `scan`.",
    ),
):
    """Light surebet-only sweep on the soft books, designed to run every
    15-30 min between the full 2h scans. Pinnacle's per-league walk takes
    5-10 min and would burn the whole cycle, so we skip it here — those
    Pinnacle-leg surebets still get caught by the regular `scan`. Comma-
    separated --sport lets one cron entry cover every sport you care about."""
    sports = [s.strip() for s in sport.split(",") if s.strip()]
    storage = Storage(ScanConfig().db_path)
    teams.init(storage)
    tg_cfg = TelegramConfig.from_env()

    for current_sport in sports:
        console.print()
        console.print(f"[bold green]══ {current_sport.upper()} (surebets only) ══[/bold green]")

        # Pull every soft book for this sport.
        all_quotes: list[OddQuote] = []
        all_quotes += fetch_betano_quotes(betano_file=betano_file) if current_sport == sports[0] else []
        unibet_quotes = fetch_unibet_quotes(current_sport)
        all_quotes += unibet_quotes
        all_quotes += fetch_betfirst_quotes(current_sport)
        all_quotes += fetch_ladbrokes_quotes(current_sport)
        all_quotes += fetch_goldenpalace_quotes(current_sport)
        all_quotes += fetch_starcasinosport_quotes(current_sport)
        all_quotes += fetch_magicbetting_quotes(magicbetting_file) if current_sport == sports[0] else []
        console.print(f"  → {len(all_quotes)} soft-book quotes total")

        if not all_quotes:
            continue

        # No Pinnacle reference key set here — pick the soft book with the
        # widest coverage as anchor and reconcile the rest onto it. Unibet
        # is the typical winner since fetch_all_events walks every termKey.
        from collections import Counter
        by_book = Counter(q.event_key for q in all_quotes if q.book == Book.UNIBET_BE)
        if by_book:
            ref_keys = set(by_book)
        else:
            # Fall back to the largest book in the pool.
            book_counts = Counter(q.book for q in all_quotes)
            top_book = book_counts.most_common(1)[0][0]
            ref_keys = {q.event_key for q in all_quotes if q.book == top_book}

        normalised_quotes = remap_to_reference(all_quotes, ref_keys)
        console.print(f"  → {len(normalised_quotes)} matched to a common event")

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
        if tg_cfg.surebet_dedup:
            candidates = [
                s for s in candidates
                if not storage.surebet_already_notified(s.event_key, s.market.value, s.line)
            ]
        if not candidates:
            continue
        sent = send_surebet_alerts(
            candidates, tg_cfg,
            print_fn=lambda x: console.print(f"[yellow]{x}[/yellow]"),
            sport=current_sport,
        )
        now = datetime.now(timezone.utc)
        for s in candidates:
            storage.mark_surebet_notified(
                s.event_key, s.market.value, s.line, s.margin * 100, now,
            )
        if sent:
            console.print(f"  → {sent} surebet alerts sent")


@app.command(name="alert-test")
def alert_test():
    """Send one dummy value-bet alert and one dummy surebet alert to verify
    both Telegram channels are wired up. The value bet goes to TELEGRAM_CHAT_ID;
    the surebet goes to TELEGRAM_SUREBET_CHAT_ID if set, otherwise it falls
    back to the same chat so the existing single-channel setup keeps working."""
    cfg = TelegramConfig.from_env()
    if cfg is None:
        console.print(
            "[yellow]TELEGRAM_BOT_TOKEN and/or TELEGRAM_CHAT_ID are not set "
            "— nothing to send.[/yellow]"
        )
        return

    sample_bet = ValueBet(
        event_key="202606010000::testteamA__vs__testteamB",
        book=Book.UNIBET_BE,
        market=MarketType.H2H,
        outcome=Outcome(label="home"),
        odd_taken=1.86,
        fair_prob=0.5650,
        fair_odd=1.77,
        ev_pct=5.17,
        kelly_stake_pct=1.50,
        detected_at=datetime.now(timezone.utc),
    )
    sample_surebet = Surebet(
        event_key="202606010000::testteamA__vs__testteamB",
        market=MarketType.H2H,
        line=None,
        legs={
            "home": (1.95, Book.UNIBET_BE),
            "draw": (3.85, Book.BETFIRST),
            "away": (4.20, Book.LADBROKES_BE),
        },
        margin=0.0234,
    )

    bet_sent = send_alerts(
        [sample_bet], cfg, print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
        sport="soccer",
    )
    surebet_sent = send_surebet_alerts(
        [sample_surebet], cfg, print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
        sport="soccer",
    )

    if bet_sent:
        console.print(f"[bold]Value bet → chat {cfg.chat_id} ✓[/bold]")
    else:
        console.print("[red]Value bet alert NOT sent — check the messages above.[/red]")
    target = cfg.effective_surebet_chat_id
    same_chat = target == cfg.chat_id
    suffix = " (same as main — TELEGRAM_SUREBET_CHAT_ID not set)" if same_chat else " (dedicated surebet chat)"
    if surebet_sent:
        console.print(f"[bold]Surebet → chat {target} ✓{suffix}[/bold]")
    else:
        console.print("[red]Surebet alert NOT sent — check the messages above.[/red]")


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

    for dim in ("book", "market"):
        groups = clv_group_by(rows, dim)
        stats = {k: clv_aggregate(v) for k, v in groups.items() if v}
        if not stats:
            continue
        t = Table(title=f"CLV by {dim}", show_lines=False)
        t.add_column(dim)
        t.add_column("n", justify="right")
        t.add_column("mean CLV%", justify="right")
        t.add_column("median%", justify="right")
        t.add_column("positive%", justify="right")
        for k in sorted(stats, key=lambda k: stats[k].mean_clv_pct, reverse=True):
            s = stats[k]
            t.add_row(
                k, str(s.n),
                f"{s.mean_clv_pct:+.2f}", f"{s.median_clv_pct:+.2f}",
                f"{s.positive_rate * 100:.1f}",
            )
        console.print(t)


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
