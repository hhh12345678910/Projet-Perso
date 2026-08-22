"""La collecte : interroger les books, en parallèle, sans jamais bloquer le cycle.

Extrait de `main.py` sans changement de comportement. C'est la couche qui parle
au monde extérieur — la seule du moteur à faire du réseau — et elle porte à ce
titre tout ce que le réseau impose : caches de fond, verrous, reculs après 403,
espacement des appels.

CE QUI EST ICI, ET POURQUOI ÇA TIENT ENSEMBLE
---------------------------------------------
- un `fetch_*` par book, chacun responsable de sa propre panne : **un book qui
  tombe rend une liste vide, il ne fait pas tomber le cycle** ;
- trois caches de fond (BetFirst, Smarkets, EliteSports profond) : un
  rafraîchissement long tourne dans un thread détaché, le cycle lit ce qui est
  prêt et repart. C'est ce motif qui a permis de garder BetFirst après qu'un
  appel synchrone de 26 minutes eut fait retirer Smarkets en juillet ;
- toute la machinerie Pinnacle : réutilisation du cache, espacement minimal
  entre appels, recul exponentiel après 403, mise en veille après N réponses
  vides. Pinnacle est le point de défaillance unique du système, et c'est ici
  qu'il est ménagé ;
- `fetch_all_parallel`, le registre des books et le coupe-circuit
  `BOOKS_DISABLED`.

CE QUI N'EST PAS ICI, ET POURQUOI
----------------------------------
⚠️ La SURVEILLANCE de la collecte (`_pinnacle_health`, `_book_health`) est
restée dans `main.py`. Elle ressemble à de l'orchestration mais n'en est pas :
elle ne collecte rien, elle décide d'envoyer une alerte Telegram à partir de ce
que la collecte a renvoyé. C'est une responsabilité de la boucle du daemon, qui
possède la configuration Telegram. Les déplacer aurait fait entrer l'alerter
dans ce module sans rien y gagner.

⚠️ L'interface avec `main.py` est VOLONTAIREMENT ÉTROITE : **6 noms sur les 66
définis ici**. Le reste est privé à la collecte. Un réexport large aurait fait
passer les tests sans rien prouver — un test qui remplace `main.PinnacleScraper`
ne toucherait pas le nom que ce module consulte, et continuerait de passer en
interrogeant le vrai Pinnacle. Les 47 points d'accroche de la suite ont donc été
recâblés sur `src.orchestration`, un par un.
"""
from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Callable

import httpx
from tenacity import RetryError

from .config import SMARKETS_ENABLED
from .models import MarketType, OddQuote
from .scrapers.betano import (
    BetanoAuthError,
    BetanoScraper,
    parse_overview as betano_parse_overview,
    parse_prematch as betano_parse_prematch,
    prematch_file as betano_prematch_file,
)
from .scrapers.betcenter import BetCenterScraper
from .scrapers.betfirst import BetFirstScraper, parse_events_table as betfirst_parse_events_table
from .scrapers.bingoal import BingoalScraper, parse_listview as bingoal_parse_listview
from .scrapers.circus import load_pushed_quotes as circus_load_pushed
from .scrapers.elitesports import EliteSportsScraper
from .scrapers.elitesports import parse_prematch as elitesports_parse_prematch
from .scrapers.goldenpalace import GoldenPalaceScraper, parse_get_events as goldenpalace_parse_get_events
from .scrapers.ladbrokes import LadbrokesScraper, parse_prematch as ladbrokes_parse_prematch
from .scrapers.magicbetting import load_pushed_quotes as magic_load_pushed
from .scrapers.meridianbet import MeridianScraper, parse_offer as meridian_parse_offer
from .scrapers.napoleon import NapoleonScraper, parse_by_date as napoleon_parse_by_date
from .scrapers.pinnacle import PinnacleScraper
from .scrapers.scooore import ScoooreScraper, parse_listview as scooore_parse_listview
from .scrapers.sevenelevenbe import SevenElevenScraper, parse_listview as sevenelevenbe_parse_listview
from .scrapers.smarkets import (
    SPORT_DOMAINS as SMARKETS_SPORT_DOMAINS,
    SmarketsScraper,
    iter_all_quotes_fast as smarkets_iter_all_quotes_fast,
)
from .scrapers.starcasinosport import StarCasinoSportScraper, parse_get_events as starcasinosport_parse_get_events
from .scrapers.unibet import UnibetScraper, parse_listview as unibet_parse_listview
from .ui import console


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
                # Marquées comme live : le détecteur de marché en retard doit
                # pouvoir les distinguer du prématch, sinon tout match en cours
                # coté par Betano passerait pour une erreur du book.
                quotes.extend(replace(q, from_live_feed=True)
                              for q in betano_parse_overview(data))
        else:
            try:
                with BetanoScraper() as bet:
                    quotes.extend(
                        replace(q, from_live_feed=True)
                        for q in betano_parse_overview(bet.fetch_live_overview()))
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
    """Fetch + parse Napoleon's prematch match-winner offer (Superbet platform,
    public REST, independent odds — genuinely widens value/surebet coverage).

    Le sport doit être transmis au parseur : l'identifiant du marché vainqueur
    en dépend, et sans lui le tennis était intégralement jeté."""
    try:
        with NapoleonScraper() as nap:
            data = nap.fetch_by_date(sport)
        return list(napoleon_parse_by_date(data, sport))
    except httpx.HTTPError as e:
        console.print(f"[yellow]Napoleon skipped:[/yellow] {e}")
        return []


