from __future__ import annotations

import os
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
from typing import Callable, Iterable

import threading
import time

import httpx
import typer
from tenacity import RetryError
from rich.console import Console
from rich.table import Table

from .config import ScanConfig, load_env_file
from .devig import devig, overround as _overround
from .live_consensus import consensus_probs, edge_pct
from .ev import ev_pct, fair_odd, kelly_fraction, kelly_stake
from .leagues import categorize as _league_category
from .matcher import (parse_event_key, reconcile_event_keys, tolerance_for,
                      wide_tolerance_for)
from .models import (Book, FairLine, HALF_TIME_MARKETS, MarketType, OddQuote,
                     Outcome, TOTALS_LIKE, ValueBet, is_half_time)
from .scrapers.betano import (
    BetanoAuthError,
    BetanoScraper,
    parse_live_scores as betano_parse_live_scores,
    parse_overview as betano_parse_overview,
    parse_prematch as betano_parse_prematch,
    prematch_file as betano_prematch_file,
)
from .scrapers.betcenter import BetCenterScraper
from .scrapers.betfirst import BetFirstScraper, parse_events_table as betfirst_parse_events_table
from .scrapers.smarkets import (
    SPORT_DOMAINS as SMARKETS_SPORT_DOMAINS,
    SmarketsScraper,
    iter_all_quotes_fast as smarkets_iter_all_quotes_fast,
)
from .scrapers.elitesports import EliteSportsScraper
from .scrapers.elitesports import parse_prematch as elitesports_parse_prematch
from .scrapers.goldenpalace import GoldenPalaceScraper, parse_get_events as goldenpalace_parse_get_events
from .scrapers.ladbrokes import LadbrokesScraper, parse_prematch as ladbrokes_parse_prematch
from .scrapers.circus import load_pushed_quotes as circus_load_pushed
from .scrapers.magicbetting import load_pushed_quotes as magic_load_pushed
from .scrapers.pinnacle import PinnacleScraper
from .scrapers.sevenelevenbe import SevenElevenScraper, parse_listview as sevenelevenbe_parse_listview
from .scrapers.bingoal import BingoalScraper, parse_listview as bingoal_parse_listview
from .scrapers.scooore import ScoooreScraper, parse_listview as scooore_parse_listview
from .scrapers.meridianbet import MeridianScraper, parse_offer as meridian_parse_offer
from .scrapers.napoleon import NapoleonScraper, parse_by_date as napoleon_parse_by_date
from .scrapers.starcasinosport import StarCasinoSportScraper, parse_get_events as starcasinosport_parse_get_events
from .scrapers.unibet import UnibetScraper, parse_listview as unibet_parse_listview
from .score_sources import provider_for as score_provider_for
from .scores import OurEvent, bind_results
from .storage import Storage
from .surebet import find_surebets, Surebet
from .middle import find_middles, Middle
from .clv import (
    aggregate as clv_aggregate,
    clv_pct,
    event_started,
    group_by as clv_group_by,
    index_quotes_by_market,
    pnl as clv_pnl,
    settle as clv_settle,
)
from .alerter import (
    TelegramConfig, send_alerts, send_surebet_alerts, send_clv_alerts,
    send_late_market_alerts, send_middle_alerts, send_system_alert,
)
from . import teams, track

# ── moteur extrait ────────────────────────────────────────────────────────
# Ces noms VIVENT désormais dans `detection.py`, mais restent importables
# depuis `src.main` : 67 fichiers de test et plusieurs scripts les prennent
# ici. Le réexport est ce qui rend l'extraction invisible pour eux.
#
# ⚠️ Réexporter n'est PAS neutre pour un test qui remplace un nom. Un
# `monkeypatch.setattr(main, "…")` sur un symbole réexporté modifierait la
# copie de `main` sans toucher celle que `detection.py` consulte réellement —
# le test passerait sans rien prouver. Aucun des noms ci-dessous n'est patché
# par la suite actuelle (vérifié), et ceux qui le sont ailleurs — les
# scrapers, les constantes Pinnacle — n'ont volontairement pas été déplacés.
from .reference import (           # noqa: F401  (réexport voulu)
    SHARP_BOOKS,
    _flip_outcome_for_swap,
    _TWIN_BOOK_GROUPS,
    _TWIN_OF,
    _TWIN_PRIMARY,
    align_reference_source,
    canonicalize_for_surebets,
    merge_twin_book_value_bets,
    remap_to_reference,
)
from .late_markets import (        # noqa: F401  (réexport voulu)
    _LATE_ALERTED,
    _LATE_ALERT_COOLDOWN,
    _LATE_MARKET_ENABLED,
    _LIVE_SCORES,
    _PINNACLE_RECENT,
    _report_late_markets,
    find_late_markets,
    forget_finished_scores,
    goals_since_last_cycle,
    late_market_edge,
    read_live_scores,
    remember_pinnacle_events,
)
from .detection import (            # noqa: F401  (réexport voulu)
    _devig_group,
    _group_quotes,
    _kickoff,
    _overround_ok,
    _thin_reference_seen,
    build_event_rows,
    build_fair_lines,
    find_value_bets,
)
from .ui import console


app = typer.Typer(add_completion=False)


@app.callback()
def _load_project_env() -> None:
    """Charger `.env` AVANT toute commande, quelle qu'elle soit.

    Le daemon reçoit sa configuration par `scan-daemon.sh`, qui source `.env`
    avant de lancer Python. Une commande tapée à la main, elle, ne reçoit rien
    et croit le projet non configuré — un réglage posé dans `.env` reste alors
    sans effet, et le symptôme n'a aucun rapport visible avec sa cause.

    Le piège s'est produit quatre fois : `doctor` annonçant « Telegram non
    configuré » sur une installation qui marchait, `/scan` n'acceptant aucun
    chat (§14.11), puis `results-update` appelant la route directe malgré
    `SCORES_FOOTBALL_BRIDGE=1` et envoyant un `Bearer` vide au tennis. Les
    trois premiers ont été corrigés un par un, à leur point d'usage ; ce
    quatrième montre que le bon endroit est ici, une fois pour toutes.

    Sans effet de bord : `load_env_file` fait un `setdefault`, donc
    l'environnement déjà posé par systemd ou par un export explicite gagne
    toujours sur le fichier.
    """
    load_env_file()




# Cycles consécutifs sans cotes Pinnacle, par sport. Pinnacle est le point de
# défaillance unique : sans lui, pas de ligne juste, donc ni value bet, ni
# surebet évalué, ni — le plus coûteux — de ligne de clôture. Ces dernières ne
# se rattrapent pas : la purge tourne à deux jours et le prix de clôture
# n'existe que dans notre propre capture. Une panne silencieuse d'une heure et
# demie a coûté exactement ça.
_PINNACLE_FAILS: dict[str, int] = {}
_PINNACLE_ALERTED: set[str] = set()
_PINNACLE_DOWN_SINCE: dict[str, float] = {}
# Seuil en MINUTES, pas en cycles. Les coupures courtes sont désormais
# attendues : le recul après 403 les absorbe seul, et alerter dessus produisait
# deux messages — alarme puis rétablissement — pour une situation que rien
# n'exige de traiter. Une nuit entière de brèves limitations remplissait le
# canal critique, ce qui apprend à l'ignorer et ruine sa raison d'être.
#
# Ce qui mérite vraiment un réveil, c'est une coupure assez longue pour faire
# perdre des lignes de clôture — irrécupérables, contrairement aux détections.
_PINNACLE_ALERT_AFTER_MIN = float(os.getenv("PINNACLE_ALERT_AFTER_MIN", "20"))


def _fmt_minutes(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60} h {m % 60:02d}" if m >= 60 else f"{m} min"


def _pinnacle_health(sport: str, *, ok: bool, tg_cfg) -> None:
    """Prévient une fois par panne, et seulement si elle dure.

    Le message de rétablissement compte autant que l'alarme : sans lui, on ne
    sait pas si le problème dure encore. Il porte la durée réelle, seule
    information qui permette de juger si des clôtures ont été perdues."""
    now = time.monotonic()

    if ok:
        if sport in _PINNACLE_ALERTED:
            down = now - _PINNACLE_DOWN_SINCE.get(sport, now)
            send_system_alert(
                tg_cfg,
                f"✅ <b>Pinnacle rétabli</b> — {sport}\n"
                f"Coupure de {_fmt_minutes(down)} "
                f"({_PINNACLE_FAILS.get(sport, 0)} cycles). Les lignes de "
                f"clôture des matchs partis pendant ce temps sont perdues.",
                print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
            )
            _PINNACLE_ALERTED.discard(sport)
        _PINNACLE_FAILS[sport] = 0
        _PINNACLE_DOWN_SINCE.pop(sport, None)
        return

    _PINNACLE_FAILS[sport] = _PINNACLE_FAILS.get(sport, 0) + 1
    _PINNACLE_DOWN_SINCE.setdefault(sport, now)
    down = now - _PINNACLE_DOWN_SINCE[sport]
    if down >= _PINNACLE_ALERT_AFTER_MIN * 60 and sport not in _PINNACLE_ALERTED:
        _PINNACLE_ALERTED.add(sport)
        send_system_alert(
            tg_cfg,
            f"🚨 <b>Pinnacle muet</b> — {sport}\n"
            f"{_fmt_minutes(down)} sans cotes ({_PINNACLE_FAILS[sport]} cycles). "
            f"Plus aucun value bet n'est détecté, et les lignes de clôture ne "
            f"sont plus capturées — ce CLV-là sera définitivement perdu.\n"
            f"À vérifier : <code>./doctor.sh</code>",
            print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
        )


# Un softbook muet ne se voit NULLE PART aujourd'hui. Le 16/08, Betano est
# resté quatre heures sans une seule cote — onglet fermé — et le seul symptôme
# était une ligne dans `valuebet.log` que personne ne lisait. Pendant ce temps
# le book continue d'exister dans les rapports, ses détections manquantes ne
# manquent à personne, et la perte est invisible.
#
# Deux seuils, et il faut les DEUX. La durée seule se déclencherait sur un seul
# cycle anormalement long — pendant une purge ils montent à 9-24 minutes
# (§18.4). Le nombre de cycles seul ne veut rien dire non plus, puisqu'un cycle
# dure de 24 s à 24 min selon le moment : cinq cycles valent deux minutes ou
# deux heures.
#
# 15 minutes, et non les 20 de Pinnacle : les books les plus rapides corrigent
# leur erreur en 4 à 6 minutes (§16.3), donc un quart d'heure d'aveuglement sur
# un book coûte déjà des occasions entières. Et contrairement à Pinnacle, dont
# la panne se voit à l'effondrement des détections, un seul book absent ne
# change rien de visible.
_BOOK_ALERT_AFTER_MIN = float(os.getenv("BOOK_ALERT_AFTER_MIN", "15"))
_BOOK_ALERT_AFTER_CYCLES = int(os.getenv("BOOK_ALERT_AFTER_CYCLES", "5"))

_BOOK_SEEN: set[tuple[str, str]] = set()
_BOOK_FAILS: dict[tuple[str, str], int] = {}
_BOOK_DOWN_SINCE: dict[tuple[str, str], float] = {}
_BOOK_ALERTED: set[tuple[str, str]] = set()


def _book_health(sport: str, quotes: Iterable[OddQuote], tg_cfg,
                 now: float | None = None) -> None:
    """Alerter quand un book qui produisait cesse de produire.

    La liste des books surveillés se construit toute seule : un couple
    (book, sport) y entre le jour où il rend sa première cote. Rien à
    configurer, et surtout rien à maintenir — un book coupé par
    `BOOKS_DISABLED`, ou qui ne couvre pas ce sport, n'y entre jamais et ne
    peut donc pas alerter à vide. C'est ce qui distingue « ce book est absent »
    de « ce book n'a jamais existé ici », les deux donnant zéro cote.
    """
    now = time.monotonic() if now is None else now
    live: Counter = Counter()
    for q in quotes:
        live[q.book.value] += 1

    for book in live:
        _BOOK_SEEN.add((book, sport))

    for key in [k for k in _BOOK_SEEN if k[1] == sport]:
        book = key[0]
        if live.get(book):
            if key in _BOOK_ALERTED:
                down = now - _BOOK_DOWN_SINCE.get(key, now)
                send_system_alert(
                    tg_cfg,
                    f"✅ <b>{book} de retour</b> — {sport}\n"
                    f"Absence de {_fmt_minutes(down)} "
                    f"({_BOOK_FAILS.get(key, 0)} cycles). Les value bets de ce "
                    f"book pendant ce temps sont perdus.",
                    print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
                )
                _BOOK_ALERTED.discard(key)
            _BOOK_FAILS[key] = 0
            _BOOK_DOWN_SINCE.pop(key, None)
            continue

        _BOOK_FAILS[key] = _BOOK_FAILS.get(key, 0) + 1
        _BOOK_DOWN_SINCE.setdefault(key, now)
        down = now - _BOOK_DOWN_SINCE[key]
        if (down >= _BOOK_ALERT_AFTER_MIN * 60
                and _BOOK_FAILS[key] >= _BOOK_ALERT_AFTER_CYCLES
                and key not in _BOOK_ALERTED):
            _BOOK_ALERTED.add(key)
            # Les trois books à pont navigateur sont la cause de loin la plus
            # fréquente, et la seule que l'utilisateur peut corriger en dix
            # secondes. Le message le dit plutôt que de laisser chercher.
            hint = ("Vérifie que l'onglet est ouvert et que le pont pousse.\n"
                    if book in ("betano_be", "circus_be", "magicbetting")
                    else "")
            send_system_alert(
                tg_cfg,
                f"🚨 <b>{book} muet</b> — {sport}\n"
                f"{_fmt_minutes(down)} sans une seule cote "
                f"({_BOOK_FAILS[key]} cycles).\n{hint}"
                f"À vérifier : <code>./doctor.sh</code>",
                print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"),
            )




