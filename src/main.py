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
from .matcher import reconcile_event_keys
from .models import Book, FairLine, MarketType, OddQuote, ValueBet
from .scrapers.betano import BetanoAuthError, BetanoScraper, parse_overview as betano_parse_overview
from .scrapers.betfirst import BetFirstScraper, parse_events_table as betfirst_parse_events_table
from .scrapers.ladbrokes import LadbrokesScraper, parse_prematch as ladbrokes_parse_prematch
from .scrapers.pinnacle import PinnacleScraper
from .scrapers.unibet import UnibetScraper, parse_listview as unibet_parse_listview
from .storage import Storage


app = typer.Typer(add_completion=False)
console = Console()


def _group_quotes(quotes: Iterable[OddQuote]) -> dict[tuple[str, MarketType, float | None], list[OddQuote]]:
    """Group Pinnacle quotes into competing-outcome sets per (event, market, line)."""
    groups: dict[tuple[str, MarketType, float | None], list[OddQuote]] = defaultdict(list)
    for q in quotes:
        groups[(q.event_key, q.market, q.outcome.line)].append(q)
    return groups


def build_fair_lines(pinnacle_quotes: list[OddQuote], method: str) -> dict[tuple[str, MarketType, float | None], FairLine]:
    fair: dict[tuple[str, MarketType, float | None], FairLine] = {}
    for key, group in _group_quotes(pinnacle_quotes).items():
        if len(group) < 2:
            continue
        labels = [q.outcome.label for q in group]
        odds = [q.decimal_odd for q in group]
        try:
            probs = devig(odds, method=method)
        except Exception:
            continue
        event_key_, market, line = key
        fair[key] = FairLine(
            event_key=event_key_,
            market=market,
            outcomes={lbl: p for lbl, p in zip(labels, probs)},
            method=method,
            reference_book=Book.PINNACLE,
            computed_at=datetime.now(timezone.utc),
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


def remap_to_reference(
    soft_quotes: list[OddQuote],
    reference_keys: Iterable[str],
) -> list[OddQuote]:
    """Re-key soft-book quotes onto the matching Pinnacle event_key via fuzzy
    matching, so they line up with the fair lines. Unmatched quotes are dropped."""
    soft_to_ref = reconcile_event_keys(
        reference_keys=list(reference_keys),
        candidate_keys={q.event_key for q in soft_quotes},
    )
    out: list[OddQuote] = []
    for q in soft_quotes:
        ref = soft_to_ref.get(q.event_key)
        if ref is None:
            continue
        out.append(replace(q, event_key=ref) if ref != q.event_key else q)
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
    """Fetch + parse the Unibet (Kambi) list view for a sport."""
    try:
        with UnibetScraper() as uni:
            data = uni.fetch_listview(sport)
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
    """Fetch + parse every Ladbrokes football meeting via the detail-service."""
    if sport != "soccer":
        return []
    try:
        with LadbrokesScraper() as lb:
            data = lb.fetch_all_meetings("FOOTBALL", max_meetings=40)
        return list(ladbrokes_parse_prematch(data))
    except httpx.HTTPError as e:
        console.print(f"[yellow]Ladbrokes skipped:[/yellow] {e}")
        return []


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
    """Fetch Pinnacle + soft books (Betano, Unibet, BetFirst, Ladbrokes), compute fair lines, print top value bets."""
    cfg = ScanConfig(sport=sport, min_ev_pct=min_ev, bankroll=bankroll)
    storage = Storage(cfg.db_path)

    console.print(f"[bold]Fetching Pinnacle {sport} markets...[/bold]")
    with PinnacleScraper() as pin:
        quotes = list(pin.fetch_market_quotes(sport))
    console.print(f"  → {len(quotes)} Pinnacle quotes")

    fair = build_fair_lines(quotes, cfg.devig_method)
    console.print(f"  → {len(fair)} fair lines (devig={cfg.devig_method})")

    for q in quotes:
        storage.insert_quote(q)

    console.print("[bold]Fetching soft books...[/bold]")
    betano_quotes = fetch_betano_quotes(betano_file=betano_file)
    console.print(f"  → {len(betano_quotes)} Betano quotes")
    unibet_quotes = fetch_unibet_quotes(sport)
    console.print(f"  → {len(unibet_quotes)} Unibet quotes")
    betfirst_quotes = fetch_betfirst_quotes(sport)
    console.print(f"  → {len(betfirst_quotes)} BetFirst quotes")
    ladbrokes_quotes = fetch_ladbrokes_quotes(sport)
    console.print(f"  → {len(ladbrokes_quotes)} Ladbrokes quotes")

    ref_keys = {fl.event_key for fl in fair.values()}
    soft_quotes = remap_to_reference(
        betano_quotes + unibet_quotes + betfirst_quotes + ladbrokes_quotes, ref_keys
    )
    console.print(f"  → {len(soft_quotes)} matched to a Pinnacle event")
    for q in soft_quotes:
        storage.insert_quote(q)

    bets = find_value_bets(soft_quotes, fair, cfg)
    bets.sort(key=lambda b: b.ev_pct, reverse=True)
    console.print(f"[bold]Value bets: {len(bets)}[/bold]")

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