# BetFirst hors du cycle : rafraîchi en arrière-plan, jamais attendu.
#
# Même paginé en parallèle, ce book reste le plus lent du portefeuille — et
# fetch_all_parallel attend TOUS les books avant de rendre la main, donc sa
# durée devient celle du cycle entier. Or il n'a aucune raison d'être frais à
# la seconde : ses prix sont les pires du portefeuille (−3,20 points de CLV à
# sélection identique), il est là pour la donnée, pas pour être joué.
#
# Le cycle lit donc toujours le cache et repart aussitôt. Un rafraîchissement
# est lancé en fond quand le cache a vieilli ; le premier cycle après un
# démarrage voit un cache vide, ce qui est sans conséquence.
_BETFIRST_TTL = float(os.getenv("BETFIRST_REFRESH_SEC", "300"))
# Au-delà, on préfère RIEN à des cotes mortes. C'est la leçon de la garde de
# fraîcheur du §5 : un flux périmé traité comme frais fabrique des value bets
# contre des prix qui n'existent plus, en silence.
_BETFIRST_MAX_AGE = float(os.getenv("BETFIRST_MAX_AGE_SEC", "1200"))
_BETFIRST_CACHE: dict[str, tuple[float, list[OddQuote]]] = {}
_BETFIRST_REFRESHING: set[str] = set()
_BETFIRST_LOCK = threading.Lock()


def _betfirst_refresh(sport: str) -> None:
    """Recharge BetFirst en fond. Ne lève jamais : c'est un thread détaché, une
    exception y serait perdue et emporterait le drapeau de rafraîchissement."""
    try:
        with BetFirstScraper() as bf:
            data = bf.fetch_all_events(sport, days_ahead=3, max_market_count=10)
        quotes = list(betfirst_parse_events_table(data))
        with _BETFIRST_LOCK:
            _BETFIRST_CACHE[sport] = (time.monotonic(), quotes)
        console.print(f"\\[{sport}]   BetFirst rafraîchi : {len(quotes)} cotes")
    except Exception as e:                                      # noqa: BLE001
        console.print(f"[yellow]BetFirst refresh échoué : {e}[/yellow]")
    finally:
        with _BETFIRST_LOCK:
            _BETFIRST_REFRESHING.discard(sport)


def fetch_betfirst_quotes(sport: str) -> list[OddQuote]:
    """Cotes BetFirst en cache. Ne bloque jamais le cycle."""
    now = time.monotonic()
    with _BETFIRST_LOCK:
        ts, quotes = _BETFIRST_CACHE.get(sport, (0.0, []))
        age = now - ts if ts else float("inf")
        stale = age > _BETFIRST_TTL
        busy = sport in _BETFIRST_REFRESHING
        if stale and not busy:
            _BETFIRST_REFRESHING.add(sport)
            launch = True
        else:
            launch = False
    if launch:
        threading.Thread(target=_betfirst_refresh, args=(sport,), daemon=True).start()
    if age > _BETFIRST_MAX_AGE:
        return []
    return quotes


# --------------------------------------------------------------- Smarkets ----
#
# Seconde référence sharp, en repli strict derrière Pinnacle (voir
# `build_fair_lines`). Retirée en juillet parce qu'un rafraîchissement prenait
# ~26 minutes DANS le cycle de scan, silenciant un sport entier (§5).
#
# Deux choses ont changé :
#   1. le scraper groupe désormais ses identifiants par virgule sur les trois
#      passes (marchés, contrats, cotes) — mesuré sur l'API le 13/08 : les lots
#      de 50 passent, et 426 événements coûtent ~150 requêtes au lieu de ~1 000 ;
#   2. il est servi depuis un cache de fond, comme BetFirst : le cycle lit le
#      cache et repart aussitôt, il n'attend JAMAIS Smarkets.
#
# Ces deux points visent la seule raison du retrait. La juridiction n'en était
# pas une : l'API est publique, sans authentification, et sert de source de
# données — on ne parie pas dessus.
# ⛔ ÉTEINT depuis le 16/08. Plus aucun appel, aucun cache, aucune cote stockée.
#
# Le repli Smarkets a été mesuré contre la seule règle indépendante disponible
# — le consensus dévigé des softbooks, Pinnacle ne priçant jamais ces marchés :
# sur 24 paris, CLV moyenne −20,6 %, médiane −21,7 %, et **0 % de positifs**.
# Zéro sur vingt-quatre, soit environ une chance sur seize millions que ce soit
# le hasard. Contre sa PROPRE clôture, le même échantillon affichait +32,8 % —
# 53 points d'écart, qui mesurent exactement ce que vaut une référence jugée
# par elle-même.
#
# Le défaut est structurel : le repli ne se déclenche que sur les marchés que
# Pinnacle ignore, donc sur les compétitions confidentielles, donc là où le
# carnet d'un exchange est vide. Il va chercher sa référence précisément où
# elle est la moins fiable. Vu sur radualbot — viktorfrydrych : juste Smarkets
# à 3,71 quand SIX books s'accordent sur 10,22.
#
# Le code du scraper et ses tests restent en place — rallumer demande
# SMARKETS_ENABLED=1, et la mesure se refera si sa liquidité change. Mais tant
# que ce drapeau vaut 0, rien ne tourne et le cycle ne paie rien.
# Défini dans `config.py` : voir l'import en tête de module.
# Smarkets sert-il de RÉFÉRENCE de repli, ou seulement d'observateur ?
#
# ⛔ Par défaut NON, et c'est une décision prise sur mesure, le 16/08.
#
# Sur 24 paris valorisés contre le repli Smarkets et jugés au consensus dévigé
# des softbooks — règle indépendante de Smarkets, c'est tout son intérêt — la
# CLV ressort à −20,6 % de moyenne, −21,7 % de médiane, et **0 % de positifs**.
# Zéro sur vingt-quatre : environ une chance sur seize millions que ce soit le
# hasard. L'écart avec la mesure faite contre Smarkets lui-même atteint 53
# points de CLV, ce qui dit exactement à quel point mesurer une référence avec
# elle-même trompe.
#
# Le mécanisme se lit sur un cas : radualbot — viktorfrydrych, juste Smarkets à
# 3,71 quand SIX books s'accordent sur 10,22. Smarkets voit 27 % de chances là
# où le marché en voit 10. Ce n'est pas un désaccord de pricing, c'est une
# offre orpheline dans un carnet vide — et le repli va la chercher précisément
# là où l'exchange est le plus creux, puisqu'il ne se déclenche que sur les
# marchés que Pinnacle ignore.
#
# Le contrôle de marge posé le même jour ne suffit pas : il attrape les carnets
# grossièrement incohérents (144 % de marge sur les totaux 6,5), pas une cote
# isolée qui reste plausible à côté de sa contrepartie.
#
# ⚠️ Smarkets continue d'être collecté, stocké, tracé dans odds_history et
# comparé : rien n'est perdu, et si sa liquidité s'améliore la mesure le dira.
# Seule la fabrication de lignes JUSTES lui est retirée.
# Défini dans `config.py` : voir l'import en tête de module.
_SMARKETS_TTL = float(os.getenv("SMARKETS_REFRESH_SEC", "300"))
# Une référence périmée est pire que pas de référence : elle fabriquerait des
# value bets contre une ligne juste qui n'existe plus. Même leçon que la garde
# de fraîcheur des ponts navigateur (§5).
_SMARKETS_MAX_AGE = float(os.getenv("SMARKETS_MAX_AGE_SEC", "1800"))
_SMARKETS_HOURS = float(os.getenv("SMARKETS_HOURS", "48"))
_SMARKETS_CACHE: dict[str, tuple[float, list[OddQuote]]] = {}
_SMARKETS_REFRESHING: set[str] = set()
_SMARKETS_LOCK = threading.Lock()