def build_bet_features(
    bets: list[ValueBet],
    bet_ids: list[int],
    pinnacle_quotes: list[OddQuote],
    soft_quotes: list[OddQuote],
    fair_lines: dict[tuple[str, MarketType, float | None], FairLine],
    sport: str,
    now: datetime,
) -> list[tuple]:
    """Assembler une ligne de features par détection, prête pour bet_features.

    Toutes ces variables existent déjà en mémoire à cet instant précis du
    cycle, et étaient jetées ensuite. Aucune ne se reconstitue après coup :
    l'overround de la référence et l'âge de sa ligne disparaissent avec la
    purge des cotes, la ligue n'était nulle part, et le score d'appariement
    n'était même pas conservé jusqu'ici.

    Chacune répond à une hypothèse précise sur les faux positifs :
      - match_score     : mauvais rapprochement d'événements
      - n_books_market  : marché mince, personne d'autre ne le price
      - ref_overround   : Pinnacle lui-même hésite sur ce marché
      - ref_age_sec     : ligne de référence vieille, donc EV calculée contre
                          un prix qui a pu bouger
      - league_category : certains championnats sont structurellement pires
      - time_shift_min  : les deux sources ne parlent pas du même match
    """
    # Ligue et fraîcheur, par événement de la référence.
    league_by_ev: dict[str, str | None] = {}
    ref_fetched: dict[str, datetime] = {}
    for q in pinnacle_quotes:
        if q.league and not league_by_ev.get(q.event_key):
            league_by_ev[q.event_key] = q.league
        prev = ref_fetched.get(q.event_key)
        if prev is None or q.fetched_at > prev:
            ref_fetched[q.event_key] = q.fetched_at

    # Overround de la référence sur le marché exact du pari.
    pin_group: dict[tuple, list[float]] = defaultdict(list)
    for q in pinnacle_quotes:
        pin_group[(q.event_key, q.market, q.outcome.line)].append(q.decimal_odd)

    # Combien de books proposent ce marché — un marché que personne d'autre ne
    # price est le premier suspect quand une cote paraît trop belle.
    books_market: dict[tuple, set] = defaultdict(set)
    for q in soft_quotes:
        books_market[(q.event_key, q.market, q.outcome.line)].add(q.book)

    rows: list[tuple] = []
    for vb, vb_id in zip(bets, bet_ids):
        key = (vb.event_key, vb.market, vb.outcome.line)
        odds = pin_group.get(key) or []
        ovr = _overround(odds) if len(odds) >= 2 else None
        fl = fair_lines.get(key)
        parsed = parse_event_key(vb.event_key)
        delay_h = ((parsed[0] - vb.detected_at).total_seconds() / 3600.0
                   if parsed else None)
        fetched = ref_fetched.get(vb.event_key)
        age = (now - fetched).total_seconds() if fetched else None
        shift = None
        if vb.book_event_key:
            bp = parse_event_key(vb.book_event_key)
            if bp and parsed:
                shift = (parsed[0] - bp[0]).total_seconds() / 60.0
        league = vb.league or league_by_ev.get(vb.event_key)
        rows.append((
            vb_id, vb.detected_at.isoformat(), vb.event_key, sport,
            league, _league_category(league),
            vb.book.value, vb.market.value, vb.outcome.label, vb.outcome.line,
            vb.odd_taken, vb.fair_odd, vb.ev_pct, vb.kelly_stake_pct, delay_h,
            vb.reference_book.value if vb.reference_book else None,
            ovr, len(fl.outcomes) if fl else None, age,
            len(books_market.get(key) or ()), vb.match_score, shift,
        ))
    return rows


# Dernière cote enregistrée par (suivi, book), en mémoire. Une table le ferait
# aussi, au prix d'une écriture par book et par cycle — alors que le seul coût
# de l'oubli est un point redondant après un redémarrage, qui ne fausse aucune
# courbe.
#
# Purgé des suivis fermés : sans ça le dictionnaire ne rétrécit jamais, et à
# ~2 000 entrées par jour il finirait par peser plus que le service. Le seuil
# évite de reconstruire un ensemble à chaque cycle pour ne rien supprimer.
_CURVE_LAST: dict[tuple, float] = {}
_CURVE_LAST_MAX = 50_000


def _forget_closed_curves(live_owners: set[int]) -> int:
    """Oublie les suivis qui ne sont plus ouverts. Renvoie le nombre oublié."""
    if len(_CURVE_LAST) <= _CURVE_LAST_MAX:
        return 0
    stale = [k for k in _CURVE_LAST if k[0] not in live_owners]
    for k in stale:
        del _CURVE_LAST[k]
    return len(stale)


def track_corrections(
    open_rows: list[dict],
    soft_quotes: list[OddQuote],
    now: datetime,
    fair_lines: dict | None = None,
    pinnacle_quotes: list[OddQuote] | None = None,
    secondary_quotes: list[OddQuote] | None = None,
) -> tuple[list[tuple], list[tuple], list[tuple], list[tuple]]:
    """Confronter les suivis ouverts aux cotes du cycle.

    Renvoie (observations, corrections, alignements, historique).

    Le quatrième élément est la trajectoire : un point par cote qui a CHANGÉ
    depuis le dernier enregistré. Les jalons ne donnent que deux instants ; une
    courbe demande tous les points, et 97 à 99 % des cotes étant identiques d'un
    cycle à l'autre, n'écrire que les changements divise le volume par cinquante
    sans perdre une seule information — la série se reconstruit en propageant la
    dernière valeur connue.

    La trajectoire couvre **tous les books** qui proposent la sélection, plus
    les sources sharp — Pinnacle et Smarkets —, et pas seulement le book qui a
    déclenché la détection : un graphe du marché demande toutes les courbes, et
    le prix d'un seul book ne dit pas si c'est lui qui a bougé ou le marché
    entier.

    Smarkets étant un exchange, sa courbe se lit différemment : ses prix sont
    sans marge par construction, donc l'écart entre sa cote et `fair_odd` n'est
    pas une commission mais du bruit de liquidité. C'est précisément ce qui
    rend la comparaison intéressante.

    Pinnacle y figure comme n'importe quel autre book, avec sa cote AFFICHÉE.
    `fair_odd` porte à part la même ligne dévigée — les deux ne se confondent
    pas, l'écart entre elles étant la commission, mesurée à 6,6 % en médiane.

    Une sélection détectée sur trois books n'est enregistrée QU'UNE fois : sans
    cette déduplication, chacun des trois suivis relèverait les sept books et on
    écrirait trois copies de la même courbe.

    `fair_lines` permet d'y joindre la référence AU MÊME INSTANT. Sans elle on
    verrait le book bouger sans savoir s'il rejoint la référence ou si c'est la
    référence qui est venue à lui.

    Les deux jalons restent distincts
    qu'il ne faut surtout pas confondre :

    1. **Correction** : le book descend STRICTEMENT sous la cote détectée. Le
       prix qu'on avait n'existe plus, la fenêtre jouable est fermée. Détecter
       à 2,30 et voir 2,29 suffit — alors qu'on est encore très loin du compte.
    2. **Alignement** : le book atteint la ligne juste. La valeur a entièrement
       disparu. C'est la vraie convergence, et elle arrive bien plus tard, quand
       elle arrive.

    Le premier dit combien de temps tu as pour cliquer ; le second, à quelle
    vitesse le book apprend. Un book peut fermer la fenêtre en trente secondes
    et mettre six heures à s'aligner.

    Les suivis dont le marché n'apparaît pas dans ce cycle ne sont pas touchés :
    ne rien voir ne prouve rien — le book peut avoir retiré le marché, ou le
    scraper avoir échoué. Les compter comme « toujours pas corrigé » ferait
    passer une panne de scraper pour de la lenteur de bookmaker."""
    current: dict[tuple, float] = {}
    for q in soft_quotes:
        key = (q.event_key, q.book.value, q.market.value, q.outcome.label, q.outcome.line)
        prev = current.get(key)
        # Plusieurs cotes pour la même clé dans un cycle : garder la plus basse,
        # la plus défavorable — c'est celle qui décide si le prix a disparu.
        if prev is None or q.decimal_odd < prev:
            current[key] = q.decimal_odd

    ts = now.isoformat()
    # Toutes les cotes du cycle par sélection, tous books confondus.
    #
    # Les sources sharp (Pinnacle, Smarkets) tracent leur courbe ici comme
    # n'importe quel book, mais n'entrent JAMAIS dans `current` ci-dessus :
    # ce dictionnaire décide si le prix d'un book SOFT a disparu, et une
    # référence n'est pas un prix qu'on cherche à prendre.
    by_selection: dict[tuple, dict[str, float]] = defaultdict(dict)
    for q in (list(soft_quotes) + list(pinnacle_quotes or [])
              + list(secondary_quotes or [])):
        skey = (q.event_key, q.market.value, q.outcome.label, q.outcome.line)
        prev = by_selection[skey].get(q.book.value)
        if prev is None or q.decimal_odd < prev:
            by_selection[skey][q.book.value] = q.decimal_odd

    observed: list[tuple] = []
    corrected: list[tuple] = []
    aligned: list[tuple] = []
    history: list[tuple] = []
    # Une seule courbe par sélection, portée par le plus petit id : trois
    # détections de la même sélection ne doivent pas produire trois copies.
    curve_owner: dict[tuple, int] = {}
    for r in sorted(open_rows, key=lambda x: int(x["value_bet_id"])):
        skey = (r["event_key"], r["market"], r["outcome_label"], r["line"])
        curve_owner.setdefault(skey, int(r["value_bet_id"]))

    _forget_closed_curves(set(curve_owner.values()))

    for skey, owner in curve_owner.items():
        cur_fair = _current_fair_odd(fair_lines, {
            "event_key": skey[0], "market": skey[1],
            "outcome_label": skey[2], "line": skey[3],
        })
        for book, odd in sorted(by_selection.get(skey, {}).items()):
            ckey = (owner, book)
            last = _CURVE_LAST.get(ckey)
            if last is not None and abs(last - odd) <= 1e-9:
                continue
            _CURVE_LAST[ckey] = odd
            # L'EV n'a pas de sens pour la référence elle-même : la comparer à
            # sa propre ligne dévigée mesurerait la commission, pas un edge.
            ev = None
            if cur_fair and book != Book.PINNACLE.value:
                ev = (odd / cur_fair - 1.0) * 100.0
            history.append((owner, book, ts, odd, cur_fair, ev))

    for r in open_rows:
        key = (r["event_key"], r["book"], r["market"], r["outcome_label"], r["line"])
        odd = current.get(key)
        if odd is None:
            continue
        vid = int(r["value_bet_id"])
        observed.append((ts, odd, vid))
        try:
            det = datetime.fromisoformat(r["detected_at"])
            if det.tzinfo is None:
                det = det.replace(tzinfo=timezone.utc)
            secs = (now - det).total_seconds()
        except ValueError:
            secs = None
        if r.get("corrected_at") is None and odd < float(r["odd_taken"]):
            corrected.append((ts, secs, odd, vid))
        fair = r.get("fair_odd")
        if r.get("aligned_at") is None and fair and odd <= float(fair):
            aligned.append((ts, secs, odd, vid))
    return observed, corrected, aligned, history


def _current_fair_odd(fair_lines: dict | None, r: dict) -> float | None:
    """Cote juste de CE cycle pour la sélection suivie, ou None.

    La ligne de référence bouge elle aussi. Enregistrer celle de la détection
    ferait croire à une convergence du book alors que c'est parfois Pinnacle qui
    s'est déplacé — l'inverse de ce qu'on veut lire sur un graphe."""
    if not fair_lines:
        return None
    try:
        market = MarketType(r["market"])
    except ValueError:
        return None
    fl = fair_lines.get((r["event_key"], market, r["line"]))
    if fl is None:
        return None
    prob = fl.outcomes.get(r["outcome_label"])
    if not prob or prob <= 0.0 or prob >= 1.0:
        return None
    return 1.0 / prob



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
# _fetch_all_parallel attend TOUS les books avant de rendre la main, donc sa
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
_SMARKETS_ENABLED = os.getenv("SMARKETS_ENABLED", "0") not in ("0", "false", "False", "")
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
_SMARKETS_AS_REFERENCE = os.getenv("SMARKETS_AS_REFERENCE", "0") not in (
    "0", "false", "False", ""
)
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
    if not _SMARKETS_ENABLED or sport not in SMARKETS_SPORT_DOMAINS:
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
    poser le drapeau d'échec, ressortait de `_fetch_all_parallel` en simple
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


