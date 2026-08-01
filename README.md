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
    betano.py     Betano.be (danae-webapi; fed by browser userscript)
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

Betano is behind DataDome, which scores the *requesting IP*. A datacenter IP
gets 403 even with a valid, freshly-minted session cookie, so the fetch has to
happen from a browser DataDome already trusts.

Pushing the cookie to the VM and letting it fetch was tried and does not work —
measured, not assumed. `cf_clearance` never even appears on the session, so the
block is DataDome's, not Cloudflare's. `tools/betano-cookie.user.js` and the
server's `/ingest-cookie` route are what remains of that attempt; they're kept
because `BetanoScraper` still accepts a pushed cookie, which is useful if you
ever run the daemon from a residential IP.

### Automatic (recommended)

`tools/betano-ingest.user.js` is a Tampermonkey userscript that automates the
manual capture below. Leave a `betanosports.be` tab open and it pushes two
feeds to `scripts/betano_ingest_server.py` on the VM:

- **live** (every 15 s) — `/danae-webapi/api/live/overview`, all sports in one
  payload, in-play only. Written to `data/betano.json`; `scan-daemon.sh` passes
  it as `--betano-file` when present.
- **prematch** (every 5 min) — `/fr/api/sport/{slug}/matchs-a-venir`, a
  *different* API with its own market codes. That endpoint only covers ~24 h,
  so the script uses its blocks as a competition index and walks each
  competition's own url for the full calendar, merging the results into one
  payload per sport. Written to `data/prematch/{sport}.json` and read by
  `fetch_betano_quotes`.

The prematch feed is the one that matters: the live overview is in-play only
(measured: 165 of 172 events already started), so without it Betano
contributes nothing to prematch value betting.

**The tab has to stay open.** Nothing on the VM can refresh these files, so if
the browser closes or the machine sleeps they freeze rather than disappear —
which would otherwise look exactly like fresh data. `BETANO_LIVE_MAX_AGE_MIN`
(default 5) and `BETANO_PREMATCH_MAX_AGE_MIN` (default 30) make the daemon
ignore a feed past that age and log why. Set either to 0 to replay an old dump
offline.

Setup: generate a token (`openssl rand -hex 32`) into `BETANO_INGEST_TOKEN`,
install the systemd units with `bash scripts/setup.sh` (it renders
`scripts/*.service.in` for the current user and path — the ingest server
serves Circus too), open the port in the firewall, then paste the same token
into the userscript's `TOKEN`. `bash scripts/setup.sh --check` compares the
installed units against what the repo would produce, without changing
anything.

Betano freshness equals the push interval, bounded below by the daemon's own
cycle. The upload is ~430 KB measured (163 events / 1239 selections), so 15 s
is comfortable; the on-page banner reports the size each cycle if it grows.

### Manual fallback

Capture the response once and feed it to `scan`.

1. In Chrome on `betanosports.be` (logged in), open the URL directly:
   `https://www.betanosports.be/fr/danae-webapi/api/live/overview/latest?includeVirtuals=false&queryLanguageId=9&queryOperatorId=22`
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