def _smarkets_refresh(sport: str) -> None:
    """Recharge Smarkets en fond. Ne lève jamais — thread détaché."""
    try:
        t0 = time.monotonic()
        with SmarketsScraper() as sm:
            quotes = list(smarkets_iter_all_quotes_fast(
                sm, sport, within_hours=_SMARKETS_HOURS
            ))
        with _SMARKETS_LOCK:
            _SMARKETS_CACHE[sport] = (time.monotonic(), quotes)
        console.print(
            f"\\[{sport}]   Smarkets rafraîchi : {len(quotes)} cotes "
            f"en {time.monotonic() - t0:.0f} s"
        )
    except Exception as e:                                          # noqa: BLE001
        console.print(f"[yellow]Smarkets refresh échoué : {e}[/yellow]")
    finally:
        with _SMARKETS_LOCK:
            _SMARKETS_REFRESHING.discard(sport)


def fetch_smarkets_quotes(sport: str) -> list[OddQuote]:
    """Cotes Smarkets en cache. Ne bloque jamais le cycle."""
    if not SMARKETS_ENABLED or sport not in SMARKETS_SPORT_DOMAINS:
        return []
    now = time.monotonic()
    with _SMARKETS_LOCK:
        ts, quotes = _SMARKETS_CACHE.get(sport, (0.0, []))
        age = now - ts if ts else float("inf")
        launch = age > _SMARKETS_TTL and sport not in _SMARKETS_REFRESHING
        if launch:
            _SMARKETS_REFRESHING.add(sport)
    if launch:
        threading.Thread(target=_smarkets_refresh, args=(sport,), daemon=True).start()
    if age > _SMARKETS_MAX_AGE:
        return []
    return quotes


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


# ── EliteSports : balayage PROFOND, en cache de fond ────────────────────────
#
# La route globale d'EliteSports pose une fenêtre de ~2 jours : 1 480
# événements du 22 au 24/08, mesuré. La route PAR LIGUE ne la pose pas — la
# Coupe d'Allemagne y va jusqu'au 2 septembre.
#
# Balayer les 302 ligues coûte 302 appels et 55 s, et rend 255 événements
# exploitables de plus (J+0 à J+8 ; au-delà Pinnacle ne price plus, donc aucune
# ligne juste n'existe et la détection est impossible). Répartition :
#   J+1-J+2 :  78 → le creux du §9, CLV −2,70 % (n=35, non significatif)
#   J+3-J+8 : 166 → la tranche >48 h, CLV +6,38 %, significative à +3,0 σ
#
# Les deux tiers du gain tombent donc dans une zone mesurée RENTABLE.
#
# ⚠️ 55 s DANS le cycle silencieraient le sport entier — c'est très exactement
# ce qui avait fait retirer Smarkets en juillet (§5). Donc cache de fond, même
# motif que BetFirst : le cycle lit ce qui est prêt et repart aussitôt, il
# n'attend JAMAIS le balayage profond. Des cotes à J+5 ne bougent pas en trente
# secondes ; un rafraîchissement au quart d'heure suffit.
_ELITE_DEEP_TTL = float(os.getenv("ELITESPORTS_DEEP_REFRESH_SEC", "900"))
# Au-delà, on préfère RIEN à des cotes mortes — la leçon de la garde de
# fraîcheur du §5. Un flux périmé traité comme frais fabrique des value bets
# contre des prix qui n'existent plus, en silence.
_ELITE_DEEP_MAX_AGE = float(os.getenv("ELITESPORTS_DEEP_MAX_AGE_SEC", "3600"))
# Coupe-circuit : à 0, le balayage profond ne part jamais et le book reste sur
# sa route globale, comme avant le 22/08.
_ELITE_DEEP_ENABLED = os.getenv("ELITESPORTS_DEEP_ENABLED", "1").strip().lower() \
    not in ("0", "false", "no", "off", "non")
_ELITE_DEEP_CACHE: dict[str, tuple[float, list[OddQuote]]] = {}
_ELITE_DEEP_REFRESHING: set[str] = set()
_ELITE_DEEP_LOCK = threading.Lock()