def _fetch_all_parallel(
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
        # Filtrer sur SHARP_BOOKS, pas sur « != Pinnacle » : une source de
        # référence n'est jamais un book où l'on chasse une erreur de prix.
        raw_soft       = [q for q in all_quotes if q.book not in SHARP_BOOKS]

        # Seconde référence sharp, servie par le cache de fond — le cycle ne
        # l'attend jamais. Repli STRICT : elle ne sert que là où Pinnacle ne
        # price rien, et n'est jamais moyennée avec lui.
        secondary = fetch_smarkets_quotes(current_sport)

        # Repli retiré par défaut depuis le 16/08 : mesuré à 0 % de CLV
        # positive sur 24 paris jugés au consensus des softbooks. Smarkets
        # reste collecté et tracé — seule la fabrication de lignes justes
        # lui est retirée.
        fair = build_fair_lines(
            quotes, cfg.devig_method,
            secondary_quotes=secondary if _SMARKETS_AS_REFERENCE else None,
        )
        _from_secondary = sum(1 for f in fair.values() if f.reference_book != Book.PINNACLE)
        _sharp_label = "Pinnacle"
        if secondary:
            # Compter ce qui SORT de la source, jamais se contenter de l'avoir
            # appelée : c'est le mode de défaillance dominant du projet (§11).
            _sharp_label = (
                f"Pinnacle + Smarkets ({len(secondary)} cotes, "
                f"{_from_secondary} lignes en repli)"
            )
        console.print(f"  → {len(fair)} fair lines (devig={cfg.devig_method}, sharp={_sharp_label})")

        # Ne pas réenregistrer un instantané servi par le cache : il est déjà
        # en base, et le dupliquer fausserait le groupe de clôture.
        if not pinnacle_was_cached(current_sport):
            storage.insert_quotes(quotes)

        sport = current_sport  # keep local var name for downstream prints

        ref_keys = {fl.event_key for fl in fair.values()}
        soft_quotes = remap_to_reference(raw_soft, ref_keys, current_sport)
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
        if not cfg.scan_surebets:
            console.print("  surebets: coupés (SCAN_SUREBETS=0)")
            surebets, plausible, flagged = [], [], []
        else:
            surebets = find_surebets(
                canonicalize_for_surebets(quotes, raw_soft, current_sport))
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
    Comma-separated --sport lets one cron entry cover every sport you care about.

    ⚠️ Cette commande reste OPÉRANTE même quand SCAN_SUREBETS=0 : la coupure
    du 21/08 porte sur le cycle automatique, pas sur une demande explicite.
    C'est le levier pour regarder ponctuellement sans rien réactiver — et,
    accessoirement, la seule façon de vérifier que le système marche encore
    avant de le remettre en service."""
    sports = [s.strip() for s in sport.split(",") if s.strip()]
    storage = Storage(ScanConfig().db_path)
    teams.init(storage)
    tg_cfg = TelegramConfig.from_env()

    if not ScanConfig().scan_surebets:
        # Dire l'état, sinon on croit le daemon en train de faire ce travail
        # alors qu'il ne le fait plus — et c'est exactement le genre d'écart
        # silencieux entre l'état supposé et l'état réel que le projet paie
        # cher (§11).
        console.print(
            "[yellow]⚠️ SCAN_SUREBETS=0 : le daemon ne calcule NI ne diffuse "
            "plus de surebets.[/yellow]\n"
            "[dim]   Cette passe manuelle tourne quand même et peut alerter. "
            "Pour remettre en service :\n"
            "   SCAN_SUREBETS=1 dans .env, puis "
            "sudo systemctl restart valuebet-daemon[/dim]")

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
        normalised_quotes = canonicalize_for_surebets(pinnacle_quotes, soft_quotes, current_sport)
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
        soft_raw   = [q for q in all_q if q.book not in SHARP_BOOKS]

        # Marchés en retard : un book qui n'a pas suspendu son prématch sur un
        # match déjà commencé. Placé avant l'analyse de value, mais après le
        # fetch : il lui faut la réponse Pinnacle de CE cycle pour savoir quels
        # matchs sont encore prématch.
        if _LATE_MARKET_ENABLED:
            try:
                # Scores en direct, football seulement : le dump live couvre tous
                # les sports mais un « score » de tennis change à chaque point.
                _goals: set[str] = set()
                if current_sport == "soccer":
                    _now_scores = read_live_scores(betano_file)
                    if _now_scores:
                        _goals = goals_since_last_cycle(_now_scores, _LIVE_SCORES)
                        _LIVE_SCORES.update(_now_scores)
                        forget_finished_scores(_LIVE_SCORES, datetime.now(timezone.utc))
                        if _goals:
                            console.print(f"\\[{current_sport}]   ⚽ buts détectés : {len(_goals)}")
                _stats: Counter = Counter()
                _late = find_late_markets(
                    pinnacle_q, soft_raw, current_sport, datetime.now(timezone.utc),
                    prior_odds=storage.odds_before, stats=_stats)
                # Sans ces compteurs, un filtre trop strict et un book sans
                # erreur donnent la même chose : rien. Le journal doit dire
                # laquelle des deux situations on observe.
                if _stats:
                    console.print(
                        f"\\[{current_sport}]   marchés en retard — "
                        + ", ".join(f"{k} {v}" for k, v in sorted(_stats.items()))
                    )
                _report_late_markets(_late, current_sport, tg_cfg, goals=_goals)
            except Exception as e:                              # noqa: BLE001
                console.print(f"[yellow]\\[{current_sport}]   late-markets skipped: {e}[/yellow]")
        remember_pinnacle_events(pinnacle_q, time.monotonic())

        if not pinnacle_q:
            failed = pinnacle_fetch_failed(current_sport)
            console.print(
                f"[yellow]\\[{current_sport}]   "
                + ("Pinnacle en échec — skipping" if failed
                   else "Pinnacle sans événement (hors-saison ?) — skipping")
                + "[/yellow]"
            )
            # Zéro événement n'est pas une panne : en août Pinnacle ne price
            # aucun match de hockey, et alerter dessus rendrait le canal
            # critique inutilisable.
            _pinnacle_health(current_sport, ok=not failed, tg_cfg=tg_cfg)
            return
        _pinnacle_health(current_sport, ok=True, tg_cfg=tg_cfg)

        cfg = ScanConfig(sport=current_sport, min_ev_pct=min_ev, bankroll=bankroll)
        # Seconde référence sharp, servie par le cache de fond — le cycle ne
        # l'attend jamais. Repli STRICT : elle ne sert que là où Pinnacle ne
        # price rien, et n'est jamais moyennée avec lui.
        secondary = fetch_smarkets_quotes(current_sport)
        if secondary:
            # AVANT de construire les lignes justes : une cote Smarkets sur un
            # match que Pinnacle price doit porter la clé de Pinnacle, sinon
            # elle fabriquerait une ligne de repli en doublon d'un marché déjà
            # couvert — et sa courbe ne rejoindrait jamais celle des autres
            # books dans odds_history.
            secondary = align_reference_source(
                secondary, {q.event_key for q in pinnacle_q}, current_sport
            )
        fair = build_fair_lines(
            pinnacle_q, cfg.devig_method,
            secondary_quotes=secondary if _SMARKETS_AS_REFERENCE else None,
        )
        if _SMARKETS_ENABLED and current_sport in SMARKETS_SPORT_DOMAINS:
            # Compter ce qui SORT de la source, jamais se contenter de l'avoir
            # appelée : c'est le mode de défaillance dominant du projet (§11).
            # La ligne s'imprime MÊME à zéro — sans quoi « pas branché » et
            # « branché mais vide » seraient indiscernables (§13.12).
            # Le repli agit au niveau du MARCHÉ (event_key, marché, ligne), pas
            # du match. Deux cas très différents se cachent donc derrière le
            # même compteur, et seul le second peut prêter à discussion :
            #   - match absent de Pinnacle       -> couverture réellement neuve
            #   - match pricé, mais pas CE marché -> ex. Pinnacle donne le 1X2
            #     et pas l'over/under, Smarkets fournit l'over/under
            # Les séparer permet de trancher sur des chiffres plutôt que sur
            # une intuition.
            _pin_events = {q.event_key for q in pinnacle_q}
            _sec_lines = [
                k for k, f in fair.items() if f.reference_book != Book.PINNACLE
            ]
            _new_match = sum(1 for k in _sec_lines if k[0] not in _pin_events)
            _new_market = len(_sec_lines) - _new_match
            console.print(
                f"\\[{current_sport}]   Smarkets : {len(secondary)} cotes, "
                f"{len(_sec_lines)} lignes de référence en repli "
                f"({_new_match} matchs absents de Pinnacle, "
                f"{_new_market} marchés manquants sur un match qu'il price)"
            )
        # Persist the event (with its sport) for every Pinnacle event in the
        # reference frame. Value bets are keyed onto these same event_keys, so
        # this lets clv-report break CLV down per sport instead of "unknown".
        # La ligue vient de Pinnacle : c'est la seule source qui la nomme pour
        # TOUS les événements de la référence. Elle était écrite vide ici, donc
        # aucune analyse par championnat n'était possible, même a posteriori.
        league_by_event: dict[str, str] = {}
        for q in (*pinnacle_q, *(secondary or [])):
            if q.league and q.event_key not in league_by_event:
                league_by_event[q.event_key] = q.league
        # ⚠️ La source est le CADRE DE RÉFÉRENCE ENTIER, pas les seuls
        # événements de Pinnacle.
        #
        # Ne prendre que `pinnacle_q` laissait sans ligne `events` tout match
        # que Pinnacle ne price pas — c'est-à-dire précisément ceux où le repli
        # secondaire se déclenche. Ces paris-là sortaient donc sans nom
        # d'équipe, sans ligue et sans heure de coup d'envoi : invérifiables,
        # inclassables par sport, et comptés quand même dans les moyennes. Vu
        # le 16/08 : une détection à +90 % affichée « None — None ».
        #
        # Les clés de `fair` sont la bonne source parce que TOUT value bet est
        # adossé à une ligne juste : partir de là garantit qu'aucun pari ne
        # peut exister sans son match. Prendre les cotes des softbooks à la
        # place remplirait la table d'événements jamais appariés.
        event_rows = build_event_rows(
            {k[0] for k in fair} | {q.event_key for q in pinnacle_q},
            current_sport, league_by_event,
        )
        storage.upsert_events(event_rows)
        # Écriture parcimonieuse : seuls les marchés qui ont bougé sont écrits.
        # 99,6 % des cotes Pinnacle sont identiques d'un cycle à l'autre, donc
        # l'essentiel de ce qu'on écrivait répétait la base. Le marché est
        # réécrit ENTIER dès qu'une issue bouge, pour que les clôtures gardent
        # un instant de capture unique — l'invariant du devig.
        _offered = _written = 0
        if not pinnacle_was_cached(current_sport):
            _offered += len(pinnacle_q)
            _written += storage.insert_quotes_sparse(pinnacle_q)

        # Persister Smarkets EST la condition pour pouvoir le juger plus tard.
        # Le prix de clôture n'existe que dans notre propre capture : Pinnacle
        # comme Smarkets retirent leurs marchés prématch au coup d'envoi (§4).
        # Sans ces lignes en base, tout pari valorisé sur Smarkets resterait
        # définitivement sans CLV — donc absent d'export-history et invisible à
        # toute analyse. Une source neuve qu'on ne peut pas mesurer ne sert à
        # rien, et l'absence ne se verrait nulle part.
        if secondary:
            _offered += len(secondary)
            _written += storage.insert_quotes_sparse(secondary)

        # Sur soft_raw et non soft_q : on demande « ce book répond-il ? », pas
        # « ses cotes s'apparient-elles ? ». Un book qui répond mais dont rien
        # ne s'apparie est un défaut de rapprochement, pas une absence — les
        # confondre enverrait chercher un onglet fermé pendant que le vrai
        # problème est ailleurs.
        _book_health(current_sport, soft_raw, tg_cfg)

        ref_keys = {fl.event_key for fl in fair.values()}
        soft_q = remap_to_reference(soft_raw, ref_keys, current_sport)

        # ⚠️ Ce que le rapprochement JETTE ne se voyait nulle part. Mesuré le
        # 18/08 : le rendement du football alternait entre 59 % et 90 % d'un
        # cycle à l'autre, soit ~14 000 cotes écartées un cycle sur deux — des
        # books qui répondent, des cotes qui arrivent, et rien qui en sorte,
        # sans une seule erreur au journal. Le mode de défaillance dominant du
        # projet (§11), appliqué cette fois à l'appariement.
        #
        # Le nombre de lignes justes est imprimé avec : un rendement qui chute
        # parce que la référence a rétréci et un rendement qui chute parce que
        # les noms ne s'apparient plus demandent deux correctifs opposés, et
        # sans ce chiffre ils sont indiscernables.
        _kept = len(soft_q)
        _raw = len(soft_raw)
        console.print(
            rf"\[{current_sport}]   rapprochement : {_kept}/{_raw} "
            f"({100 * _kept / _raw if _raw else 0:.0f} %) sur "
            # ⚠️ `ref_keys` compte des ÉVÉNEMENTS, pas des lignes justes : c'est
            # l'ensemble des event_key distinctes de `fair`, dont chacune porte
            # plusieurs lignes (h2h, et un total par ligne cotée). L'étiquette
            # « lignes justes » faisait lire 47 là où le tennis avait 47 matchs
            # et plusieurs centaines de lignes — et donc conclure à une
            # référence effondrée alors qu'elle était normale.
            f"{len(ref_keys)} événements de référence"
        )

        _offered += len(soft_q)
        _written += storage.insert_quotes_sparse(soft_q)
        # Compter ce qui est écrit ET ce qui est proposé : « compression qui
        # marche » et « plus rien ne s'écrit » donnent le même silence (§11).
        console.print(
            rf"\[{current_sport}]   cotes : {_written} écrites / {_offered} "
            f"({100 * _written / _offered if _offered else 0:.1f} %)"
        )

        # ── CLV pre-kickoff alerts ────────────────────────────────────
        if tg_cfg is not None and tg_cfg.clv_window_minutes > 0:
            now_utc = datetime.now(timezone.utc)
            # Index the DEVIGGED lines, not Pinnacle's displayed prices. The
            # displayed price carries the commission, so measuring CLV against
            # it hands every bet the whole margin for free — a bet worth
            # nothing scores +6-7%, and one the market moved against still
            # reads positive. clv-report was fixed the same way.
            pin_idx: dict[tuple, float] = {}
            for (_ek, _mkt, _ln), _fl in fair.items():
                if "::" not in _ek:
                    continue
                _d = _ek[:8]
                _t = _ek.split("::", 1)[1]
                for _label, _prob in _fl.outcomes.items():
                    if _prob > 0:
                        pin_idx[(_d, _t, _mkt.value, _label, _ln)] = 1.0 / _prob

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
        bet_ids = [storage.insert_value_bet(b) for b in bets]
        console.print(f"\\[{current_sport}]   value bets: {len(bets)} total")

        # Features permanentes. Enveloppé : c'est de la collecte annexe, le
        # pari est déjà en base et un scan ne doit jamais tomber pour ça.
        _now = datetime.now(timezone.utc)
        try:
            storage.insert_bet_features(build_bet_features(
                bets, bet_ids, pinnacle_q, soft_q, fair, current_sport, _now,
            ))
            # Ouvrir le suivi de correction, puis confronter les suivis déjà
            # ouverts aux cotes de ce cycle. Les deux dans le même try : c'est
            # de la collecte, elle ne doit jamais faire tomber un scan.
            storage.seed_corrections([
                (vid, b.detected_at.isoformat(),
                 (parse_event_key(b.event_key) or (None,))[0].isoformat()
                 if parse_event_key(b.event_key) else None,
                 b.book.value, b.event_key, b.market.value,
                 b.outcome.label, b.outcome.line, b.odd_taken, b.fair_odd)
                for b, vid in zip(bets, bet_ids)
            ])
            # Le suivi est la partie du cycle dont le coût grandit avec la
            # borne d'âge : chaque suivi ouvert est relu ET réécrit. Sans cette
            # mesure, un cycle qui s'allonge ne dirait pas d'où vient le temps.
            _t0 = time.monotonic()
            _open = [dict(r) for r in storage.open_corrections()]
            obs, corr, algn, hist = track_corrections(
                _open, soft_q, _now, fair, pinnacle_q,
                secondary_quotes=secondary,
            )
            storage.update_corrections(obs, corr, algn, hist)
            _ms = (time.monotonic() - _t0) * 1000
            console.print(
                f"\\[{current_sport}]   suivi : {len(_open)} ouverts, "
                f"{len(hist)} points, {_ms:.0f} ms"
            )
            if corr or algn:
                console.print(f"\\[{current_sport}]   fenêtres fermées : {len(corr)}, "
                              f"alignements : {len(algn)}")
        except Exception as e:                                  # noqa: BLE001
            console.print(f"[yellow]\\[{current_sport}]   features skipped: {e}[/yellow]")
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
        #
        # Coupés par défaut depuis le 21/08 — voir ScanConfig.scan_surebets.
        # La garde englobe le CALCUL, pas seulement l'envoi : la
        # canonicalisation coûte ~0,9 s par sport et n'a aucun autre usage.
        # Un « coupé » qui calcule quand même garderait le coût sans le
        # service, et le prochain profilage chercherait longtemps pourquoi.
        if not cfg.scan_surebets:
            console.print(f"\\[{current_sport}]   surebets: coupés (SCAN_SUREBETS=0)")
            surebets = []
            plausible = []
        else:
            surebet_pool = canonicalize_for_surebets(pinnacle_q, soft_raw, current_sport)
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
        #
        # Coupés par défaut depuis le 22/08 — voir ScanConfig.scan_middles.
        # Le canal n'est pas touché : les middles partaient sur le canal CLV,
        # partagé avec les alertes de CLV qu'on garde.
        if not cfg.scan_middles:
            console.print(f"\\[{current_sport}]   middles: coupés (SCAN_MIDDLES=0)")
        elif tg_cfg is not None:
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
    under systemd (`bash scripts/setup.sh`) or screen/tmux.

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


@app.command(name="alert-test-system")
def alert_test_system(
    book: str = typer.Option("elitesports", "--book", help="Nom du book simulé."),
    sport: str = typer.Option("soccer", "--sport"),
):
    """Prouver de bout en bout l'alerte « softbook muet » (§20.6, §21.9 pt 6).

    ⚠️ Cette vérification manquait depuis le 18/08, et pour une raison bête :
    `alert-test` couvre les canaux value / surebet / CLV, **jamais les alertes
    SYSTÈME**. `send_system_alert` n'avait donc aucune commande qui la
    déclenche, et la seule tentative manuelle est morte sur une config
    Telegram vide (§20.10). La logique, elle, est testée depuis toujours —
    neuf tests dans `tests/test_book_health.py`. C'est le DERNIER MAILLON, la
    livraison, qui n'était pas prouvé.

    On ne fabrique pas un message à la main : on pilote le VRAI `_book_health`
    avec une horloge avancée, pour que ce soit le code de production qui
    décide d'envoyer et qui envoie. Un test qui emprunte un autre chemin que
    la production ne prouve pas la production (§17.7).

    Trois messages doivent arriver dans le canal critique — ou le canal
    principal s'il n'est pas configuré : la mise sous surveillance est
    silencieuse, puis « muet », puis « de retour ».
    """
    cfg = TelegramConfig.from_env()
    if cfg is None:
        console.print("[yellow]TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID absents — "
                      "rien à envoyer. C'est exactement l'échec du §20.10.[/yellow]")
        raise typer.Exit(1)

    destination = cfg.effective_critical_chat_id or cfg.chat_id
    console.print(f"[bold]Destination :[/bold] "
                  f"{'canal critique' if cfg.effective_critical_chat_id else 'canal PRINCIPAL (critique non configuré)'}"
                  f" — {destination}")
    console.print(f"[dim]Seuils en vigueur : {_BOOK_ALERT_AFTER_MIN:g} min ET "
                  f"{_BOOK_ALERT_AFTER_CYCLES} cycles.[/dim]\n")

    faux = Book.PINNACLE          # objet Book quelconque, on réécrit sa valeur
    class _Faux:
        value = book
    quote = SimpleNamespace(book=_Faux())

    # État isolé : on ne touche pas aux compteurs d'un daemon qui tournerait.
    sauve = (dict(m_fails := _BOOK_FAILS), set(_BOOK_SEEN),
             dict(_BOOK_DOWN_SINCE), set(_BOOK_ALERTED))
    cle = (book, sport)
    for conteneur in (_BOOK_SEEN, _BOOK_ALERTED):
        conteneur.discard(cle)
    _BOOK_FAILS.pop(cle, None)
    _BOOK_DOWN_SINCE.pop(cle, None)

    try:
        t = 0.0
        console.print("1. le book produit → il entre sous surveillance")
        _book_health(sport, [quote], cfg, now=t)

        console.print(f"2. silence de {_BOOK_ALERT_AFTER_CYCLES + 1} cycles et "
                      f"{_BOOK_ALERT_AFTER_MIN:g} minutes → l'alerte doit partir")
        for i in range(_BOOK_ALERT_AFTER_CYCLES + 1):
            t += (_BOOK_ALERT_AFTER_MIN * 60) / _BOOK_ALERT_AFTER_CYCLES
            _book_health(sport, [], cfg, now=t)
        if cle not in _BOOK_ALERTED:
            console.print("[red]   ✗ aucune alerte déclenchée — la LOGIQUE est en "
                          "cause, pas la livraison.[/red]")
            raise typer.Exit(1)
        console.print("[green]   ✓ `_book_health` a décidé d'alerter[/green]")

        console.print("3. le book revient → l'alerte de retour doit partir")
        _book_health(sport, [quote], cfg, now=t + 60)
    finally:
        _BOOK_FAILS.clear(); _BOOK_FAILS.update(sauve[0])
        _BOOK_SEEN.clear(); _BOOK_SEEN.update(sauve[1])
        _BOOK_DOWN_SINCE.clear(); _BOOK_DOWN_SINCE.update(sauve[2])
        _BOOK_ALERTED.clear(); _BOOK_ALERTED.update(sauve[3])

    console.print("\n[bold]Va regarder Telegram.[/bold] Deux messages attendus : "
                  f"« 🚨 {book} muet » puis « ✅ {book} de retour ».")
    console.print("[dim]Si rien n'arrive, le défaut est dans la LIVRAISON — "
                  "jeton, identifiant de canal, ou bot absent du canal.[/dim]")


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
    # Premium channel sample: a big value bet (>min_premium_ev, odds in band).
    # Plus de surebet ici — le premium ne reçoit que des value bets.
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
    # Premium : grosse value uniquement (les surebets ont leur propre canal).
    premium_bet_sent = send_alerts(
        [sample_premium_bet], cfg,
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
    else:
        console.print(
            "[dim]Premium: TELEGRAM_PREMIUM_CHAT_ID non défini — canal premium désactivé.[/dim]"
        )


def _closing_prices(storage: Storage, cfg: ScanConfig, bet) -> tuple | None:
    """(raw odd, fair odd, fair prob, overround) for one bet's closing market.

    Returns None when no pre-kickoff Pinnacle quote survives for the selection,
    and a fair_odd of None when the captured market is too incomplete to devig
    — the margin is only visible in how competing outcomes over-sum, so a lone
    quote can never be devigged."""
    parsed = parse_event_key(bet["event_key"])
    if parsed is None:
        return None
    kickoff, _, _ = parsed
    # Mesurer chaque pari contre LA référence qui l'a valorisé. Un pari
    # valorisé sur Smarkets ne peut pas être mesuré contre Pinnacle : si
    # Pinnacle pricait ce marché, il n'y aurait pas eu de repli. Le laisser
    # sans clôture le rendrait invisible à `export-history`, à `clv-report` et
    # à toute analyse — une source neuve qu'on ne peut pas juger ne sert à rien.
    # NULL = Pinnacle, y compris pour tout l'historique antérieur à la colonne.
    #
    # ⚠️ Pas de try/except ici. La première version en avait un, et quand la
    # colonne n'existait pas encore sur `value_bets` il avalait le KeyError et
    # retombait sur « pinnacle » : les paris de repli cherchaient leur clôture
    # chez Pinnacle, ne la trouvaient jamais, et disparaissaient de
    # export-history comme de clv-report. Le correctif était inerte ET masquait
    # ce qu'il devait corriger. Une colonne manquante doit crier.
    ref_book = bet["reference_book"] or "pinnacle"
    group = storage.closing_group(
        event_key=bet["event_key"], market=bet["market"],
        line=bet["line"], before=kickoff, book=ref_book,
    )
    row = next((q for q in group if q["outcome_label"] == bet["outcome_label"]), None)
    if row is None:
        return None
    pinnacle_odd = float(row["decimal_odd"])

    if len(group) < 2:
        return pinnacle_odd, None, None, None
    odds = [float(q["decimal_odd"]) for q in group]
    try:
        probs = devig(odds, method=cfg.devig_method)
    except Exception:
        return pinnacle_odd, None, None, None
    prob = {q["outcome_label"]: p for q, p in zip(group, probs)}.get(bet["outcome_label"])
    if not prob or prob <= 0:
        return pinnacle_odd, None, None, None
    return pinnacle_odd, 1.0 / prob, prob, sum(1.0 / o for o in odds)


@app.command(name="close-lines")
def close_lines(sport: str = "soccer"):
    """Capture la ligne de clôture de tout value bet dont le match a commencé.

    `--sport` ne sert qu'à choisir la méthode de dévig : la sélection des paris
    ignore le sport et traite toute la file en un passage. Une liste séparée
    par des virgules est donc acceptée mais inutile — contrairement à `scan`,
    il n'y a rien à répéter par sport.

    For every detected value bet whose event has kicked off, snapshot the
    last Pinnacle price as the closing line. The closing price comes from our
    own historical capture in the quotes table — Pinnacle removes prematch
    markets from the live API at kickoff, so by the time this command runs
    the only place the real closing line still exists is in the rows scan
    persisted before kickoff. Run after kickoff (e.g. cron a few minutes
    past every hour); the closing snapshot feeds `clv-report`."""
    cfg = ScanConfig(sport=sport.split(",")[0].strip() or "soccer")
    storage = Storage(cfg.db_path)
    teams.init(storage)
    open_bets = storage.open_value_bets()
    if not open_bets:
        console.print("[bold]No open value bets to close.[/bold]")
        return

    now = datetime.now(timezone.utc)
    due = [b for b in open_bets if event_started(b["event_key"], now=now)]

    # Un pari dont le coup d'envoi précède la plus ancienne cote en base n'a
    # plus aucune trace à déviger : la purge est passée avant la capture. Le
    # retirer de la file est ce qui rend le compte-rendu lisible — sinon le
    # même échec se répète à chaque heure, et une vraie panne lui ressemble
    # trait pour trait.
    oldest = storage.oldest_quote_at()
    if oldest:
        cutoff = datetime.fromisoformat(oldest)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
        lost = [int(b["id"]) for b in due
                if (parse_event_key(b["event_key"]) or (now,))[0] < cutoff]
        if lost:
            storage.mark_closing_lost(lost)
            due = [b for b in due if int(b["id"]) not in set(lost)]
            console.print(
                f"[yellow]{len(lost)} paris retirés de la file : coup d'envoi "
                f"antérieur à la plus ancienne cote en base ({str(oldest)[:16]}), "
                f"clôture définitivement perdue.[/yellow]"
            )

    console.print(f"[bold]{len(open_bets)} open bets, {len(due)} past kickoff.[/bold]")
    if not due:
        return

    closed = 0
    missing = 0
    thin = 0
    for b in due:
        priced = _closing_prices(storage, cfg, b)
        if priced is None:
            missing += 1
            continue
        pinnacle_odd, fair_odd, fair_prob, book_overround = priced
        if fair_odd is None:
            # Better a bet with no CLV than a bet with an inflated one: an
            # incomplete closing market cannot be devigged, and falling back to
            # the raw price here is exactly the bug this command fixes.
            thin += 1
            continue

        storage.insert_clv_snapshot(
            value_bet_id=int(b["id"]),
            pinnacle_odd=pinnacle_odd,
            pinnacle_prob=1.0 / pinnacle_odd,
            snapshot_at=now,
            closing=True,
            fair_odd=fair_odd,
            fair_prob=fair_prob,
            overround=book_overround,
        )
        closed += 1
    console.print(
        f"  → {closed} closing snapshots written; {missing} bets with no "
        f"pre-kickoff Pinnacle quote on file; {thin} skipped (closing market "
        f"too incomplete to devig)"
    )


@app.command(name="backfill-fair-lines")
def backfill_fair_lines():
    """Recalculer la ligne juste des clôtures capturées avant le correctif.

    Ne récupère que ce que la rétention a laissé : les cotes Pinnacle des paris
    plus anciens ont été purgées, et leur CLV est définitivement perdu."""
    cfg = ScanConfig()
    storage = Storage(cfg.db_path)
    pending = storage.closing_snapshots_missing_fair()
    if not pending:
        console.print("[bold]Toutes les clôtures ont déjà une ligne juste.[/bold]")
        return

    # Ne pas interroger la base pour des clôtures dont les cotes sont déjà
    # purgées : sur 150 M de lignes, chaque tentative coûte cher et le
    # résultat est connu d'avance. La borne est la plus ancienne cote encore
    # stockée.
    oldest = storage.oldest_quote_at()
    recuperable, perdu = [], 0
    for row in pending:
        parsed = parse_event_key(row["event_key"])
        if oldest and parsed is not None and parsed[0].isoformat() < oldest:
            perdu += 1
        else:
            recuperable.append(row)

    console.print(
        f"[bold]{len(pending)} clôtures sans ligne juste[/bold] — {perdu} hors "
        f"rétention (cotes purgées), {len(recuperable)} à tenter."
    )
    if not recuperable:
        console.print("[yellow]Rien de récupérable : tout précède la rétention.[/yellow]")
        return

    fixed = lost = 0
    for i, row in enumerate(recuperable, 1):
        priced = _closing_prices(storage, cfg, row)
        if priced is None or priced[1] is None:
            lost += 1
        else:
            _, fair_odd, fair_prob, book_overround = priced
            storage.update_snapshot_fair(
                int(row["snapshot_id"]), fair_odd, fair_prob, book_overround
            )
            fixed += 1
        if i % 200 == 0 or i == len(recuperable):
            console.print(f"  … {i}/{len(recuperable)} — {fixed} recalculées", end="\r")

    console.print(
        f"\n[green]✓[/green] {fixed} clôtures recalculées ; {lost + perdu} "
        f"irrécupérables (cotes Pinnacle purgées)"
    )


_EV_BUCKET_ORDER = ["<5%", "5-8%", "8-15%", "15-35%", "35%+"]

_RESULT_FR = {"won": "Gagné", "lost": "Perdu", "void": "Annulé"}

TRACK_PATH = track.PATH
TRACK_HEADERS = track.HEADERS
TRACK_STAKE_EUR = track.STAKE_EUR


def _pretty_match(event_key: str) -> str:
    parsed = parse_event_key(event_key)
    if parsed is None:
        return event_key
    _, home, away = parsed
    return f"{teams.display(home)} vs {teams.display(away)}"


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
        # Lu dans .env comme SPORT_LIST ou MIN_EV : la rétention est le seul
        # levier sur la taille de la base, et elle n'était réglable qu'en
        # éditant l'unité systemd — donc en pratique jamais. Mesuré le 01/08 :
        # deux jours pèsent 23 Go de données utiles, sur un disque de 48.
        int(os.getenv("PRUNE_DAYS", "2")), "--days",
        help="Delete raw quote rows older than this many days. Défaut 2, "
        "réglable par PRUNE_DAYS dans .env. Une journée suffit dès lors que "
        "close-lines tourne toutes les heures : la ligne de clôture est "
        "capturée dans l'heure suivant le coup d'envoi, et la table grossit "
        "de dizaines de millions de lignes par jour.",
    ),
    vacuum: bool = typer.Option(True, "--vacuum/--no-vacuum",
                                help="Run VACUUM to actually shrink the file on disk."),
    max_seconds: float = typer.Option(
        1800, "--max-seconds",
        help="Budget de temps. Supprimer des dizaines de millions de lignes "
        "coûte surtout la mise à jour des index, et sur un gros retard cela "
        "dure des heures. Une purge nocturne qui déborde sur la journée est "
        "pire que le retard qu'elle rattrape : elle s'arrête au budget et "
        "reprend la nuit suivante. 0 pour ne pas borner.",
    ),
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

    # Sans retour visible, la commande reste muette plusieurs minutes et donne
    # toutes les raisons de croire qu'elle est bloquée — c'est ce qui a conduit
    # à l'interrompre en production. Un point toutes les cinq secondes suffit,
    # et vaut aussi pour la purge nocturne, dont le log était un trou noir.
    _last = [0.0]

    def _tick(rows: int, elapsed: float) -> None:
        if elapsed - _last[0] < 5.0:
            return
        _last[0] = elapsed
        rate = rows / elapsed if elapsed else 0.0
        console.print(
            f"[dim]  {rows:,} lignes supprimées en {elapsed:.0f}s "
            f"({rate:,.0f}/s)…[/dim]"
        )

    _t0 = time.monotonic()
    q = storage.prune_quotes(
        retention_days,
        max_seconds=max_seconds if max_seconds > 0 else None,
        progress=_tick,
    )
    _elapsed = time.monotonic() - _t0
    n = storage.prune_notifications()
    console.print(f"Deleted {q} quote rows (> {retention_days}d) and {n} stale dedup rows.")

    # Un arrêt sur budget doit se voir : sans ça, une purge qui n'arrive jamais
    # au bout ressemble trait pour trait à une purge qui a réussi, et la base
    # grossit pendant qu'on la croit maîtrisée.
    #
    # ⚠️ Mais il ne faut pas crier au loup dans l'autre sens. Ce message se
    # déclenchait dès qu'il RESTAIT des lignes, sans regarder la durée écoulée.
    # Or il en reste toujours : pendant qu'on purge, des lignes franchissent le
    # seuil de rétention. Une purge terminée en 3 958 s sur un budget de 10 800
    # annonçait donc « budget atteint » et faisait conclure à un échec alors
    # qu'elle était à l'équilibre. Deux situations opposées, un seul message.
    left = storage.count_quotes_older_than(retention_days)
    budget_hit = max_seconds > 0 and _elapsed >= max_seconds * 0.98
    if left and budget_hit:
        console.print(
            f"[yellow]Budget de {max_seconds:.0f}s atteint : il reste "
            f"{left:,} lignes à supprimer.[/yellow]\n"
            "[dim]  La purge suivante reprendra là où celle-ci s'arrête. "
            "Si ce nombre ne baisse pas d'une nuit à l'autre, le budget est "
            "trop court pour le rythme d'écriture.[/dim]"
        )
    elif left:
        console.print(
            f"[green]Purge terminée en {_elapsed:.0f}s "
            f"(budget {max_seconds:.0f}s).[/green]\n"
            f"[dim]  {left:,} lignes restantes : elles ont franchi le seuil de "
            f"rétention PENDANT la purge. C'est le régime normal, pas un "
            f"retard.[/dim]"
        )
    if vacuum:
        # VACUUM reconstruit la base dans une copie compactée, puis la remet en
        # place. L'espace à prévoir est donc celui de la base COMPACTÉE, pas
        # celui du fichier actuel.
        #
        # ⚠️ La première version estimait « taille du fichier × 1,1 », ce qui
        # est très au-dessus de la réalité dès que la purge a supprimé
        # l'essentiel des lignes : mesuré le 16/08, un fichier de 34 Go dont
        # les pages libres représentaient la quasi-totalité réclamait « 37,1 Go
        # libres » alors que la base compactée en fait moins d'un. Le VACUUM
        # était donc refusé exactement quand il devenait utile — et le fichier
        # restait gonflé pour toujours.
        #
        # L'estimation correcte se lit dans SQLite : (page_count - freelist)
        # × page_size donne la taille des données VIVANTES.
        import shutil as _shutil
        import sqlite3 as _sq

        free_mb = _shutil.disk_usage(os.path.dirname(os.path.abspath(db_path)) or ".").free / (1024 * 1024)
        try:
            with _sq.connect(db_path) as _c:
                _pages = _c.execute("PRAGMA page_count").fetchone()[0]
                _free = _c.execute("PRAGMA freelist_count").fetchone()[0]
                _psize = _c.execute("PRAGMA page_size").fetchone()[0]
            live_mb = max(0, _pages - _free) * _psize / (1024 * 1024)
            console.print(
                f"[dim]  données vivantes : {live_mb/1024:.2f} Go sur "
                f"{_size_mb(db_path)/1024:.1f} Go de fichier "
                f"({100*_free/max(_pages,1):.0f} % de pages libres)[/dim]"
            )
        except Exception:                                           # noqa: BLE001
            live_mb = _size_mb(db_path)      # au pire, l'ancienne hypothèse
        need_mb = live_mb * 1.3
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


def _clv_breakdown(rows: list[dict], dim: str, title: str,
                   order: list[str] | None = None) -> None:
    groups = clv_group_by(rows, dim)
    stats = {k: clv_aggregate(v) for k, v in groups.items() if v}
    if not stats:
        return
    t = Table(title=title, show_lines=False)
    t.add_column(dim)
    t.add_column("n", justify="right")
    t.add_column("CLV moy%", justify="right")
    t.add_column("médiane%", justify="right")
    t.add_column("positifs%", justify="right")
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


def _parse_report_date(raw: str, *, end_of_day: bool) -> datetime:
    """Accepte JJ/MM/AAAA, AAAA-MM-JJ et JJ/MM, en UTC.

    Trois formats parce qu'on tape ces bornes à la main, entre deux commandes,
    et qu'un format refusé au milieu d'une analyse coûte plus cher que
    dix lignes de tolérance. `--until` porte sur la FIN du jour : sinon
    `--until 17/08` exclurait le 17 août tout entier, ce qui est exactement
    l'inverse de ce qu'on croit demander.
    """
    s = raw.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d/%m"):
        try:
            d = datetime.strptime(s, fmt)
        except ValueError:
            continue
        if fmt == "%d/%m":
            d = d.replace(year=datetime.now(timezone.utc).year)
        if end_of_day:
            d = d.replace(hour=23, minute=59, second=59)
        return d.replace(tzinfo=timezone.utc)
    raise typer.BadParameter(
        f"date illisible : {raw!r}. Formats acceptés : 17/08/2026, 2026-08-17, 17/08."
    )


def _within_window(raw: str | None, lo: datetime | None,
                   hi: datetime | None) -> bool:
    """Une détection sans horodatage lisible est ÉCARTÉE d'une fenêtre.

    La garder reviendrait à la compter dans toutes les fenêtres à la fois, y
    compris celles qui ne la contiennent pas — et une moyenne calculée sur des
    paris qui n'appartiennent pas à la période ne mesure rien.
    """
    if not raw:
        return False
    try:
        t = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return False
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return (lo is None or t >= lo) and (hi is None or t <= hi)


@app.command(name="clv-report")
def clv_report(
    detected: bool = typer.Option(
        False, "--detected",
        help="Inclure aussi la population complète des détections (bruyante).",
    ),
    since: str = typer.Option(
        "", "--since", help="Ne garder que les détections à partir de cette date "
                            "(JJ/MM/AAAA, AAAA-MM-JJ, ou JJ/MM)."),
    until: str = typer.Option(
        "", "--until", help="…et jusqu'à celle-ci, incluse."),
):
    """CLV mesuré contre la ligne de clôture DÉVIGÉE.

    Deux populations, et seule la première compte pour décider : les paris que
    tu as réellement joués, et l'ensemble des détections — dont l'immense
    majorité n'a jamais été jouée.

    `--since` / `--until` bornent sur la date de DÉTECTION. C'est ce qui permet
    de répondre à la seule question qui vaille pendant une mauvaise série :
    « mon edge a-t-il bougé, ou est-ce de la variance ? » La courbe de bankroll
    ment dans les deux sens, le CLV sur la même fenêtre non.

    ⚠️ Sur une fenêtre courte les effectifs s'effondrent, et un CLV moyen sur
    quelques dizaines de paris ne distingue rien. Le taux de positives est plus
    robuste que la moyenne : le CLV a des queues épaisses, et une poignée de
    gros gagnants déplace la moyenne sans rien dire de la régularité."""
    cfg = ScanConfig()
    storage = Storage(cfg.db_path)
    teams.init(storage)
    rows = [dict(r) for r in storage.all_closed_bets()]
    if not rows:
        console.print("[bold]Aucun pari clôturé — lance `close-lines` après des coups d'envoi.[/bold]")
        return

    legacy = [r for r in rows if r.get("closing_fair_odd") is None]
    rows = [r for r in rows if r.get("closing_fair_odd")]
    if legacy:
        console.print(
            f"[yellow]{len(legacy)} paris ignorés : clôture capturée avant le "
            f"correctif, sans ligne dévigée. Ils ne reviendront pas — les cotes "
            f"Pinnacle correspondantes ont été purgées.[/yellow]"
        )
    if not rows:
        console.print(
            "[bold]Aucune clôture dévigée pour l'instant.[/bold] "
            "Relance `close-lines` : les nouvelles captures en auront une."
        )
        return

    if since or until:
        lo = _parse_report_date(since, end_of_day=False) if since else None
        hi = _parse_report_date(until, end_of_day=True) if until else None
        before = len(rows)
        rows = [r for r in rows if _within_window(r.get("detected_at"), lo, hi)]
        label = (f"depuis {lo:%d/%m/%Y}" if lo else "") + \
                (" " if lo and hi else "") + (f"jusqu'au {hi:%d/%m/%Y}" if hi else "")
        console.print(f"[cyan]Fenêtre {label} — {len(rows)} paris sur {before}.[/cyan]")
        if not rows:
            console.print("[bold]Aucun pari détecté sur cette fenêtre.[/bold]")
            return

    for r in rows:
        r["sport"] = r["sport"] or "unknown"
        r["ev_bucket"] = _ev_bucket(float(r["ev_pct"]))
        r["odd_taken"] = float(r["odd_taken"])
        # clv_aggregate consumes (taken, closing) pairs; the closing side is
        # the devigged line from here on.
        r["closing_odd"] = float(r["closing_fair_odd"])

    played = [r for r in rows if r.get("played")]

    def _headline(label: str, subset: list[dict]) -> None:
        if not subset:
            console.print(f"[bold]{label} :[/bold] aucun pari")
            return
        s = clv_aggregate([(r["odd_taken"], r["closing_odd"]) for r in subset])
        console.print(
            f"[bold]{label} :[/bold] n={s.n}  CLV moyen {s.mean_clv_pct:+.2f}%  "
            f"médiane {s.median_clv_pct:+.2f}%  positifs {s.positive_rate * 100:.1f}%"
        )

    _headline("Paris joués", played)
    _headline("Toutes détections", rows)
    console.print(
        "[dim]CLV = cote prise ÷ ligne juste à la clôture. La commission de "
        "Pinnacle est retirée des deux côtés.[/dim]\n"
    )

    target = played if played else rows
    if not played:
        console.print(
            "[yellow]Aucun pari joué encore relié à une clôture — ventilation "
            "sur les détections. Les clics sur Jouer alimenteront la vue « joués ».[/yellow]"
        )
    _clv_breakdown(target, "book", "CLV par book (joués)" if played else "CLV par book")
    _clv_breakdown(target, "market", "CLV par marché")
    _clv_breakdown(target, "sport", "CLV par sport")
    _clv_breakdown(target, "ev_bucket", "CLV par tranche d'EV", order=_EV_BUCKET_ORDER)

    if detected and played:
        console.print("\n[bold]— Population complète des détections —[/bold]")
        _clv_breakdown(rows, "ev_bucket", "CLV par tranche d'EV (tout)",
                       order=_EV_BUCKET_ORDER)


@app.command(name="features")
def features_report(
    min_ev: float = typer.Option(0.0, "--min-ev", help="Ne garder que les EV ≥ ce seuil."),
    premium: bool = typer.Option(
        False, "--premium",
        help="Restreindre au canal premium (EV≥8 & cote 1.5-4, ou EV≥20 & cote 4-6).",
    ),
    max_ev: float = typer.Option(
        40.0, "--max-ev",
        help="Plafond d'EV. Au-delà, une cote n'est pas bonne mais fausse.",
    ),
):
    """État de la collecte permanente, et CLV par championnat.

    Sert d'abord à vérifier que la collecte tourne : une variable à 0 % de
    remplissage est une panne silencieuse, elle ne se voit nulle part ailleurs.
    Les ventilations n'ont de sens qu'une fois assez de clôtures capturées."""
    storage = Storage(ScanConfig().db_path)
    rows = [dict(r) for r in storage.features_with_clv()]
    if not rows:
        console.print(
            "[bold]Aucune feature collectée.[/bold]\n"
            "[dim]Normal si le daemon n'a pas encore tourné depuis la mise à jour. "
            "Sinon, cherche « features skipped » dans valuebet.log.[/dim]"
        )
        return

    days = len({r["detected_at"][:10] for r in rows})
    console.print(
        f"[bold]{len(rows):,} détections collectées[/bold] sur {days} jour(s) — "
        f"{rows[0]['detected_at'][:10]} → {rows[-1]['detected_at'][:10]}"
    )

    # Taux de remplissage : c'est le contrôle qui compte. Une colonne vide ne
    # produit aucune erreur, seulement une analyse impossible six semaines plus
    # tard, quand il est trop tard pour recollecter.
    t = Table(title="Variables écrites à la détection", show_lines=False)
    t.add_column("variable"); t.add_column("rempli", justify="right")
    t.add_column("", justify="left")
    for col, label in (
        ("league", "ligue"), ("league_category", "catégorie"),
        ("ref_overround", "overround référence"), ("ref_age_sec", "âge de la ligne"),
        ("n_books_market", "books sur le marché"), ("match_score", "score d'appariement"),
        ("delay_h", "délai"),
    ):
        # None ou chaîne vide seulement : zéro est une valeur légitime pour un
        # âge de ligne ou un overround, le compter comme manquant ferait crier
        # à la panne sur une collecte parfaitement saine.
        n = sum(1 for r in rows if r.get(col) is not None and r.get(col) != "")
        pct = 100 * n / len(rows)
        flag = "✅" if pct >= 90 else ("⚠️ à vérifier" if pct >= 1 else "❌ rien ne remonte")
        t.add_row(label, f"{pct:5.1f} %", flag)
    console.print(t)

    # La clôture ne se juge pas comme les autres : elle est écrite des heures
    # plus tard, par close-lines, une fois le match commencé. La compter parmi
    # les variables de détection affichait « ❌ rien ne remonte » sur une
    # collecte parfaitement saine dont aucun match n'avait encore débuté — le
    # seul dénominateur qui a un sens est le nombre de matchs déjà joués.
    now_utc = datetime.now(timezone.utc)
    started, started_closed = 0, 0
    for r in rows:
        parsed = parse_event_key(r["event_key"])
        if parsed is None or parsed[0] > now_utc:
            continue
        started += 1
        if r.get("closing_fair_odd"):
            started_closed += 1
    if started == 0:
        console.print(
            "[dim]Clôture dévigée : aucun de ces matchs n'a encore commencé — "
            "rien à capturer pour l'instant, c'est attendu.[/dim]"
        )
    else:
        pct = 100 * started_closed / started
        mark = "✅" if pct >= 80 else "⚠️"
        console.print(
            f"{mark} Clôture dévigée : {started_closed}/{started} des matchs déjà "
            f"joués ont leur ligne ({pct:.0f} %)."
            + ("" if pct >= 80 else "  [yellow]Vérifie valuebet-close-lines.timer.[/yellow]")
        )

    scored = [r for r in rows if r.get("closing_fair_odd")]
    if not scored:
        console.print(
            "\n[yellow]Aucune clôture capturée pour ces détections.[/yellow]\n"
            "[dim]  Les ventilations arriveront d'elles-mêmes : close-lines tourne "
            "toutes les heures et remplit au fur et à mesure des coups d'envoi.[/dim]"
        )
        return

    for r in scored:
        r["clv"] = (float(r["odd_taken"]) / float(r["closing_fair_odd"]) - 1) * 100
    pool = [r for r in scored if float(r["ev_pct"]) <= max_ev
            and float(r["ev_pct"]) >= min_ev]
    if premium:
        pool = [r for r in pool if
                (float(r["ev_pct"]) >= 8 and 1.5 <= float(r["odd_taken"]) <= 4.0)
                or (float(r["ev_pct"]) >= 20 and 4.0 < float(r["odd_taken"]) <= 6.0)]

    # Dédoublonnage par opportunité : une même sélection est détectée sur
    # plusieurs books, ce sont des observations corrélées et on n'en joue
    # qu'une. Sans ça les effectifs gonflent d'un tiers, et toutes les
    # significativités avec.
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for r in pool:
        groups[(r["event_key"], r["market"], r["outcome_label"], r["line"])].append(r)
    dedup = []
    for g in groups.values():
        base = dict(g[0])
        base["clv"] = sum(x["clv"] for x in g) / len(g)
        base["odd_taken"] = float(base["odd_taken"])
        # _clv_breakdown repart des couples (cote prise, clôture). On lui donne
        # la clôture qui redonne exactement le CLV moyenné de la grappe, plutôt
        # que celle d'un book pris au hasard parmi les books de la grappe.
        base["closing_odd"] = base["odd_taken"] / (1 + base["clv"] / 100)
        base["league_category"] = base.get("league_category") or "autre"
        dedup.append(base)

    console.print(f"\n[bold]{len(pool)} lignes → {len(dedup)} opportunités[/bold] "
                  f"(avec clôture{', premium' if premium else ''})")
    _clv_breakdown(dedup, "league_category", "CLV par catégorie de championnat")
    top = Counter(r["league"] for r in dedup if r.get("league")).most_common(12)
    if top:
        t2 = Table(title="Ligues les plus fréquentes", show_lines=False)
        t2.add_column("ligue"); t2.add_column("n", justify="right")
        t2.add_column("CLV moy%", justify="right")
        for lg, n in top:
            v = [r["clv"] for r in dedup if r.get("league") == lg]
            t2.add_row(lg, str(n), f"{sum(v)/len(v):+.2f}")
        console.print(t2)


@app.command(name="corrections")
def corrections_report():
    """Combien de temps chaque book met-il à corriger sa cote ?

    Mesure la fenêtre pendant laquelle une alerte reste jouable, et classe les
    books du plus lent au plus réactif.

    Les pourcentages tiennent compte de la censure : un pari observé seulement
    trois minutes ne compte pas dans la colonne « 15 min », ni au numérateur ni
    au dénominateur. Sans ça, tout suivi encore jeune serait compté comme « pas
    corrigé » et ferait paraître tous les books plus lents qu'ils ne sont."""
    import statistics as _stats
    storage = Storage(ScanConfig().db_path)
    rows = [dict(r) for r in storage.corrections_report()]
    if not rows:
        console.print(
            "[bold]Aucun suivi de correction.[/bold]\n"
            "[dim]Normal tant que le daemon n'a pas tourné depuis la mise à jour.[/dim]"
        )
        return

    def _elapsed(r, field: str) -> float | None:
        """Durée d'observation utile pour un jalon : jusqu'à son franchissement,
        sinon jusqu'à la dernière fois qu'on a vu le marché."""
        if r.get(field) is not None:
            return float(r[field])
        if not r.get("observed_until"):
            return None
        try:
            a = datetime.fromisoformat(r["detected_at"])
            b = datetime.fromisoformat(r["observed_until"])
        except ValueError:
            return None
        return (b - a).total_seconds()

    watched = [r for r in rows if (r.get("observations") or 0) > 0]
    console.print(
        f"[bold]{len(rows):,} suivis ouverts[/bold], {len(watched):,} réellement "
        f"observés — "
        f"{sum(1 for r in watched if r.get('seconds_to_corr') is not None):,} "
        f"fenêtres fermées, "
        f"{sum(1 for r in watched if r.get('seconds_to_align') is not None):,} "
        f"alignements"
    )
    if not watched:
        console.print("[dim]Aucun marché revu depuis la détection — attends "
                      "quelques cycles.[/dim]")
        return

    THRESHOLDS = ((300, "5 min"), (900, "15 min"), (3600, "1 h"), (10800, "3 h"))
    # Effectif minimum sous lequel un pourcentage ne veut rien dire. Sans ce
    # garde-fou, un seul pari corrigé affichait « 100 % » à côté d'une colonne
    # « suivis » qui en annonçait trente — deux dénominateurs différents sur la
    # même ligne, et la lecture naturelle était fausse.
    MIN_N = 5

    by_book: dict[str, list[dict]] = defaultdict(list)
    for r in watched:
        by_book[r["book"]].append(r)

    def _render(field: str, title: str) -> None:
        t = Table(title=title, show_lines=False)
        t.add_column("book"); t.add_column("suivis", justify="right")
        for _, lab in THRESHOLDS:
            t.add_column(f"< {lab}", justify="right")
        t.add_column("médiane", justify="right")
        for book in sorted(by_book, key=lambda b: -(len(by_book[b]))):
            rs = by_book[book]
            cells = []
            for secs, _ in THRESHOLDS:
                # Censure : ne comptent que les paris ayant franchi le jalon
                # avant le seuil, ou observés au moins jusqu'au seuil sans
                # l'avoir franchi. Un pari détecté il y a deux minutes
                # n'apprend rien sur le seuil « 15 min ».
                elig = [r for r in rs
                        if (r.get(field) is not None and float(r[field]) <= secs)
                        or ((_elapsed(r, field) or 0) >= secs)]
                hit = [r for r in elig
                       if r.get(field) is not None and float(r[field]) <= secs]
                # Le nombre d'éligibles est affiché : c'est LUI le dénominateur.
                cells.append(f"{100 * len(hit) / len(elig):.0f} % ({len(elig)})"
                             if len(elig) >= MIN_N else f"— ({len(elig)})")
            v = [float(r[field]) for r in rs if r.get(field) is not None]
            med = _stats.median(v) if len(v) >= MIN_N else None
            t.add_row(book, str(len(rs)), *cells,
                      f"{med / 60:.0f} min" if med else "—")
        console.print(t)

    _render("seconds_to_corr",
            "1. Fenêtre jouable — le book passe sous la cote que tu as vue")
    _render("seconds_to_align",
            "2. Alignement — le book rejoint la ligne juste, la valeur a disparu")
    console.print(
        "[dim]Les deux tableaux ne disent pas la même chose. Le premier mesure le\n"
        "temps dont tu disposes pour cliquer : passer de 2,30 à 2,29 suffit à le\n"
        "déclencher, alors que la valeur est presque intacte. Le second mesure la\n"
        "vitesse à laquelle le book apprend vraiment — il arrive bien plus tard,\n"
        "quand il arrive.\n"
        f"Entre parenthèses : le nombre de paris qui renseignent la colonne ; en\n"
        f"dessous de {MIN_N}, aucun pourcentage n'est affiché.[/dim]"
    )


@app.command(name="export-tracking")
def export_tracking(
    out: str = typer.Option("data/tracking.db", "--out",
                            help="Fichier SQLite compact à produire."),
    gzip_it: bool = typer.Option(True, "--gzip/--no-gzip"),
):
    """Extraire l'historique durable dans un fichier léger, transportable.

    Pourquoi cette commande plutôt qu'une sauvegarde de la base
    ------------------------------------------------------------
    `scripts/backup-db.sh` copie la base ENTIÈRE, dont `quotes` — 20 Go de
    cotes brutes purgées tous les deux jours, qui ne servent à rien une fois
    les clôtures capturées. Compressées, cela reste plusieurs gigaoctets, et
    `push-backups.sh` les envoie vers GitHub, qui refuse tout fichier de plus
    de 100 Mo. La sauvegarde ne pouvait donc plus aboutir.

    Ici on n'exporte que ce qui ne se recalcule pas : les détections, les
    lignes de clôture, les features, les suivis de correction, les paris joués
    et les résultats. Quelques mégaoctets — transportable, versionnable, et
    analysable hors de la VM."""
    import gzip as _gzip
    import shutil as _shutil
    import sqlite3 as _sq

    # `quotes` est délibérément absente : c'est la seule table volumineuse, et
    # la seule dont le contenu soit reconstituable par un simple scan.
    TABLES = ("events", "value_bets", "clv_snapshots", "played_bets",
              "results", "bet_features", "bet_corrections", "odds_history",
              "teams")

    src = ScanConfig().db_path
    if not os.path.exists(src):
        console.print(f"[red]Base introuvable : {src}[/red]")
        raise typer.Exit(1)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    for stale in (out, out + ".gz"):
        if os.path.exists(stale):
            os.remove(stale)

    conn = _sq.connect(src)
    try:
        conn.execute("ATTACH DATABASE ? AS ex", (out,))
        copied = []
        for tbl in TABLES:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (tbl,)
            ).fetchone()
            if not exists:
                continue
            conn.execute(f"CREATE TABLE ex.{tbl} AS SELECT * FROM main.{tbl}")
            n = conn.execute(f"SELECT COUNT(*) FROM ex.{tbl}").fetchone()[0]
            copied.append((tbl, n))
        conn.commit()
        conn.execute("DETACH DATABASE ex")
    finally:
        conn.close()

    t = Table(title="Exporté", show_lines=False)
    t.add_column("table"); t.add_column("lignes", justify="right")
    for tbl, n in copied:
        t.add_row(tbl, f"{n:,}")
    console.print(t)

    size = os.path.getsize(out)
    final = out
    if gzip_it:
        with open(out, "rb") as fi, _gzip.open(out + ".gz", "wb", compresslevel=9) as fo:
            _shutil.copyfileobj(fi, fo)
        os.remove(out)
        final = out + ".gz"
        size = os.path.getsize(final)
    console.print(f"[green]✓[/green] {final} — {size / 1e6:.1f} Mo")
    if size > 90_000_000:
        console.print(
            "[yellow]⚠️  Au-delà de 90 Mo : GitHub refuse les fichiers de plus "
            "de 100 Mo. Exporte vers un autre support.[/yellow]"
        )


