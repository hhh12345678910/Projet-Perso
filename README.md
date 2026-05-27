# valuebet-be

Value betting engine for the Belgian sports betting market. Uses Pinnacle as the
sharp reference (devigged → fair probability) and compares against soft books
(Betano.be, Unibet.be, Ladbrokes.be, Circus, BetFirst, Betcenter) to surface
+EV bets. Tracks Closing Line Value (CLV) as the primary KPI.

## Layout

```
src/
  models.py       data classes: Event, Outcome, OddQuote, ValueBet
  devig.py        Shin / power / multiplicative devigging
  ev.py           EV%, fractional Kelly stake
  matcher.py      fuzzy event matching across books
  storage.py      SQLite (events, quotes, bets, clv)
  scrapers/
    base.py       Scraper interface
    pinnacle.py   Pinnacle public guest API (sharp reference)
    betano.py     Betano.be (danae-webapi; needs cookie)
    unibet.py     Unibet.be (Kambi offering API; guest)
  main.py         orchestration loop + CLI
tests/            unit tests for math
```

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m src.main scan --sport soccer --min-ev 2.0
pytest
```

## Roadmap

1. Math core + Pinnacle scraper + SQLite (done).
2. Belgian scrapers: Betano.be (done), Unibet.be (done), Ladbrokes.be (next).
3. Circus, BetFirst, Betcenter (custom reverse-engineering needed).
4. Telegram alerter + CLV dashboard.

## Disclaimer

For personal research. Respect each operator's ToS and Belgian gambling
regulations (Commission des Jeux de Hasard). Only `.be` licensed sites are
legal for Belgian residents.