def _elitesports_deep_refresh(sport: str) -> None:
    """Balaye EliteSports ligue par ligue, en fond.

    Ne lève JAMAIS : c'est un thread détaché, une exception y serait perdue et
    emporterait le drapeau de rafraîchissement, laissant le cache figé pour
    toujours sans que rien ne le dise.
    """
    try:
        from .scrapers.elitesports import leagues_seen

        quotes: list[OddQuote] = []
        with EliteSportsScraper() as es:
            ligues: dict[str, str] = {}
            for payload in es.fetch_pages(sport):
                ligues.update(leagues_seen(payload))
            echecs = 0
            for lid in ligues:
                try:
                    for payload in es.fetch_league_pages(sport, lid):
                        quotes.extend(elitesports_parse_prematch(payload, es.book))
                except Exception:                                 # noqa: BLE001
                    # Une ligue qui casse ne doit pas emporter les 301 autres.
                    echecs += 1
        with _ELITE_DEEP_LOCK:
            _ELITE_DEEP_CACHE[sport] = (time.monotonic(), quotes)
        console.print(f"\\[{sport}]   EliteSports profond : {len(quotes)} cotes "
                      f"sur {len(ligues)} ligues"
                      + (f" ({echecs} ligues en échec)" if echecs else ""))
    except Exception as e:                                        # noqa: BLE001
        console.print(f"[yellow]EliteSports profond échoué : {e}[/yellow]")
    finally:
        with _ELITE_DEEP_LOCK:
            _ELITE_DEEP_REFRESHING.discard(sport)


def _elitesports_deep_quotes(sport: str) -> list[OddQuote]:
    """Ce que le balayage profond a en réserve. Ne bloque JAMAIS le cycle."""
    if not _ELITE_DEEP_ENABLED:
        return []
    now = time.monotonic()
    with _ELITE_DEEP_LOCK:
        ts, quotes = _ELITE_DEEP_CACHE.get(sport, (0.0, []))
        age = now - ts if ts else float("inf")
        lancer = age > _ELITE_DEEP_TTL and sport not in _ELITE_DEEP_REFRESHING
        if lancer:
            _ELITE_DEEP_REFRESHING.add(sport)
    if lancer:
        threading.Thread(target=_elitesports_deep_refresh,
                         args=(sport,), daemon=True).start()
    return [] if age > _ELITE_DEEP_MAX_AGE else quotes


def fetch_elitesports_quotes(sport: str) -> list[OddQuote]:
    """Toute l'offre prématch d'EliteSports pour un sport.

    Marque blanche FM Gaming, PLATEFORME NEUVE dans le portefeuille : ni Kambi,
    ni Altenar, ni Gaming1, ni Digitain. Ses prix sont donc réellement
    indépendants — à l'inverse des jumeaux Kambi (711, Bingoal, Scooore),
    désactivés plus bas précisément parce qu'ils répètent Unibet.

    Les cotes arrivent DANS la liste des matchs, donc pas d'appel par
    événement : `size=500` est servi tel quel, soit 4 appels pour les
    1 523 matchs de football (mesuré le 22/08).

    Une page qui échoue en cours de balayage n'emporte pas celles déjà
    obtenues — même règle que la pagination de `LiveTennisScores`, et pour la
    même raison : un book à moitié collecté vaut mieux qu'un book absent.
    """
    quotes: list[OddQuote] = []
    try:
        with EliteSportsScraper() as es:
            for page_no, payload in enumerate(es.fetch_pages(sport)):
                try:
                    quotes.extend(elitesports_parse_prematch(payload, es.book))
                except Exception as e:                            # noqa: BLE001
                    console.print(f"[yellow]EliteSports page {page_no} illisible:"
                                  f"[/yellow] {e}")
    except httpx.HTTPError as e:
        # Une panne sur la première page laisse `quotes` vide et le book est
        # simplement absent du cycle ; sur une page suivante, on garde ce qui
        # a été collecté.
        console.print(f"[yellow]EliteSports skipped:[/yellow] {e}")

    # ── Fusion avec le balayage profond ──────────────────────────────────
    # La route globale est FRAÎCHE (ce cycle) mais bornée à ~J+2 ; le cache
    # profond est plus vieux (≤ 15 min) mais va jusqu'à J+8. Sur un marché
    # présent des deux côtés, **la route globale gagne** : entre deux prix,
    # celui de maintenant vaut mieux que celui d'il y a un quart d'heure, et
    # une cote périmée traitée comme fraîche fabrique des value bets contre
    # des prix qui n'existent plus (§5).
    profond = _elitesports_deep_quotes(sport)
    if profond:
        vus = {(q.event_key, q.market, q.outcome.label, q.outcome.line)
               for q in quotes}
        ajout = [q for q in profond
                 if (q.event_key, q.market, q.outcome.label, q.outcome.line) not in vus]
        if ajout:
            console.print(f"\\[{sport}]   EliteSports +{len(ajout)} cotes "
                          f"du balayage profond")
            quotes.extend(ajout)
    return quotes


def fetch_starcasinosport_quotes(sport: str) -> list[OddQuote]:
    """Bulk-fetch StarCasino Sport via the same Altenar widget."""
    try:
        with StarCasinoSportScraper() as ss:
            data = ss.fetch_events(sport)
        return list(starcasinosport_parse_get_events(data))
    except httpx.HTTPError as e:
        console.print(f"[yellow]StarCasino Sport skipped:[/yellow] {e}")
        return []


# Sports poussés par le userscript Circus. Le daemon scanne sport par sport et
# lit un fichier par sport ; ajouter un sport ici suppose de l'ajouter aussi
# dans tools/circus-ingest.user.js, sinon le fichier n'existera jamais.
# SportId Gaming1, lus dans la réponse GetSports. Ils servent à vérifier que le
# fichier poussé contient bien le sport annoncé par son nom.
CIRCUS_SPORTS = {"soccer": 844, "tennis": 848}
# Clé (sport, BetType) et non le seul BetType : un code déjà vu en football
# rendrait muet le même code en tennis, alors que c'est précisément le signal
# attendu — les deux sports ne nomment pas leurs marchés pareil.
_CIRCUS_SEEN_TYPES: set[tuple[str, str]] = set()
_CIRCUS_SEEN_OUTCOMES: set[tuple[str, str]] = set()


# MagicBetting (Digitain) : poussé par le navigateur comme Betano et Circus.
# Cloudflare y sert un défi à toute IP de datacenter — mesuré, la VM reçoit 403
# là où le navigateur reçoit 200. Le serveur d'ingestion déchiffre le payload
# avec le WebAssembly du site lui-même et dépose un JSON ordinaire.
MAGIC_SPORTS = {"soccer", "tennis"}