@app.command(name="export-curves")
def export_curves(
    out: str = typer.Option("curves.csv", "--out", help="Fichier CSV de sortie."),
    days: float = typer.Option(30.0, "--days", help="Détections des N derniers jours."),
    min_ev: float = typer.Option(0.0, "--min-ev", help="Ne garder que les EV >= ce seuil."),
    filled: bool = typer.Option(
        False, "--filled/--changes-only",
        help="Rééchantillonner à la minute en propageant la dernière valeur "
             "connue. Plus gros, mais directement traçable.",
    ),
):
    """Exporter la TRAJECTOIRE de chaque détection : un point par changement de
    cote, de la détection au coup d'envoi.

    Une ligne par point, avec l'identité du pari répétée sur chaque ligne — un
    tableur ou un notebook peuvent alors grouper par `value_bet_id` et tracer
    directement, sans jointure.

    La base ne stocke que les CHANGEMENTS (97 à 99 % des cycles répètent la même
    cote). `--filled` reconstitue la série minute par minute en propageant la
    dernière valeur connue : c'est la même information, sous la forme qu'attend
    un graphe à pas régulier.
    """
    import csv
    from datetime import timedelta as _td
    storage = Storage(ScanConfig().db_path)
    teams.init(storage)
    since = (datetime.now(timezone.utc) - _td(days=days)).isoformat()
    rows = storage.odds_curves(since=since, min_ev=min_ev)
    if not rows:
        console.print(
            "[bold]Aucune trajectoire.[/bold] La table se remplit au fil des "
            "cycles ; elle est vide tant que le daemon n'a pas tourné avec "
            "cette version."
        )
        return

    headers = [
        "value_bet_id", "Sport", "Match", "Marché", "Pari",
        "Coup d'envoi", "Détecté à", "Book détection", "Cote détection",
        "EV détection %",
        "Book", "Instant", "Minutes après détection",
        "Minutes avant coup d'envoi", "Cote", "Cote juste", "EV %",
    ]
    # Groupé par (pari, book) : chaque book est une courbe distincte, et
    # `--filled` doit propager chacune séparément.
    by_bet: dict[tuple, list] = defaultdict(list)
    for r in rows:
        by_bet[(r["value_bet_id"], r["book"])].append(r)

    n_pts = 0
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for (vid, book), pts in by_bet.items():
            head = pts[0]
            ko = parse_event_key(head["event_key"])
            kickoff = ko[0] if ko else None
            try:
                det = datetime.fromisoformat(head["detected_at"])
                if det.tzinfo is None:
                    det = det.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                continue
            series = _fill_curve(pts, kickoff) if filled else pts
            for p in series:
                try:
                    t = datetime.fromisoformat(p["seen_at"])
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue
                w.writerow([
                    vid, head["sport"] or "", _pretty_match(head["event_key"]),
                    head["market"],
                    f"{head['outcome_label']}"
                    + (f" {head['line']}" if head["line"] is not None else ""),
                    kickoff.isoformat()[:19] if kickoff else "",
                    head["detected_at"][:19],
                    head["detect_book"],
                    f"{float(head['odd_taken']):.2f}",
                    f"{float(head['ev_detect']):.2f}",
                    book,
                    p["seen_at"][:19],
                    f"{(t - det).total_seconds() / 60:.1f}",
                    f"{(kickoff - t).total_seconds() / 60:.1f}" if kickoff else "",
                    f"{float(p['odd']):.3f}",
                    f"{float(p['fair_odd']):.3f}" if p["fair_odd"] else "",
                    f"{float(p['ev_pct']):+.2f}" if p["ev_pct"] is not None else "",
                ])
                n_pts += 1
    console.print(
        f"[green]✓[/green] {len({k[0] for k in by_bet})} sélections, "
        f"{len(by_bet)} courbes, {n_pts} points → "
        f"[bold]{out}[/bold]"
    )


