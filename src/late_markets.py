"""Les marchés en retard : un book qui cote encore un match déjà commencé.

Extrait de `main.py` sans changement de comportement. Ce module est le SEUL du
projet qui raisonne déjà après le coup d'envoi — c'est lui qui appelle
`live_consensus`, et c'est donc la pièce la plus proche du futur moteur LIVE.
Le sortir maintenant, avant d'écrire ce moteur, évite d'avoir à l'en extraire
plus tard alors qu'il sera devenu un point de passage des deux.

⚠️ Ce n'est PAS un moteur live, et le confondre avec un serait une erreur
coûteuse. Ce qu'il détecte, c'est l'OUBLI d'un book : une cote prématch restée
ouverte alors que le match a commencé. La référence reste la ligne prématch de
Pinnacle plus un consensus de books ; il n'existe encore aucune source sharp
après le coup d'envoi, et c'est exactement le verrou que la phase LIVE devra
lever.

L'état de déduplication vit ici, en mémoire du processus : `_LATE_ALERTED`
(un envoi par match/book, puis silence pendant le délai), `_PINNACLE_RECENT`
et `_LIVE_SCORES`. Trois dictionnaires de module — donc trois choses qui ne
survivent pas à un redémarrage et que deux daemons simultanés ne partageraient
pas. C'est documenté au §21.22, ce n'est pas corrigé ici.
"""
from __future__ import annotations

import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Callable

from .alerter import send_late_market_alerts
from .live_consensus import consensus_probs, edge_pct
from .matcher import parse_event_key, reconcile_event_keys, tolerance_for
from .models import Book, OddQuote
from .scrapers.betano import parse_live_scores as betano_parse_live_scores
from .ui import console


# Événements que Pinnacle a pricés en prématch, et quand. Sert uniquement au
# détecteur de marché en retard : c'est la DISPARITION d'un événement de ce
# flux, alors que son coup d'envoi est passé, qui prouve qu'il a commencé.
# Purgé au-delà de six heures — passé ce délai un match est terminé, et le
# dictionnaire n'a pas vocation à grossir.
_PINNACLE_RECENT: dict[str, float] = {}
_PINNACLE_RECENT_TTL = 6 * 3600.0

# Minutes après le coup d'envoi au-delà desquelles un marché encore ouvert
# devient suspect. Dix minutes laissent passer les décalages d'horaire
# habituels (coup d'envoi retardé, arrondi de programmation) sans noyer le
# canal : en dessous, on signalerait surtout des matchs qui n'ont pas encore
# vraiment commencé.
_LATE_MARKET_MIN_MIN = float(os.getenv("LATE_MARKET_MIN_MINUTES", "10"))
_LATE_MARKET_MAX_MIN = float(os.getenv("LATE_MARKET_MAX_MINUTES", "75"))
# Coupe-circuit : le détecteur peut se tromper en masse sans se tromper en
# silence, et un canal critique noyé ne sert plus à rien. Mieux vaut pouvoir
# l'éteindre depuis .env, sans déploiement, que de subir une nuit d'alertes.
_LATE_MARKET_ENABLED = os.getenv("LATE_MARKET_ENABLED", "1") == "1"
# Écart minimal contre le consensus live pour qu'un marché figé mérite une
# alerte. Élevé à dessein : la référence est une moyenne de books soft, dont les
# marges live tournent à 8-12 %. On cherche des marchés qu'un but a déjà
# tranchés, pas des edges à 3 % — ceux-là seraient du bruit de mesure.
_LATE_MARKET_MIN_EDGE = float(os.getenv("LATE_MARKET_MIN_EDGE", "15.0"))


def remember_pinnacle_events(pinnacle_quotes: list[OddQuote], now: float) -> None:
    """Mémoriser les événements pricés en prématch, et oublier les vieux."""
    for q in pinnacle_quotes:
        _PINNACLE_RECENT[q.event_key] = now
    for k in [k for k, t in _PINNACLE_RECENT.items() if now - t > _PINNACLE_RECENT_TTL]:
        del _PINNACLE_RECENT[k]