def fetch_magicbetting_quotes(sport: str) -> list[OddQuote]:
    """Lit le dump MagicBetting déjà déchiffré par le serveur d'ingestion.

    Silencieux tant que rien n'a été poussé : installer le pont ne doit avoir
    aucun effet de bord. La garde de fraîcheur, elle, parle — un onglet fermé
    doit se voir."""
    directory = os.getenv("MAGIC_INGEST_DIR", "data/magicbetting")
    max_age = float(os.getenv("MAGIC_MAX_AGE_MIN", "10"))
    return magic_load_pushed(
        f"{directory}/{sport}.json", max_age,
        print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
        expect_sport=sport,
    )


def fetch_circus_quotes(sport: str) -> list[OddQuote]:
    """Lit le prématch Circus poussé par le navigateur, pour un sport.

    Renvoie une liste vide tant que rien n'a été poussé : tant que le pont
    n'est pas installé, Circus est simplement absent, sans bruit dans les logs.
    La garde de fraîcheur, elle, parle — un onglet fermé doit se voir.

    Les BetType non reconnus sont signalés une fois par sport. Le tennis n'a pas
    encore été capturé, donc son code de marché « vainqueur » sortira ici au
    premier cycle : c'est ce qui permettra de l'ajouter sans deviner."""
    directory = os.getenv("CIRCUS_INGEST_DIR", "data/circus")
    max_age = float(os.getenv("CIRCUS_MAX_AGE_MIN", "30"))
    unknown: set[str] = set()
    unknown_out: set[str] = set()
    quotes = circus_load_pushed(
        f"{directory}/{sport}.json", max_age,
        print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
        unknown_types=unknown,
        unknown_outcomes=unknown_out,
        expect_sport_id=CIRCUS_SPORTS.get(sport),
    )
    new_types = {t for t in unknown if (sport, t) not in _CIRCUS_SEEN_TYPES}
    if new_types:
        _CIRCUS_SEEN_TYPES.update((sport, t) for t in new_types)
        console.print(
            f"[yellow]Circus {sport} — BetType non exploités : "
            f"{', '.join(sorted(new_types))}[/yellow]"
        )
    # Un marché correctement mappé peut perdre ses cotes sur un simple libellé
    # d'issue non traduit, et `find_value_bets` écarte alors le groupe ENTIER.
    # C'est ce qui a coûté 66 % des h2h de mi-temps de Circus jusqu'au 21/08,
    # sans une ligne dans les logs (§21.15).
    new_out = {t for t in unknown_out if (sport, t) not in _CIRCUS_SEEN_OUTCOMES}
    if new_out:
        _CIRCUS_SEEN_OUTCOMES.update((sport, t) for t in new_out)
        console.print(
            f"[yellow]Circus {sport} — noms d'issues non traduits, cotes "
            f"JETÉES : {', '.join(sorted(new_out))} — à ajouter dans "
            f"circus.py (_DRAW / _OVER / _UNDER)[/yellow]"
        )
    return quotes


def fetch_betcenter_quotes(sport: str) -> list[OddQuote]:
    """Fetch + parse the Betcenter prematch offer (Cashpoint/Merkur platform,
    own odds API on oddsservice.betcenter.be -- independent odds)."""
    try:
        with BetCenterScraper() as bc:
            return bc.fetch_all_quotes(sport)
    except httpx.HTTPError as e:
        console.print(f"[yellow]Betcenter skipped:[/yellow] {e}")
        return []