def _fill_curve(points: list, kickoff: datetime | None) -> list[dict]:
    """Rééchantillonne une trajectoire à la minute, en propageant la dernière
    valeur connue.

    Une cote reste valable jusqu'à son changement suivant : entre deux points,
    la valeur n'est pas inconnue, elle est constante. C'est ce qui rend
    l'écriture des seuls changements sans perte — mais un graphe à pas régulier
    veut la série développée."""
    from datetime import timedelta as _td
    out: list[dict] = []
    for i, p in enumerate(points):
        try:
            t = datetime.fromisoformat(p["seen_at"])
            if t.tzinfo is None:
                t = t.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        if i + 1 < len(points):
            try:
                nxt = datetime.fromisoformat(points[i + 1]["seen_at"])
                if nxt.tzinfo is None:
                    nxt = nxt.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                nxt = t
        else:
            # Le dernier point tient jusqu'au coup d'envoi, pas au-delà.
            nxt = kickoff if kickoff and kickoff > t else t
        cur = t
        while cur <= nxt:
            out.append({**dict(p), "seen_at": cur.isoformat()})
            cur += _td(minutes=1)
            if len(out) > 20000:      # garde-fou : un horaire faux ferait boucler
                return out
    return out


@app.command(name="export-history")
def export_history(
    out: str = typer.Option("history.csv", "--out", help="Chemin du fichier CSV de sortie."),
):
    """Exporter chaque value bet clôturé en CSV, prêt pour Excel : date, match,
    book, cotes, EV%, clôture brute ET dévigée, CLV%, et le résultat quand il
    est connu.

    Pour le suivi des paris réellement joués avec mise et P&L, c'est
    `track-update` qu'il faut : ce fichier-ci couvre toutes les détections."""
    import csv
    storage = Storage(ScanConfig().db_path)
    teams.init(storage)
    rows = [dict(r) for r in storage.all_closed_bets()]
    if not rows:
        console.print("[bold]Aucun pari clôturé — lance `close-lines` après des coups d'envoi.[/bold]")
        return

    # Oldest first so the simulated capital curve reads chronologically.
    rows.sort(key=lambda r: r.get("detected_at") or "")

    _match = _pretty_match

    headers = [
        "Date", "Coup d'envoi", "Délai (h)", "Sport", "Ligue", "Catégorie ligue",
        "Match", "Book", "Marché",
        "Pari", "Joué", "Cote prise", "Cote fair (détection)", "Référence", "EV %",
        "Clôture brute (référence)", "Clôture juste (dévigée)", "Overround clôture",
        "CLV %", "Mise % (Kelly)", "Résultat", "P&L réel", "event_key",
    ]
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            taken = float(r["odd_taken"])
            raw = float(r["closing_odd"]) if r.get("closing_odd") else 0.0
            fair_close = float(r["closing_fair_odd"]) if r.get("closing_fair_odd") else 0.0
            clv = clv_pct(taken, fair_close) * 100 if fair_close else None
            line = r.get("line")
            pari = f"{r['outcome_label']}{(' ' + str(line)) if line is not None else ''}"
            status = clv_settle(
                r["market"], r["outcome_label"], line, r.get("winner"), None, None,
            )
            # Le délai avant coup d'envoi et l'overround de la référence sont
            # les deux variables continues qui expliquent le mieux le CLV : la
            # première parce que la ligne de référence est bruitée loin du
            # match, la seconde parce qu'une grosse marge Pinnacle signale un
            # marché sur lequel Pinnacle lui-même n'est pas sûr.
            kickoff = parse_event_key(r["event_key"])
            detected = r.get("detected_at") or ""
            delai = ""
            if kickoff is not None and detected:
                try:
                    d = datetime.fromisoformat(detected)
                    if d.tzinfo is None:
                        d = d.replace(tzinfo=timezone.utc)
                    delai = f"{(kickoff[0] - d).total_seconds() / 3600:.1f}"
                except ValueError:
                    pass
            w.writerow([
                detected[:19],
                kickoff[0].isoformat()[:19] if kickoff is not None else "",
                delai,
                r.get("sport") or "",
                r.get("league") or "",
                # La ligue brute compte 141 valeurs pour 375 paris : aucune n'a
                # d'effectif exploitable (§9). La catégorie — amical, féminin,
                # jeunes, coupe, D2, top5… — est le niveau auquel une analyse a
                # une chance de conclure quelque chose.
                _league_category(r.get("league")),
                _match(r["event_key"]),
                r["book"],
                r["market"],
                pari,
                "oui" if r.get("played") else "",
                f"{taken:.2f}",
                f"{float(r['fair_odd']):.2f}",
                # Quelle source sharp a produit la fair odd ci-dessus. Sans
                # cette colonne, les paris valorisés sur une référence de repli
                # sont mélangés aux autres et leur CLV propre est indécelable.
                # Même remarque qu'en §_closing_prices : pas de garde
                # défensive. `in r.keys()` renvoyait faux tant que la colonne
                # n'existait pas, et écrivait « pinnacle » sur TOUTES les
                # lignes — y compris celles valorisées sur un repli. Le fichier
                # paraissait sain et disait le contraire de la réalité.
                r["reference_book"] or "pinnacle",
                f"{float(r['ev_pct']):.2f}",
                f"{raw:.2f}" if raw else "",
                f"{fair_close:.2f}" if fair_close else "",
                f"{float(r['closing_overround']):.4f}" if r.get("closing_overround") else "",
                f"{clv:+.2f}" if clv is not None else "",
                f"{float(r.get('kelly_pct') or 0.0):.2f}",
                _RESULT_FR.get(status, ""),
                "",   # P&L réel (mises réelles : voir track-update)
                r["event_key"],
            ])
    n_fair = sum(1 for r in rows if r.get("closing_fair_odd"))
    console.print(
        f"[green]✓[/green] {len(rows)} paris exportés vers [bold]{out}[/bold] "
        f"({n_fair} avec une clôture dévigée exploitable)"
    )



