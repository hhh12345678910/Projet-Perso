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
    betfirst.py   BetFirst.be (Entain sportsbook API; guest)
    ladbrokes.py  Ladbrokes.be (Eurobet sport-schedule; guest)
    goldenpalace.py Golden Palace (Altenar GetEvents widget; guest)
    starcasinosport.py StarCasino Sport (same Altenar API, different operator)
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

## Including Betano in a scan

Betano sits behind Cloudflare + DataDome. Their tokens expire every few hours,
so the VM needs a periodically refreshed cookie.

### Automatic (recommended)

`tools/betano-cookie.user.js` is a Tampermonkey userscript: leave a
`betanosports.be` tab open and every 5 min it reads the session cookie and
POSTs it to `scripts/betano_ingest_server.py` on the VM, which stores it in
`data/betano_cookie.json`. `BetanoScraper` re-reads that file on every fetch,
so a new cookie takes effect with no daemon restart and no manual pasting.

Setup: generate a token (`openssl rand -hex 32`) into `BETANO_INGEST_TOKEN`,
run the ingest server (`scripts/betano-ingest.service`), open the port, then
paste the same token into the userscript's `TOKEN`.

The script never calls Betano's API — it only reads cookies the browser
already holds — so it adds no traffic for DataDome to score.

### Manual fallback

Capture the response once and feed it to `scan`.

1. In Chrome on `betanosports.be` (logged in), open the URL directly:
   `https://www.betanosports.be/fr/danae-webapi/api/live/overview/latest?includeVirtuals=true&queryLanguageId=9&queryOperatorId=22`
2. Save the JSON the page returns (Ctrl+S) to e.g. `betano.json`.
3. Run with the flag:
   ```bash
   python -m src.main scan --sport soccer --min-ev 2.0 --betano-file betano.json
   ```

Refresh the file whenever you want fresh Betano odds.

## Closing Line Value tracking

Each `scan` persists every detected value bet to the local SQLite. Once an
event has kicked off, snapshot the Pinnacle closing price for the same
outcome, then aggregate CLV stats:

```bash
# during the day — detect + persist value bets
python -m src.main scan

# after kickoffs (e.g. cron 5 past every hour) — record closing lines
python -m src.main close-lines

# anytime — see how well the engine is beating the close
python -m src.main clv-report
```

Mean CLV is the single most reliable indicator of long-run profitability:
roughly positive and stable -> the engine is finding real edges; around
zero -> the +EV signals are noise; negative -> you're paying the soft
book's margin without any sharp justification.

## Telegram alerts

Set two env vars and `scan` will push a notification for every newly
detected value bet above the EV threshold (re-detections of the same
opportunity are deduped, so you won't get spammed).

```bash
export TELEGRAM_BOT_TOKEN=<token from @BotFather>
export TELEGRAM_CHAT_ID=<your chat id>
export TELEGRAM_MIN_EV=3.0          # optional, defaults to 3.0
python -m src.main alert-test       # one-off sanity check
python -m src.main scan             # alerts fire automatically
```

To get your chat id: message your bot once, then visit
`https://api.telegram.org/bot<TOKEN>/getUpdates` and read `message.chat.id`.

## Roadmap

1. Math core + Pinnacle scraper + SQLite (done).
2. Belgian scrapers: Betano.be (done), Unibet.be (done), BetFirst.be (done), Ladbrokes.be (done).
3. Circus, Betcenter (custom reverse-engineering needed).
4. Telegram alerter + CLV dashboard.

## Disclaimer

For personal research. Respect each operator's ToS and Belgian gambling
regulations (Commission des Jeux de Hasard). Only `.be` licensed sites are
legal for Belgian residents.