# Régulation des appels à Pinnacle
# -------------------------------
# L'API invitée limite au débit, pas à l'IP : le 403 saute d'un sport à l'autre
# selon celui qui consomme le quota — le football, avec ses 23 000 cotes, le
# vide presque à lui seul. Deux garde-fous, parce qu'un cycle de 20 s
# redemandait immédiatement après un refus, ce qui prolonge la limitation au
# lieu de la laisser retomber.
#
# 1. Recul exponentiel après un 403 ou un 429, remis à zéro au premier succès.
#    C'est le garde-fou principal : redemander toutes les 20 s pendant une
#    limitation la prolonge au lieu de la laisser retomber.
# 2. Espacement minimum entre deux appels réussis, **désactivé par défaut**
#    (PINNACLE_MIN_INTERVAL_SEC=0) : la référence est réinterrogée à chaque
#    cycle, comme les books soft. Le mettre à 60 diviserait par deux la charge
#    sur Pinnacle au prix d'une ligne de référence vieille d'un cycle — levier
#    à activer si les 403 persistent malgré le recul.
#
# Entre deux appels, les dernières cotes obtenues sont réutilisées tant
# qu'elles n'ont pas dépassé PINNACLE_MAX_REUSE_SEC. Au-delà, mieux vaut aucune
# détection qu'une détection contre une référence périmée : c'est exactement
# ainsi qu'on fabrique des value bets fantômes.
_PINNACLE_MIN_INTERVAL = float(os.getenv("PINNACLE_MIN_INTERVAL_SEC", "0"))
_PINNACLE_MAX_REUSE = float(os.getenv("PINNACLE_MAX_REUSE_SEC", "150"))
# Recul après refus — plafonné SOUS la durée de réutilisation du cache.
#
# L'ancienne échelle (60 s de départ, doublement, plafond 600 s) fabriquait
# elle-même les coupures qu'elle était censée amortir. Sur 403 répétés :
#
#   60 + 120 + 240 + 480 + 600 + 600 = 2100 s = 35 min
#
# soit exactement les deux pannes observées le 01/08 (« Coupure de 35 min,
# 60 cycles », puis 34 min). Dès le troisième refus le recul dépassait
# _PINNACLE_MAX_REUSE : le cache mourait, et plus rien n'était détecté ni
# capturé pendant une demi-heure — pour six tentatives en tout. La limitation
# de Pinnacle était retombée depuis longtemps.
#
# Le raisonnement d'origine — « redemander prolonge la limitation » — vaut
# pour un limiteur qui compte les requêtes. Celui-ci compte visiblement les
# octets : le football pèse 5 Mo compressés par cycle contre 0,15 Mo au
# tennis, et c'est lui seul qui se fait refuser. Or une réponse 403 pèse
# quelques centaines d'octets. Réessayer ne coûte donc presque rien, tandis
# que ne pas réessayer coûte des lignes de clôture, qui ne se rattrapent pas.
#
# Règle de dimensionnement à garder : PLAFOND < MAX_REUSE. Tant qu'elle
# tient, une limitation isolée est entièrement absorbée par le cache et ne
# produit aucun trou de détection — seulement une référence un peu plus
# vieille. C'est ce qui rend le recul indolore au lieu d'aveuglant.
_PINNACLE_BACKOFF_START = float(os.getenv("PINNACLE_BACKOFF_START_SEC", "30"))
_PINNACLE_BACKOFF_MAX = float(os.getenv("PINNACLE_BACKOFF_MAX_SEC", "120"))
_PINNACLE_CACHE: dict[str, tuple[float, list[OddQuote]]] = {}
# Vrai quand le dernier appel a été servi par le cache. Ces cotes sont DÉJÀ en
# base avec leur horodatage d'origine : les réinsérer les dupliquerait, et
# pinnacle_closing_group — qui prend toutes les issues du dernier instantané —
# déviguerait alors sur six cotes au lieu de trois. L'overround doublerait et
# la ligne de clôture serait fausse, sans le moindre signe extérieur.
_PINNACLE_SERVED_FROM_CACHE: dict[str, bool] = {}
# Le dernier appel a-t-il ÉCHOUÉ ? Distinct d'une réponse vide : hors-saison,
# Pinnacle répond correctement avec zéro événement — en août il n'y a pas un
# seul match de hockey. Confondre les deux faisait alerter en permanence sur
# les sports sans calendrier, ce qui remplissait le canal critique nuit après
# nuit pour une situation parfaitement normale.
_PINNACLE_FAILED: dict[str, bool] = {}
# Sports que Pinnacle ne price pas du tout — hors-saison. Les réinterroger à
# chaque cycle dépense le quota qui manque au football : quatre sports scannés
# font huit requêtes par cycle, dont quatre pour des calendriers vides.
#
# Après quelques réponses vides d'affilée, le sport n'est plus sondé que toutes
# les dix minutes. Il revient donc de lui-même dès la reprise de sa saison,
# sans qu'il faille se souvenir d'éditer SPORT_LIST en octobre.
_PINNACLE_EMPTY_STREAK: dict[str, int] = {}
_PINNACLE_IDLE_AFTER = int(os.getenv("PINNACLE_IDLE_AFTER_EMPTY", "10"))
_PINNACLE_IDLE_INTERVAL = float(os.getenv("PINNACLE_IDLE_INTERVAL_SEC", "600"))
_PINNACLE_LAST_PROBE: dict[str, float] = {}
_PINNACLE_BLOCKED_UNTIL: dict[str, float] = {}
_PINNACLE_BACKOFF: dict[str, float] = {}
_PINNACLE_LOCK = threading.Lock()
# Les sports sont scannés en parallèle : sans sérialisation, chaque cycle part
# en rafale de six requêtes Pinnacle simultanées (deux par sport, trois sports).
# Le délai interne au scraper ne sépare que les deux requêtes d'un même sport.
# Un limiteur de débit sanctionne bien plus durement une rafale que le même
# volume étalé — c'est probablement ce qui déclenchait les blocages prolongés
# du football, le plus gros des trois. Sérialiser ne change pas la fréquence
# d'interrogation, seulement sa répartition dans le cycle.
_PINNACLE_CALL_LOCK = threading.Lock()
_PINNACLE_LAST_CALL = 0.0
_PINNACLE_GAP = float(os.getenv("PINNACLE_GAP_SEC", "1.5"))


def _pinnacle_cached(sport: str) -> list[OddQuote]:
    """Dernières cotes Pinnacle si elles sont encore utilisables, sinon []."""
    hit = _PINNACLE_CACHE.get(sport)
    if not hit:
        return []
    age = time.monotonic() - hit[0]
    if age > _PINNACLE_MAX_REUSE:
        return []
    return hit[1]