def _track_rows(storage: Storage) -> list[list]:
    """Build the tracker's rows from the database. Every played bet appears,
    settled or not — a bet with no closing line yet is still a bet, and hiding
    it would make the file look like the engine had stopped firing."""
    out: list[list] = []
    running = 0.0
    for r in storage.played_bets_with_clv():
        odd = float(r["odd_taken"]) if r["odd_taken"] else 0.0
        stake = float(r["stake"]) if r["stake"] is not None else TRACK_STAKE_EUR
        fair_close = r["closing_fair_odd"]
        clv = clv_pct(odd, float(fair_close)) * 100 if (fair_close and odd) else None
        status = clv_settle(
            r["market"] or "", r["outcome_label"] or "", r["line"],
            r["winner"], r["home_score"] if "home_score" in r.keys() else None,
            r["away_score"] if "away_score" in r.keys() else None,
        )
        bet_pnl = clv_pnl(status, odd, stake)
        if bet_pnl is not None:
            running += bet_pnl
        line = r["line"]
        out.append([
            (r["played_at"] or "")[:19],
            r["sport"] or r["event_sport"] or "",
            _pretty_match(r["event_key"] or ""),
            r["book"] or "",
            r["market"] or "",
            f"{r['outcome_label'] or ''}{(' ' + str(line)) if line is not None else ''}",
            f"{odd:.2f}" if odd else "",
            f"{float(r['fair_odd']):.2f}" if r["fair_odd"] else "",
            f"{float(r['ev_pct']):.2f}" if r["ev_pct"] is not None else "",
            f"{float(fair_close):.2f}" if fair_close else "",
            f"{clv:+.2f}" if clv is not None else "",
            f"{stake:.2f}",
            # Scores written back once known, so the regenerated file stays a
            # complete record instead of asking for them a second time.
            "" if r["home_score"] is None else f"{r['home_score']:g}",
            "" if r["away_score"] is None else f"{r['away_score']:g}",
            _RESULT_FR.get(status, ""),
            f"{bet_pnl:+.2f}" if bet_pnl is not None else "",
            f"{running:+.2f}" if bet_pnl is not None else "",
            r["event_key"] or "",
        ])
    return out


