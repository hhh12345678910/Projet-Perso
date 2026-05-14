from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable

import typer
from rich.console import Console
from rich.table import Table

from .config import ScanConfig
from .devig import devig
from .ev import ev_pct, fair_odd, kelly_fraction, kelly_stake
from .models import Book, FairLine, MarketType, OddQuote, ValueBet
from .scrapers.pinnacle import PinnacleScraper
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


@app.command()
def scan(
    sport: str = "soccer",
    min_ev: float = 2.0,
    bankroll: float = 1000.0,
):
    """Fetch Pinnacle, compute fair lines, print top value bets (no soft books wired yet)."""
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

    # No Belgian books yet — show fair lines preview only.
    table = Table(title=f"Pinnacle fair lines preview ({sport})", show_lines=False)
    table.add_column("event_key", overflow="fold")
    table.add_column("market")
    table.add_column("line")
    table.add_column("outcomes")
    for (ek, m, line), fl in list(fair.items())[:15]:
        outs = ", ".join(f"{k}={v:.3f}" for k, v in fl.outcomes.items())
        table.add_row(ek, m.value, str(line) if line is not None else "-", outs)
    console.print(table)

    bets = find_value_bets(quotes, fair, cfg)
    console.print(f"[bold]Value bets vs Pinnacle: {len(bets)}[/bold] (will be >0 once soft books are wired)")


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