def fetch_pinnacle_quotes(sport: str) -> list[OddQuote]:
    """Cotes Pinnacle, en respectant l'espacement et le recul après refus."""
    now = time.monotonic()
    with _PINNACLE_LOCK:
        blocked_until = _PINNACLE_BLOCKED_UNTIL.get(sport, 0.0)
        cached = _PINNACLE_CACHE.get(sport)
        fresh_enough = (_PINNACLE_MIN_INTERVAL > 0 and cached
                        and (now - cached[0]) < _PINNACLE_MIN_INTERVAL)

        # Sport sans calendrier, entre deux sondages — AVANT tout le reste.
        #
        # On ne demande rien dans ce cas : ni succès, ni échec, un saut
        # délibéré. Il a fallu dix réponses vides ET RÉUSSIES pour en arriver
        # là, donc ce sport n'a aucun match à clôturer et rien à perdre.
        #
        # Placé après la garde de blocage, ce test ne servait à rien : la
        # branche « encore limité » s'exécutait d'abord et remettait le drapeau
        # d'échec à vrai à chaque cycle. Une seule sonde refusée faisait alors
        # compter les minutes suivantes comme autant de cycles en panne, sans
        # qu'aucune requête ne parte — « coupure de 33 min (52 cycles) » sur un
        # sport qui n'avait rien à capturer. Observé au tennis le 01/08 à 21 h :
        # les matchs du jour sont finis ou en cours, ceux du lendemain pas
        # encore publiés, donc zéro événement prématch.
        idle = _PINNACLE_EMPTY_STREAK.get(sport, 0) >= _PINNACLE_IDLE_AFTER
        if idle and (now - _PINNACLE_LAST_PROBE.get(sport, 0.0)) < _PINNACLE_IDLE_INTERVAL:
            _PINNACLE_SERVED_FROM_CACHE[sport] = True
            _PINNACLE_FAILED[sport] = False
            return []

        if now < blocked_until:
            _PINNACLE_SERVED_FROM_CACHE[sport] = True
            _PINNACLE_FAILED[sport] = True     # toujours limité
            return _pinnacle_cached(sport)
        if fresh_enough:
            reuse = _pinnacle_cached(sport)
            # Ne servir le cache que s'il a effectivement quelque chose. Réglé
            # au-delà de PINNACLE_MAX_REUSE_SEC, l'espacement produisait sinon
            # un vide silencieux : lu plus haut comme « Pinnacle sans événement
            # (hors-saison ?) », il ne déclenchait aucune alerte et n'était
            # visible nulle part. Cache vide = on retourne chercher.
            if reuse:
                _PINNACLE_SERVED_FROM_CACHE[sport] = True
                return reuse
        # L'intervalle de sondage est écoulé : on va vraiment demander.
        if idle:
            _PINNACLE_LAST_PROBE[sport] = now

    global _PINNACLE_LAST_CALL
    try:
        with _PINNACLE_CALL_LOCK:
            gap = _PINNACLE_GAP - (time.monotonic() - _PINNACLE_LAST_CALL)
            if gap > 0:
                time.sleep(gap)
            with PinnacleScraper() as pin:
                quotes = list(pin.fetch_market_quotes(sport))
            _PINNACLE_LAST_CALL = time.monotonic()
    except (httpx.HTTPStatusError, RetryError) as raw:
        _PINNACLE_LAST_CALL = time.monotonic()
        e = _unwrap_retry(raw)
        _PINNACLE_FAILED[sport] = True
        if isinstance(e, httpx.HTTPStatusError):
            code = e.response.status_code
            # 5xx rejoint 403/429 : pendant une maintenance Pinnacle répond 503
            # à chaque appel, et réessayer à chaque cycle ne fait que brasser du
            # vide. Le recul vaut aussi pour une panne d'en face.
            if code in (403, 429) or code >= 500:
                with _PINNACLE_LOCK:
                    back = min(_PINNACLE_BACKOFF.get(sport, 0.0) * 2 or _PINNACLE_BACKOFF_START,
                               _PINNACLE_BACKOFF_MAX)
                    _PINNACLE_BACKOFF[sport] = back
                    _PINNACLE_BLOCKED_UNTIL[sport] = time.monotonic() + back
                console.print(
                    f"[yellow]Pinnacle {sport} : HTTP {code}"
                    + (" (maintenance annoncée)" if code == 503 else "")
                    + f", pause de {back:.0f}s avant nouvelle tentative[/yellow]"
                )
                _PINNACLE_SERVED_FROM_CACHE[sport] = True
                return _pinnacle_cached(sport)
        raise

    _PINNACLE_SERVED_FROM_CACHE[sport] = False
    _PINNACLE_FAILED[sport] = False
    with _PINNACLE_LOCK:
        # L'appel a abouti : la limitation est retombée, quel que soit le
        # contenu de la réponse. Remettre le recul à zéro ici et non dans la
        # seule branche « quotes non vide » — sinon un sport hors-saison, qui
        # répond correctement avec zéro événement, gardait indéfiniment le
        # recul hérité de son dernier 403 et repartait du plafond au suivant.
        _PINNACLE_BACKOFF.pop(sport, None)
        _PINNACLE_BLOCKED_UNTIL.pop(sport, None)
        if quotes:
            _PINNACLE_CACHE[sport] = (time.monotonic(), quotes)
            _PINNACLE_EMPTY_STREAK[sport] = 0
        else:
            n = _PINNACLE_EMPTY_STREAK.get(sport, 0) + 1
            _PINNACLE_EMPTY_STREAK[sport] = n
            if n == _PINNACLE_IDLE_AFTER:
                _PINNACLE_LAST_PROBE[sport] = time.monotonic()
                console.print(
                    f"[dim]Pinnacle {sport} : aucun événement depuis {n} cycles, "
                    f"sondage réduit à toutes les "
                    f"{_PINNACLE_IDLE_INTERVAL / 60:.0f} min[/dim]"
                )
    return quotes


def _unwrap_retry(exc: BaseException) -> BaseException:
    """Rendre à une RetryError l'exception qu'elle enveloppe.

    tenacity ré-emballe l'échec final dans `RetryError`, qui n'est pas une
    `HTTPStatusError`. Le tri par code HTTP juste au-dessus ne la voyait donc
    jamais : un 503 de maintenance traversait `fetch_pinnacle_quotes` sans
    poser le drapeau d'échec, ressortait de `fetch_all_parallel` en simple
    « Pinnacle skipped », et le cycle concluait « Pinnacle sans événement
    (hors-saison ?) » — sur du football, un 4 août. Aucune alerte n'est partie
    et la panne s'est vue à l'absence de value bets, pas au journal."""
    if isinstance(exc, RetryError) and exc.last_attempt is not None:
        inner = exc.last_attempt.exception()
        if inner is not None:
            return inner
    return exc


def pinnacle_fetch_failed(sport: str) -> bool:
    """Le dernier appel a-t-il échoué, par opposition à n'avoir rien renvoyé ?

    Une réponse vide est normale hors-saison. Seul un échec réel justifie une
    alerte : sinon un sport sans calendrier alerte toutes les nuits."""
    return _PINNACLE_FAILED.get(sport, False)


def pinnacle_was_cached(sport: str) -> bool:
    """Les cotes Pinnacle du dernier appel viennent-elles du cache ?

    À consulter avant de les persister : elles sont déjà en base."""
    return _PINNACLE_SERVED_FROM_CACHE.get(sport, False)