@app.command(name="backfill-played-bets")
def backfill_played_bets():
    """Rattacher les anciens clics sur Jouer à leur value bet.

    Avant le suivi, un clic n'enregistrait qu'une clé et une date. Cette clé
    vaut event_key|marché|issue|ligne, ce qui suffit à retrouver le pari
    détecté — et donc sa cote, son EV et sa ligne de clôture. Sans ça, la vue
    « paris joués » de clv-report reste vide malgré des centaines de clics."""
    cfg = ScanConfig()
    storage = Storage(cfg.db_path)
    teams.init(storage)
    pending = storage.played_bets_unlinked()
    if not pending:
        console.print("[bold]Tous les clics sont déjà rattachés.[/bold]")
        return

    linked = orphan = 0
    for row in pending:
        parts = (row["dedup_key"] or "").split("|")
        if len(parts) != 4:
            orphan += 1
            continue
        line = None if parts[3] in ("None", "") else float(parts[3])
        vb = storage.latest_value_bet_for(parts[0], parts[1], parts[2], line)
        if vb is None:
            # La détection a été purgée : le clic reste, mais plus rien à quoi
            # le rattacher.
            orphan += 1
            continue
        storage.link_played_bet(row["dedup_key"], vb, track.STAKE_EUR)
        linked += 1

    console.print(
        f"[green]✓[/green] {linked} clics rattachés à leur value bet ; "
        f"{orphan} orphelins (détection purgée)"
    )
    if linked:
        console.print("[dim]Relance `track-update` puis `clv-report`.[/dim]")


@app.command(name="track-update")
def track_update(
    out: str = typer.Option(TRACK_PATH, "--out", help="Fichier de suivi à régénérer."),
):
    """Régénérer le fichier de suivi des paris joués.

    Une ligne par clic sur Jouer, avec l'EV de départ, le CLV réel une fois la
    clôture capturée, et le P&L sur une mise fictive constante. Le fichier est
    reconstruit entièrement à chaque appel : remplis les colonnes de score,
    passe-les en base avec `settle --from`, relance cette commande."""
    storage = Storage(ScanConfig().db_path)
    teams.init(storage)
    rows = _track_rows(storage)
    if not rows:
        console.print("[bold]Aucun pari joué enregistré pour l'instant.[/bold]")
        return

    track.write_all(out, rows)

    i_clv, i_res, i_pnl, i_stake = (
        TRACK_HEADERS.index(h)
        for h in ("CLV %", "Résultat", "P&L", "Mise fictive"))
    clvs = [float(r[i_clv]) for r in rows if r[i_clv]]
    settled = [r for r in rows if r[i_res]]
    total = sum(float(r[i_pnl]) for r in settled if r[i_pnl])

    console.print(f"[green]✓[/green] {len(rows)} paris joués → [bold]{out}[/bold]")
    if clvs:
        pos = sum(1 for c in clvs if c > 0) / len(clvs) * 100
        console.print(
            f"  CLV réel : n={len(clvs)}  moyen {sum(clvs)/len(clvs):+.2f}%  "
            f"positifs {pos:.1f}%"
        )
    else:
        console.print("  [yellow]Aucune clôture dévigée encore rattachée — "
                      "lance `close-lines`.[/yellow]")
    if settled:
        # Somme des mises RÉELLEMENT enregistrées, jamais un forfait multiplié
        # par le nombre de paris : depuis le 16/08 les mises valent 35 ou 45 €
        # selon l'EV, et un dénominateur constant fausserait le ROI — le
        # premier chiffre qu'on lit, et celui sur lequel on dimensionne.
        staked = sum(float(r[i_stake]) for r in settled if r[i_stake])
        console.print(
            f"  Résultats connus : {len(settled)}/{len(rows)}  "
            f"P&L {total:+.2f}€ sur {staked:.0f}€ misés "
            f"(ROI {total / staked * 100:+.2f}%)" if staked else
            f"P&L {total:+.2f}€ — aucune mise enregistrée, ROI incalculable"
        )
    else:
        console.print(
            f"  [yellow]Aucun résultat connu — remplis « Score dom. » / "
            f"« Score ext. » puis `settle --from {out}`.[/yellow]"
        )


@app.command(name="settle")
def settle_results(
    source: str = typer.Option(..., "--from", help="CSV contenant les scores."),
):
    """Importer des résultats depuis un CSV et calculer les P&L.

    Le fichier doit porter une colonne `event_key` (ou `Match`) et soit deux
    colonnes de score, soit une colonne `winner` valant home / draw / away. Le
    fichier de suivi produit par `track-update` convient tel quel : remplis ses
    deux colonnes de score et repasse-le ici."""
    import csv
    storage = Storage(ScanConfig().db_path)
    teams.init(storage)

    by_name = {}
    for r in storage.played_bets_with_clv():
        if r["event_key"]:
            by_name.setdefault(_pretty_match(r["event_key"]).lower(), r["event_key"])

    def _num(v: str | None) -> float | None:
        if v is None or str(v).strip() == "":
            return None
        try:
            return float(str(v).strip().replace(",", "."))
        except ValueError:
            return None

    now = datetime.now(timezone.utc)
    imported = skipped = 0
    with open(source, newline="", encoding="utf-8") as f:
        for raw in csv.DictReader(f):
            row = {(k or "").strip().lower(): v for k, v in raw.items()}
            ek = (row.get("event_key") or "").strip()
            if not ek:
                ek = by_name.get((row.get("match") or "").strip().lower(), "")
            if not ek:
                skipped += 1
                continue

            home = _num(row.get("score dom.") or row.get("home_score"))
            away = _num(row.get("score ext.") or row.get("away_score"))
            winner = (row.get("winner") or "").strip().lower() or None
            if winner is None and home is not None and away is not None:
                winner = "home" if home > away else ("away" if away > home else "draw")
            if winner not in ("home", "draw", "away"):
                skipped += 1
                continue

            storage.record_result(
                event_key=ek, winner=winner, settled_at=now,
                home_score=home, away_score=away, source=Path(source).name,
            )
            imported += 1

    console.print(
        f"[green]✓[/green] {imported} résultats importés"
        + (f", {skipped} lignes sans score exploitable" if skipped else "")
    )
    if imported:
        console.print("[dim]Relance `track-update` pour recalculer les P&L.[/dim]")


def _coverage_by_league(events, bindings, league_of) -> list[tuple[str, int, int]]:
    """Par ligue : (nom, résolus, total). Trié par nombre de MANQUES.

    Sépare « la source rate un peu partout » de « la source ignore telle
    compétition ». Le second cas biaise le P&L et ne se voit pas dans le taux
    global — c'est le §13.12 appliqué à la couverture.
    """
    lies = {k for k, _ in bindings}
    par_ligue: dict[str, list[int]] = {}
    for e in events:
        ligue = league_of.get(e.event_key, "?")
        slot = par_ligue.setdefault(ligue, [0, 0])
        slot[1] += 1
        if e.event_key in lies:
            slot[0] += 1
    return sorted(((lg, r, t) for lg, (r, t) in par_ligue.items()),
                  key=lambda x: (x[1] - x[2], -x[2]))