def find_late_markets(
    pinnacle_quotes: list[OddQuote],
    soft_raw: list[OddQuote],
    sport: str,
    now: datetime,
    *,
    prior_odds: "Callable[[str, Book, datetime], dict]",
    recent: dict[str, float] | None = None,
    stats: "Counter | None" = None,
) -> dict[tuple[str, Book], list[OddQuote]]:
    """Books qui proposent encore un marché PRÉMATCH sur un match commencé.

    Le scénario : un match a débuté il y a vingt minutes, il est 1-1, et le
    book n'a pas suspendu son marché « les deux équipes marquent ». Le pari est
    déjà gagné au moment où on le prend. Ce n'est pas un value bet — c'est une
    erreur d'exploitation du book.

    Comment on sait qu'un match a commencé
    --------------------------------------
    Le scraper Pinnacle ignore délibérément les matchs en cours (`isLive`) : un
    événement DISPARAÎT donc de son flux au coup d'envoi. C'est cette
    disparition, et non l'heure affichée, qui fait foi — une heure de coup
    d'envoi seule ne distingue pas un match commencé d'un match reporté.

    D'où les trois conditions cumulées :
      1. Pinnacle a pricé cet événement récemment (il existe, et il le connaît) ;
      2. il ne le price plus maintenant (il est passé en direct) ;
      3. son coup d'envoi est dépassé d'au moins _LATE_MARKET_MIN_MIN.

    Pourquoi la présence dans le flux ne suffit pas
    ----------------------------------------------
    Première version : « le book expose encore ce match, donc il a oublié de
    suspendre ». Faux, et coûteux — le canal a été noyé. La plupart des books
    belges continuent d'exposer un match commencé et se contentent de le
    repricer en direct, sans que rien dans la réponse ne le dise. Seul Betano
    marque ses cotes live, et seul Ladbrokes demande explicitement
    `live: 0` ; pour Circus, Unibet (et ses clones Kambi), Napoleon ou
    StarCasino, un match en cours est indiscernable d'un match à venir.

    Le vrai discriminant est le PRIX. Un marché réellement oublié a gardé sa
    cote d'avant le coup d'envoi ; un book qui price en direct l'a forcément
    déplacée. D'où la quatrième condition : la cote actuelle doit être
    identique à la dernière relevée avant le coup d'envoi. Sans historique on
    se tait — l'exigence est une preuve positive d'immobilité, pas une absence
    de preuve du contraire.

    Deux garde-fous supplémentaires, chacun contre un faux positif précis :

    - Les cotes issues d'un flux LIVE sont ignorées. Betano en expose un :
      sans ce filtre, tout match en cours qu'il price passerait pour une
      erreur.

    - On retient l'heure la PLUS TARDIVE parmi celles connues, à l'inverse de
      _kickoff qui prend la plus précoce. Les deux vont dans le sens sûr, mais
      pas pour la même raison : là-bas il s'agit de ne pas alerter sur un match
      peut-être commencé, ici d'être certain qu'il l'est.

    Au-delà de _LATE_MARKET_MAX_MIN on cesse d'alerter : un marché encore
    ouvert deux heures après le coup d'envoi ne relève plus de l'oubli mais
    d'un horaire faux, et le pari ne serait pas payé."""
    recent = _PINNACLE_RECENT if recent is None else recent
    stats = Counter() if stats is None else stats
    # Sans réponse de Pinnacle à ce cycle, `live_now` est vide et TOUT événement
    # mémorisé passe pour disparu : le veto « Pinnacle le price encore » saute
    # sans bruit, au moment précis où l'on est le moins sûr de soi. Un recul
    # après 403 ou un sondage espacé suffisent à déclencher ça. On préfère ne
    # rien dire — la détection reprendra au cycle suivant.
    if not pinnacle_quotes:
        stats["pinnacle_muet"] += 1
        return {}
    live_now = {q.event_key for q in pinnacle_quotes}

    # Événements que Pinnacle connaissait, qu'il ne price plus, et dont le coup
    # d'envoi est dépassé de la bonne quantité.
    started: set[str] = set()
    for ek in recent:
        if ek in live_now:
            continue
        parsed = parse_event_key(ek)
        if parsed is None:
            continue
        mins = (now - parsed[0]).total_seconds() / 60.0
        if _LATE_MARKET_MIN_MIN <= mins <= _LATE_MARKET_MAX_MIN:
            started.add(ek)
    if not started:
        return {}

    if not soft_raw:
        return {}
    mapping = reconcile_event_keys(
        reference_keys=list(started),
        candidate_keys={q.event_key for q in soft_raw},
        time_tolerance_minutes=tolerance_for(sport),
    )

    # Premier passage : classer chaque cote en FIGÉE (inchangée depuis le coup
    # d'envoi, donc suspecte) ou VIVANTE (le book a repricé, donc utilisable
    # comme référence). Une cote sans historique n'est ni l'une ni l'autre :
    # on ne peut prouver ni qu'elle a bougé, ni qu'elle est restée. L'inclure
    # dans le consensus tirerait la référence vers le prix périmé et masquerait
    # justement l'écart qu'on cherche.
    frozen: list[tuple[str, OddQuote]] = []
    # (ref_key, market, line) -> {book: {label: cote}}
    live: dict[tuple, dict[Book, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    # Un événement porte des dizaines de cotes : sans ce cache, l'historique
    # serait relu une fois par cote au lieu d'une fois par (match, book).
    history: dict[tuple[str, Book], dict] = {}
    for q in soft_raw:
        match = mapping.get(q.event_key)
        if match is None:
            continue
        ref_key = match[0]
        # L'heure du book compte aussi : au tennis, le rapprochement tolère
        # trois heures d'écart. Si le book annonce un coup d'envoi encore à
        # venir, c'est peut-être lui qui a raison.
        book_parsed = parse_event_key(q.event_key)
        if book_parsed is not None:
            if (now - book_parsed[0]).total_seconds() / 60.0 < _LATE_MARKET_MIN_MIN:
                continue
        mkey = (ref_key, q.market.value, q.outcome.line)

        # Une cote issue d'un flux live est vivante par construction : c'est le
        # book lui-même qui la déclare en direct.
        if q.from_live_feed:
            live[mkey][q.book][q.outcome.label] = q.decimal_odd
            continue

        hkey = (ref_key, q.book)
        if hkey not in history:
            ref_parsed = parse_event_key(ref_key)
            kickoff = ref_parsed[0] if ref_parsed is not None else now
            # Avant le coup d'envoi la cote est rangée sous la clé de la
            # référence ; une fois Pinnacle parti, plus rien ne la réaligne et
            # elle repasse sous celle du book. On interroge donc les deux.
            past = prior_odds(ref_key, q.book, kickoff)
            if not past and q.event_key != ref_key:
                past = prior_odds(q.event_key, q.book, kickoff)
            history[hkey] = past or {}
        before = history[hkey].get(
            (q.market.value, q.outcome.label, q.outcome.line))
        if before is None:
            stats["sans_historique"] += 1
            continue
        if abs(before - q.decimal_odd) > 1e-9:
            stats["cote_bougée"] += 1
            live[mkey][q.book][q.outcome.label] = q.decimal_odd
            continue
        frozen.append((ref_key, q))

    if not frozen:
        return {}

    # Second passage : une cote figée ne vaut une alerte que si le marché a
    # DIVERGÉ. Sans cette mesure, on signalait aussi bien un book qui a oublié
    # de suspendre un 1-1 qu'un book simplement lent sur un 0-0 sans histoire —
    # le second n'offre rien à gagner, et c'est lui qui noyait le canal.
    out: dict[tuple[str, Book], list[OddQuote]] = defaultdict(list)
    consensus: dict[tuple, dict[str, float] | None] = {}
    for ref_key, q in frozen:
        mkey = (ref_key, q.market.value, q.outcome.line)
        if mkey not in consensus:
            others = {b: o for b, o in live.get(mkey, {}).items() if b != q.book}
            consensus[mkey] = consensus_probs(others)
        probs = consensus[mkey]
        if probs is None:
            # Personne d'autre ne price ce marché en direct : impossible de
            # savoir si le prix figé est devenu absurde. On se tait.
            stats["sans_consensus"] += 1
            continue
        fair = probs.get(q.outcome.label)
        if fair is None:
            stats["sans_consensus"] += 1
            continue
        edge = edge_pct(q.decimal_odd, fair)
        if edge < _LATE_MARKET_MIN_EDGE:
            stats["écart_faible"] += 1
            continue
        stats["retenue"] += 1
        # L'écart voyage avec la cote : c'est lui que l'alerte doit montrer.
        _LATE_EDGES[(ref_key, q.book, q.market.value, q.outcome.label,
                     q.outcome.line)] = edge
        out[(ref_key, q.book)].append(q)
    return dict(out)


# Écart mesuré par cote retenue, relu au moment de formater l'alerte. Un
# dictionnaire plutôt qu'un champ sur OddQuote : la structure est gelée et
# partagée par tous les scrapers, alors que cette valeur n'a de sens que ici.
_LATE_EDGES: dict[tuple, float] = {}


def late_market_edge(ref_key: str, book: Book, q: OddQuote) -> float | None:
    return _LATE_EDGES.get(
        (ref_key, book, q.market.value, q.outcome.label, q.outcome.line))


# (event_key, book) déjà signalés, avec l'instant de la dernière alerte. Un
# marché oublié le reste plusieurs cycles : sans mémoire, la même erreur
# partirait toutes les quinze secondes et rendrait le canal critique
# inutilisable.
#
# Cinq minutes, et non trente : tant que le marché reste ouvert, l'occasion
# vit encore, et le message porte le temps écoulé depuis le coup d'envoi —
# donc chaque rappel apprend quelque chose de neuf. C'est aussi la cadence à
# laquelle le score peut avoir changé, ce qui change tout sur un marché de
# type « les deux équipes marquent ».
# Dernier score connu par événement, pour repérer un but. Le flux live Betano
# est la seule source de score du projet : Pinnacle ignore les matchs en cours.
_LIVE_SCORES: dict[str, tuple[int, int, int]] = {}

_LATE_ALERTED: dict[tuple, float] = {}
_LATE_ALERT_COOLDOWN = float(os.getenv("LATE_MARKET_COOLDOWN_SEC", "300"))


def read_live_scores(betano_file: str | None) -> dict[str, tuple[int, int, int]]:
    """Scores en direct du dump Betano, ou {} si indisponible.

    Jamais bloquant : une absence de score ne doit pas empêcher la détection
    des marchés en retard, qui fonctionne très bien sans."""
    if not betano_file:
        return {}
    try:
        import json as _json
        from pathlib import Path as _Path
        raw = _Path(betano_file).read_text()
        return betano_parse_live_scores(_json.loads(raw))
    except Exception:                                           # noqa: BLE001
        return {}


def goals_since_last_cycle(
    scores: dict[str, tuple[int, int, int]],
    previous: dict[str, tuple[int, int, int]],
) -> set[str]:
    """Événements dont le score a changé depuis le cycle précédent.

    Un événement vu pour la PREMIÈRE fois ne compte pas comme un but : au
    démarrage du daemon tous les matchs en cours auraient l'air de venir de
    marquer, et le canal partirait en rafale."""
    changed: set[str] = set()
    for ek, (h, a, _m) in scores.items():
        prev = previous.get(ek)
        if prev is None:
            continue
        if (h, a) != (prev[0], prev[1]):
            changed.add(ek)
    return changed


def forget_finished_scores(scores: dict, now: datetime, max_age_h: float = 6.0) -> None:
    """Oublier les matchs terminés — le daemon tourne pendant des semaines.

    Le score n'est mémorisé que pour comparer deux cycles consécutifs ; passé
    six heures le match est fini et n'apprendra plus rien. Sans ça le
    dictionnaire garde tous les matchs jamais vus depuis le démarrage."""
    stale = []
    for ek in scores:
        parsed = parse_event_key(ek)
        if parsed is None or (now - parsed[0]).total_seconds() > max_age_h * 3600:
            stale.append(ek)
    for ek in stale:
        del scores[ek]


def _report_late_markets(late: dict, sport: str, tg_cfg,
                         goals: set[str] | None = None) -> None:
    """Alerter une fois par (match, book), puis se taire pendant le délai."""
    if not late:
        return
    now_m = time.monotonic()
    for k in [k for k, t in _LATE_ALERTED.items() if now_m - t > _LATE_ALERT_COOLDOWN * 2]:
        del _LATE_ALERTED[k]

    goals = goals or set()
    now = datetime.now(timezone.utc)
    fresh = []
    for (ek, book), quotes in late.items():
        seen = _LATE_ALERTED.get((ek, book))
        # Un but rouvre immédiatement la parole : c'est précisément l'instant
        # où un marché prématch oublié devient exploitable — « les deux
        # équipes marquent » sur un 1-1 est déjà gagné. Attendre le prochain
        # rappel ferait manquer la seule minute qui compte.
        if ek not in goals and seen is not None and now_m - seen < _LATE_ALERT_COOLDOWN:
            continue
        parsed = parse_event_key(ek)
        if parsed is None:
            continue
        edges = {(q.market.value, q.outcome.label, q.outcome.line): e
                 for q in quotes
                 if (e := late_market_edge(ek, book, q)) is not None}
        fresh.append((ek, book, quotes, (now - parsed[0]).total_seconds() / 60.0,
                      _LIVE_SCORES.get(ek), ek in goals, edges))
    if not fresh:
        return
    console.print(
        f"\\[{sport}]   ⏱️  marchés en retard : "
        + ", ".join(f"{b.value} ({len(q)} cotes)" for _, b, q, *_ in fresh)
    )
    sent = send_late_market_alerts(
        fresh, tg_cfg,
        print_fn=lambda s: console.print(f"[yellow]{s}[/yellow]"), sport=sport,
    )
    # Ne mémoriser que ce qui est réellement parti : un envoi différé par la
    # limite de débit doit repasser au cycle suivant.
    for ek, book, *_rest in sent:
        _LATE_ALERTED[(ek, book)] = now_m