def fetch_all_parallel(
    sport: str,
    betano_file: str | None = None,
    *,
    include_file_books: bool = True,
    keep_handicaps: bool = False,
) -> list[OddQuote]:
    """Fetch Pinnacle + all soft books concurrently. Returns the merged list;
    callers split by book to route the sharp reference separately."""
    tasks: dict[str, Callable[[], list[OddQuote]]] = {
        "Pinnacle":      lambda: fetch_pinnacle_quotes(sport),
        "Unibet":        lambda: fetch_unibet_quotes(sport),
        # "711":           lambda: fetch_sevenelevenbe_quotes(sport),  # Kambi jumeau d'Unibet -> desactive (anti rate-limit)
        # "Bingoal":       lambda: fetch_bingoal_quotes(sport),  # Kambi jumeau d'Unibet -> desactive (anti rate-limit)
        # "Scooore":       lambda: fetch_scooore_quotes(sport),  # Kambi jumeau d'Unibet -> desactive (anti rate-limit)
        # MeridianBet: scraper prêt mais l'API exige un token (anti-bot
        # TrafficGuard) -> réactiver ici une fois le token capturé.
        # "MeridianBet": lambda: fetch_meridian_quotes(sport),
        # BetFirst : le 403 est tombé (vérifié le 06/08). Servi depuis un cache
        # rafraîchi EN FOND — le cycle ne l'attend jamais. Voir
        # fetch_betfirst_quotes : même paginé en parallèle il reste le plus lent
        # du portefeuille, et sa fraîcheur n'a aucune valeur puisqu'il offre les
        # pires prix (−3,20 points de CLV à sélection identique, meilleur prix
        # 15 % du temps). Il est là pour la donnée, pas pour être joué.
        "BetFirst":      lambda: fetch_betfirst_quotes(sport),
        "Ladbrokes":     lambda: fetch_ladbrokes_quotes(sport),
        "StarCasino":    lambda: fetch_starcasinosport_quotes(sport),
        "Napoleon":      lambda: fetch_napoleon_quotes(sport),
        # "Betcenter":     lambda: fetch_betcenter_quotes(sport),  # desactive: cotes erronees
        # Golden Palace : le motif « compte limité » ne concernait que le PARI.
        # Son API Altenar ne demande aucune authentification, et elle répond en
        # 1,8 s — vérifié le 06/08, 6 729 cotes sur 1 354 événements, la
        # meilleure couverture d'événements de tout le portefeuille. Jamais
        # mesuré en CLV faute de données : il l'est maintenant.
        "GoldenPalace":  lambda: fetch_goldenpalace_quotes(sport),
        # EliteSports (marque blanche FM Gaming) : PLATEFORME NEUVE, donc prix
        # indépendants — c'est tout son intérêt face aux jumeaux Kambi ci-dessus.
        # Aucune authentification, aucun anti-bot, et l'IP de datacenter est
        # acceptée : pas de pont navigateur, contrairement à Betano, Circus et
        # MagicBetting. Vérifié depuis la VM le 22/08 (§21.19).
        "EliteSports":   lambda: fetch_elitesports_quotes(sport),
    }
    # The live dump mixes every sport, so it's parsed once (on the sport that
    # owns include_file_books) to avoid duplicating it across sport threads.
    # The prematch feed is per-sport and must run every time.
    tasks["Betano"] = lambda: fetch_betano_quotes(
        betano_file=betano_file, sport=sport, include_live=include_file_books,
    )
    # Circus (Gaming1) : poussé par le navigateur comme Betano, l'ASN datacenter
    # étant refusé sur tout le domaine. Silencieux tant qu'aucun dump n'a été
    # poussé, pour qu'installer le pont soit sans effet de bord.
    if sport in CIRCUS_SPORTS:
        tasks["Circus"] = lambda: fetch_circus_quotes(sport)
    if sport in MAGIC_SPORTS:
        tasks["MagicBetting"] = lambda: fetch_magicbetting_quotes(sport)

    # Coupe-circuit par book, sans déploiement. Les motifs de désactivation
    # vivent dans les commentaires du registre ci-dessus et y restent — ceci
    # sert aux décisions du moment : un book trop lent, un book qui se met à
    # renvoyer n'importe quoi, un test A/B de couverture.
    #
    # ⚠️ Coupe la COLLECTE, donc les données. Pour ne faire taire que les
    # alertes en continuant de mesurer, c'est /book qu'il faut (§15.3).
    disabled = {
        b.strip().lower()
        for b in os.getenv("BOOKS_DISABLED", "").split(",") if b.strip()
    }
    if disabled:
        dropped = [n for n in tasks if n.lower() in disabled]
        for name in dropped:
            del tasks[name]
        if dropped:
            console.print(
                f"[dim]\\[{sport}]   books coupés par BOOKS_DISABLED : "
                f"{', '.join(dropped)}[/dim]"
            )
        # Un nom mal orthographié ne couperait rien et ne dirait rien : c'est
        # exactement le genre de réglage qu'on croit appliqué et qui ne l'est pas.
        unknown = disabled - {n.lower() for n in tasks} - {d.lower() for d in dropped}
        if unknown:
            console.print(
                f"[yellow]\\[{sport}]   BOOKS_DISABLED : nom(s) inconnu(s) "
                f"{', '.join(sorted(unknown))} — aucun book de ce nom[/yellow]"
            )

    all_quotes: list[OddQuote] = []
    with ThreadPoolExecutor(max_workers=max(1, len(tasks))) as executor:
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
    # and surebets, so parsing/storing/matching them is pure wasted CPU.
    # Filtering here lightens the whole downstream pipeline — critical on a
    # small CPU.
    #
    # ⚠️ Ce commentaire annonçait « ~45% of Pinnacle's payload ». Mesuré le
    # 20/08 (scripts/market_expansion.py) : les `spread` font 26,9 % de la
    # période 0 au football (8 876 sur 32 947) et 36,7 % au tennis (324 sur
    # 884). Le gisement reste important, mais il était surévalué de moitié.
    # Pinnacle signe chaque côté sans exception (8 876/8 876 et 324/324 en
    # lignes opposées) : la normalisation à faire est du côté des softs, pas
    # de la référence. Voir §21.13.
    if keep_handicaps:
        # Uniquement pour les sondes de mesure : la production n'en veut pas
        # tant que les conventions de signe ne sont pas normalisées. Passer par
        # ici plutôt que de refaire la collecte ailleurs — une sonde qui
        # recalcule autre chose que la production ment (§17.7).
        return all_quotes
    return [q for q in all_quotes if q.market != MarketType.HANDICAP]