@app.command(name="results-update")
def results_update(
    days: int = typer.Option(3, "--days", help="Fenêtre de matchs à rattraper."),
    sport: str = typer.Option("soccer,tennis", "--sport", help="Sports à traiter."),
    day: str = typer.Option(
        "", "--day",
        help="Ne juger QUE les matchs de cette journée UTC (AAAA-MM-JJ). "
             "Ignore --days. Indispensable pour mesurer une source proprement "
             "— voir la docstring."),
    dry_run: bool = typer.Option(
        False, "--dry-run",
        help="Ne rien écrire : mesure la couverture réelle des sources."),
):
    """Récupérer les résultats des matchs pariés et remplir la table `results`.

    C'est le chaînon qui manquait au P&L réel : tout le reste — `settle()`,
    `pnl()`, la jointure de `played_bets_with_clv` — existe depuis juillet et
    attendait cette écriture.

    `--dry-run` ne modifie rien et sert de SONDE : il dit quelle part de tes
    matchs les sources savent réellement résoudre, sur ton univers et non sur
    la plaquette du fournisseur. À lancer avant de croire qu'une source
    convient — c'est la règle du §15.7, celle qui a évité de mettre BetFirst en
    production avec ses 80 secondes de collecte.

    ⚠️ **Pour MESURER une source, utilise `--day`, jamais `--days`.**

    La fenêtre de `--days` va jusqu'à `maintenant - 2 h`, donc elle contient
    TOUJOURS la journée en cours — dont aucune source n'a encore le résultat,
    et dont le pont n'a pas encore déposé le fichier. Le taux affiché mélange
    alors « la source n'a pas ce match » et « ce jour n'a pas encore été
    demandé », et le compteur `journee_non_pontee` ne retombe jamais à zéro.
    Mesuré le 21/08 : un dry-run à 32 % dont la moitié du manque venait de deux
    journées absentes, pas de la source. Deux causes, un chiffre — le §13.12.

        results-update --dry-run --day 2026-08-20 --sport soccer

    `--day` borne sur une journée UTC entière et RÉVOLUE : le taux qui en sort
    ne parle que de la couverture de la source. C'est celui sur lequel on
    décide de payer un abonnement, ou de construire un pont.

    Une requête par (sport, jour) : trois jours de football et de tennis
    coûtent six appels sur les cent autorisés.
    """
    cfg = ScanConfig()
    storage = Storage(cfg.db_path)
    teams.init(storage)
    sports = [s.strip() for s in sport.split(",") if s.strip()]

    now = datetime.now(timezone.utc)
    if day:
        # Une journée UTC entière, bornes franches. Aucun rabot sur `until` :
        # la journée demandée est révolue par construction, et la rogner de
        # deux heures retirerait les matchs du soir — ceux d'Amérique du Sud,
        # justement, qui sont 7 % du flux.
        try:
            d0 = datetime.fromisoformat(day).date()
        except ValueError:
            raise typer.BadParameter(
                f"--day attend une date AAAA-MM-JJ, reçu {day!r}") from None
        since = datetime(d0.year, d0.month, d0.day, tzinfo=timezone.utc)
        until = since + timedelta(days=1)
        if since >= now:
            raise typer.BadParameter(
                f"--day {day} n'est pas une journée révolue : aucun résultat "
                "n'existe encore.")
    else:
        # Deux heures de grâce : un match qui vient de commencer n'a pas de
        # résultat, et le réclamer ne ferait que consommer du quota.
        until = now - timedelta(hours=2)
        since = now - timedelta(days=days)

    pending = storage.events_awaiting_result(since, until)
    if not pending:
        console.print("[bold]Aucun match en attente de résultat sur la fenêtre.[/bold]")
        return

    # La ligue de chacun de nos événements, gardée pour le diagnostic de
    # `--dry-run`. Un taux global ne dit pas si le manque est RÉPARTI ou
    # CONCENTRÉ, et c'est toute la différence : à 68 %, une source dont les
    # 32 % manquants tombent au hasard donne un P&L représentatif, alors
    # qu'une source qui rate systématiquement les amicaux et les troisièmes
    # divisions donne un P&L biaisé vers les grands championnats — ceux dont
    # le §21.9 dit qu'ils ne trancheraient le §20.4 que sur un sous-ensemble
    # non représentatif. Deux situations, un seul pourcentage.
    league_of = {r["event_key"]: (r["league"] or "?") for r in pending}

    def _start_of(raw: str) -> datetime | None:
        """L'heure de nos événements, toujours rendue en UTC conscient.

        Une date naïve comparée à une date consciente lève un TypeError au
        milieu du rapprochement, et l'exception emporterait le sport entier
        sans dire lequel des milliers d'événements l'a déclenchée.
        """
        try:
            dt = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    by_sport: dict[str, list[OurEvent]] = defaultdict(list)
    for r in pending:
        if r["sport"] not in sports:
            continue
        start = _start_of(r["start_time"])
        if start is None:
            continue
        # ⚠️ `events.home` ne contient PAS le nom brut : `build_event_rows` le
        # dérive de `parse_event_key`, qui rend la forme COMPACTÉE de la clé,
        # tag de classe interne compris — « bocajuniors »,
        # « tampereunitedxreserve ». L'en-tête de `scores.py` prescrit
        # pourtant les noms bruts, et cite le §15.4 où le compactage avait
        # coûté le tennis de deux books.
        #
        # Mesuré le 21/08 sur des paires réelles : le compactage coûte 3 à
        # 12 points de similarité, et fait basculer « colonsantafe » vs
        # « Colón Res. » à 80,0 — sous le seuil de 85, donc match PERDU —
        # là où le nom brut donne 87,5.
        #
        # Le registre `teams` existe exactement pour ça et il est clé sur la
        # MÊME forme compactée : il rend le nom d'origine tel qu'un scraper
        # l'a vu. On répare donc à la lecture, sans toucher à ce que le
        # daemon écrit — le correctif de fond reste ouvert (§21.16 pt 15).
        by_sport[r["sport"]].append(OurEvent(
            event_key=r["event_key"],
            home=teams.display(r["home"]) or r["home"],
            away=teams.display(r["away"]) or r["away"],
            start_time=start,
            # La ligue porte la classe (féminin, jeunes) que Pinnacle ne met
            # pas sur le nom d'équipe. L'omettre perd tout le féminin.
            league=r["league"] or "",
        ))

    # La fenêtre est dans le titre : un taux se relit des jours plus tard, et
    # « 32 % » ne veut rien dire sans savoir sur quoi il porte.
    fenetre = f"journée {day}" if day else f"{days} j"
    table = Table(title=f"Résultats récupérés ({fenetre})"
                        + (" — DRY RUN" if dry_run else ""))
    manques: dict[str, list[tuple[str, int, int]]] = {}
    apparie: dict[str, dict[str, int]] = {}
    for col in ("Sport", "À noter", "Résolus", "%", "Sans résultat", "Écartés source"):
        table.add_column(col, justify="right" if col != "Sport" else "left")

    total_written = 0
    for sp in sports:
        events = by_sport.get(sp, [])
        if not events:
            table.add_row(sp, "0", "0", "—", "0", "—")
            continue

        provider_cls = score_provider_for(sp)
        if provider_cls is None:
            console.print(f"[yellow]{sp} : aucune source de scores configurée.[/yellow]")
            continue

        # Un appel par jour couvert par la fenêtre, jamais un par match.
        days_needed = sorted({e.start_time.date() for e in events})
        fetched: list = []
        source_counters: Counter = Counter()
        try:
            with provider_cls() as provider:
                for day in days_needed:
                    res, counters = provider.fetch_with_counters(sp, day)
                    fetched.extend(res)
                    source_counters.update(counters)
        except Exception as e:                                    # noqa: BLE001
            # Une source injoignable ne doit pas emporter les autres sports :
            # le tennis et le football ont des fournisseurs distincts, et la
            # panne de l'un n'apprend rien sur l'autre.
            console.print(f"[red]{sp} : source injoignable — {e}[/red]")
            table.add_row(sp, str(len(events)), "0", "0 %", str(len(events)), "panne")
            continue

        bindings, match_counters = bind_results(events, fetched, sport=sp)
        if dry_run:
            manques[sp] = _coverage_by_league(events, bindings, league_of)
            apparie[sp] = dict(match_counters)
        if not dry_run:
            for event_key, result in bindings:
                storage.record_result(
                    event_key=event_key,
                    winner=result.winner or "",
                    settled_at=now,
                    home_score=result.home_score,
                    away_score=result.away_score,
                    source=result.source,
                )
            total_written += len(bindings)

        pct = 100.0 * len(bindings) / len(events) if events else 0.0
        table.add_row(
            sp, str(len(events)), str(len(bindings)), f"{pct:.0f} %",
            str(match_counters["sans_candidat"]),
            ", ".join(f"{k}={v}" for k, v in sorted(source_counters.items())
                      if k != "retenus") or "—",
        )

    console.print(table)
    if dry_run:
        # ⚠️ Les compteurs de l'APPARIEMENT, et pas seulement ceux de la source.
        # La colonne « Écartés source » ne montre que ce que le fournisseur a
        # écarté ; ce que le rapprochement a fait restait invisible. Deux
        # d'entre eux se lisent comme des alertes : `orientation_corrigee` dit
        # que des résultats ont été retournés (§scores, piège n°2), et
        # `classe_posee` à zéro sur une journée qui compte du féminin dit que
        # la classe n'est pas reprise — c'est très exactement l'erreur du
        # 21/08, restée invisible parce qu'aucun compteur n'était affiché.
        for sp, cs in apparie.items():
            lisibles = {k: v for k, v in sorted(cs.items())
                        if k not in ("lies", "sans_candidat") and v}
            if lisibles:
                console.print(f"[dim]{sp} — appariement : "
                              + ", ".join(f"{k}={v}" for k, v in lisibles.items())
                              + "[/dim]")
        for sp, lignes in manques.items():
            rates = [(lg, r, t) for lg, r, t in lignes if r < t]
            if not rates:
                continue
            t2 = Table(title=f"{sp} — où la source manque ({fenetre})")
            for col, just in (("Ligue", "left"), ("Résolus", "right"),
                              ("Total", "right"), ("Manquants", "right")):
                t2.add_column(col, justify=just)
            for lg, r, t in rates[:15]:
                t2.add_row(lg, str(r), str(t), str(t - r))
            console.print(t2)
            # ⚠️ Une ligue à zéro ne dit PAS que la source ignore cette
            # compétition. La première version de ce message l'affirmait, et
            # c'était faux : le 21/08, six matchs féminins étaient donnés
            # « trou de catalogue » alors que la source les servait — c'est la
            # barrière de classe du matcher qui les rejetait, la classe vivant
            # dans le nom de LIGUE chez eux et d'ÉQUIPE chez nous (§21.16).
            # Deux causes, un zéro. Le tableau les signale, il ne les tranche
            # pas — et l'inventaire du fichier, lui, les sépare.
            muettes = [lg for lg, r, t in rates if r == 0]
            if muettes:
                console.print(
                    f"[yellow]⚠️ {len(muettes)} ligue(s) où la source ne résout "
                    f"RIEN[/yellow] :\n   {', '.join(muettes[:8])}"
                    + (" …" if len(muettes) > 8 else "")
                    + "\n   [dim]Deux causes possibles, et un zéro ne les "
                      "sépare pas : la source n'a pas cette\n   compétition, "
                      "OU elle l'a et les noms ne s'apparient pas (convention "
                      "différente).\n   Vérifie dans le fichier avant de "
                      "conclure :\n   [/dim][cyan]grep -o '\"name\":\"[^\"]*\"' "
                      "data/scores/soccer/<jour>.json | sort -u[/cyan]")
            console.print(
                "[dim]Manque RÉPARTI sur beaucoup de ligues → le P&L restera "
                "représentatif.\nManque CONCENTRÉ sur quelques compétitions → "
                "le P&L penchera vers ce que la source couvre (§21.9).[/dim]")
        console.print("[dim]Sonde seule — rien n'a été écrit.[/dim]")
    else:
        console.print(f"[green]✓[/green] {total_written} résultats enregistrés. "
                      "[dim]Relance `track-update` pour recalculer les P&L.[/dim]")


@app.command(name="elitesports-check")
def elitesports_check(
    sport: str = typer.Option("soccer,tennis", "--sport"),
    pages: int = typer.Option(1, "--pages", help="Pages à récupérer par sport."),
):
    """Sonde d'acceptation d'EliteSports — APPELLE l'API réelle, n'écrit rien.

    À lancer AVANT de mettre ce book dans le cycle. C'est la règle du §15.7,
    celle qui a évité de mettre BetFirst en production avec ses 80 secondes de
    collecte : un book se juge sur ce qu'il rend chez toi, pas sur ce que sa
    capture laissait espérer.

    Ce qu'elle vérifie : que la VM est acceptée, que la pagination répond, et
    que le parseur rend des cotes des DEUX marchés attendus.
    """
    from .scrapers.elitesports import (
        EliteSportsScraper, compte_rejets, parse_prematch,
    )

    for sp in [x.strip() for x in sport.split(",") if x.strip()]:
        console.print(f"\n[bold green]══ {sp.upper()} ══[/bold green]")
        quotes, n_pages, total = [], 0, None
        rejets: Counter = Counter()
        try:
            with EliteSportsScraper() as sc:
                for payload in sc.fetch_pages(sp):
                    n_pages += 1
                    total = (payload.get("page") or {}).get("totalElements", total)
                    quotes.extend(parse_prematch(payload, sc.book))
                    rejets.update(compte_rejets(payload))
                    if n_pages >= pages:
                        break
        except Exception as e:                                    # noqa: BLE001
            console.print(f"[red]  injoignable — {type(e).__name__}: {e}[/red]")
            continue

        evs = {q.event_key for q in quotes}
        par_marche = Counter(q.market.value for q in quotes)
        lignes = sorted({q.outcome.line for q in quotes if q.outcome.line is not None})
        console.print(f"  pages lues        : {n_pages}"
                      f"   (offre annoncée : {total} événements)")
        console.print(f"  événements        : {len(evs)}")
        console.print(f"  cotes             : {len(quotes)}  {dict(par_marche)}")
        console.print(f"  lignes de totaux  : {lignes[:14]}")
        # ⚠️ L'écart entre annoncés et retenus doit être EXPLIQUÉ, pas constaté.
        ecartes = {k: v for k, v in sorted(rejets.items())
                   if k not in ("annonces", "retenus") and v}
        console.print(f"  écartés           : {sum(ecartes.values())} "
                      f"{ecartes or '(aucun)'}")
        manque = rejets.get("annonces", 0) - rejets.get("retenus", 0) - sum(ecartes.values())
        if manque:
            console.print(f"[yellow]  ⚠️ {manque} événement(s) perdus sans motif — "
                          "le parseur en laisse tomber.[/yellow]")
        if not quotes:
            console.print("[yellow]  ⚠️ zéro cote : l'API répond mais le parseur ne "
                          "reconnaît rien — la forme a changé.[/yellow]")
        elif not par_marche.get("h2h"):
            console.print("[yellow]  ⚠️ aucun 1X2 — vérifier marketExternalId=1.[/yellow]")
    console.print("\n[dim]Sonde seule — rien n'a été écrit. Le book EST dans le "
                  "cycle depuis le 22/08 : pour le couper, BOOKS_DISABLED=elitesports "
                  "(coupe la donnée)\n   ou /book elitesports (ne coupe que les "
                  "alertes, §15.3).[/dim]")


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
                    q.event_key for q in remap_to_reference(quotes, ref_events, current_sport)
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
        betano_matched = remap_to_reference(betano_raw, ref_keys, current_sport)
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

    # Sans ça, le diagnostic annonce « Telegram non configuré » sur une
    # installation qui marche — il n'a simplement pas l'environnement du daemon.
    load_env_file(project / ".env")

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
                   f"prématch uniquement, [bold]hors de ce que premium a pris[/bold]")
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
                    f"Base à {size_mb/1024:.1f} Go — lance `./runscan.sh prune` "
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
            #
            # ⚠️ Depuis l'écriture parcimonieuse, ce compte ne mesure PLUS le
            # débit d'un book mais son ACTIVITÉ — le nombre de marchés qui ont
            # bougé. Un book aux cotes stables écrit peu sans être en panne. La
            # fenêtre doit donc couvrir au moins un battement de cœur
            # (QUOTES_HEARTBEAT_SEC, 30 min par défaut), sans quoi un book sain
            # peut afficher zéro. Un zéro sur une fenêtre plus longue que le
            # battement reste, lui, un vrai signal d'alarme.
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
                        odd, ev = r["odd_taken"], r["ev_pct"]
                        # Le premium prend d'abord ; le critique ne récupère que
                        # ce qu'aucune bande premium n'accepte. Nommer lequel des
                        # deux a routé, sinon « pas d'alerte critique » et « déjà
                        # parti ailleurs » se ressemblent (§13.12).
                        premium = bool(tg.effective_premium_chat_id) and (
                            (ev >= tg.min_premium_ev_pct
                             and tg.premium_min_odd <= odd <= tg.premium_max_odd)
                            or (ev >= tg.premium_hi_min_ev
                                and tg.premium_hi_min_odd <= odd <= tg.premium_hi_max_odd)
                        )
                        if live:
                            verdict = "[yellow]live — premium et critique sont prématch only[/yellow]"
                        elif premium:
                            verdict = "[green]→ premium[/green] (pas de doublon critique)"
                        elif not tg.effective_critical_chat_id:
                            verdict = "[yellow]canal critique non configuré[/yellow]"
                        else:
                            verdict = "[green]→ critique[/green] (hors bandes premium)"
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
