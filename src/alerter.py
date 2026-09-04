from __future__ import annotations

import os
import sqlite3
from pathlib import Path
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9 fallback — sandbox is 3.11 so this is safety net only
    ZoneInfo = None  # type: ignore[assignment]

from .config import SQLITE_BUSY_TIMEOUT_SEC
from .matcher import parse_event_key
from .models import Book, ValueBet, is_half_time
from .surebet import Surebet
from .middle import Middle
from . import teams


# Plafond de mise (% bankroll) applique par-dessus le quart de Kelly.
_MAX_STAKE_PCT = float(os.getenv("TELEGRAM_MAX_STAKE_PCT", "3.0"))


def _round_stake(eur: float) -> float:
    """Arrondit une mise a un montant 'humain' (camouflage anti-limitation :
    un bot qui mise 12,47 EUR se fait griller -- aucun parieur ne mise a 2
    decimales). Paliers : 1 EUR sous 10, 5 EUR sous 50, 10 EUR au-dela.
    Desactivable en mettant TELEGRAM_ROUND_STAKES=0 (renvoie la mise brute)."""
    if eur <= 0:
        return 0.0
    if os.getenv("TELEGRAM_ROUND_STAKES", "1") != "1":
        return round(eur, 2)
    step = 1.0 if eur < 10 else 5.0 if eur < 50 else 10.0
    return max(step, round(eur / step) * step)


def _round_stake5(eur: float) -> float:
    """Comme _round_stake mais cale TOUJOURS sur un multiple de 5 (mises de
    middle : on veut 40/20/45/25, jamais 42/22). Minimum 5 EUR.
    Respecte aussi TELEGRAM_ROUND_STAKES=0 (renvoie la mise brute)."""
    if eur <= 0:
        return 0.0
    if os.getenv("TELEGRAM_ROUND_STAKES", "1") != "1":
        return round(eur, 2)
    return max(5.0, round(eur / 5.0) * 5.0)


# --- Staking mode -----------------------------------------------------------
# "kelly" (default, legacy) = quarter-Kelly% × bankroll, capped. This embeds the
#   1/(odds-1) term, which over-stakes short odds (their edge is thinnest) and
#   leaks the edge to the book via stake size — a sharp tell.
# "flat" = base fixe + léger palier EV, SANS pondération par la cote. Anti-
#   limitation (mises plus uniformes) et aligné sur l'edge réel (l'EV, calibré).
#   Active-le avec STAKE_MODE=flat dans .env.
_STAKE_MODE = os.getenv("STAKE_MODE", "kelly").lower()
# Mode "flat" = mise STABLE, indépendante de la cote, avec un léger nudge EV.
#   base = STAKE_PCT % de la bankroll (look "Kelly" : mise en % du capital, donc
#   la bankroll pilote le niveau) ; si STAKE_PCT=0 on retombe sur STAKE_BASE_EUR
#   (€ fixe). Boost DOUX au-dessus de STAKE_EV_TIER (1.0 = l'EV n'influence pas
#   du tout). La cote n'entre JAMAIS dans le calcul.
_STAKE_PCT = float(os.getenv("STAKE_PCT", "2.0"))            # base = ce % de la bankroll (0 => € fixe)
_STAKE_BASE_EUR = float(os.getenv("STAKE_BASE_EUR", "25"))   # base € fixe si STAKE_PCT=0
_STAKE_EV_TIER = float(os.getenv("STAKE_EV_TIER", "15"))     # EV% à partir duquel léger boost
_STAKE_EV_MULT = float(os.getenv("STAKE_EV_MULT", "1.25"))   # boost DOUX (1.0 = pas d'influence EV)


def _advised_stake_eur(ev_pct: float | None, kelly_stake_pct: float | None,
                       bankroll: float) -> float | None:
    """€ stake advised for a value bet, per _STAKE_MODE. Returns None when no
    stake can be computed (kelly mode without a stored Kelly%)."""
    if _STAKE_MODE == "flat":
        base = (_STAKE_PCT / 100.0 * bankroll) if _STAKE_PCT > 0 else _STAKE_BASE_EUR
        mult = _STAKE_EV_MULT if (ev_pct is not None and ev_pct >= _STAKE_EV_TIER) else 1.0
        return _round_stake(base * mult)
    if kelly_stake_pct is None:
        return None
    pct = min(kelly_stake_pct, _MAX_STAKE_PCT)
    return _round_stake(pct / 100.0 * bankroll)


def _advised_stake_line(ev_pct: float | None, kelly_stake_pct: float | None,
                        bankroll: float) -> str:
    """The 'Mise conseillée : …' text (without leading newline), or '' if none."""
    stake = _advised_stake_eur(ev_pct, kelly_stake_pct, bankroll)
    if stake is None:
        return ""
    if _STAKE_MODE == "flat":
        boost = "  ⚡" if (ev_pct is not None and ev_pct >= _STAKE_EV_TIER) else ""
        if _STAKE_PCT > 0 and bankroll > 0:
            return f"Mise conseillée : {stake:.0f}€ ({stake / bankroll * 100:.1f}% de {bankroll:.0f}€){boost}"
        return f"Mise conseillée : {stake:.0f}€{boost}"
    pct = min(kelly_stake_pct, _MAX_STAKE_PCT)
    return f"Mise conseillée : {stake:.0f}€ ({pct:.2f}% de {bankroll:.0f}€)"


# Belgium-friendly display: dates relative to today, kickoff in local time.
_LOCAL_TZ = ZoneInfo("Europe/Brussels") if ZoneInfo is not None else timezone.utc
_FR_WEEKDAYS = ["Lundi", "Mardi", "Mercredi", "Jeudi", "Vendredi", "Samedi", "Dimanche"]
_FR_MONTHS = [
    "janvier", "février", "mars", "avril", "mai", "juin",
    "juillet", "août", "septembre", "octobre", "novembre", "décembre",
]

# Book.value -> human label used in the message header.
_BOOK_NAMES = {
    Book.PINNACLE: "Pinnacle",
    Book.UNIBET_BE: "Unibet / 711 / Bingoal / Scooore",
    Book.BETANO_BE: "Betano",
    Book.BETFIRST: "BetFirst",
    Book.LADBROKES_BE: "Ladbrokes",
    Book.GOLDEN_PALACE: "Golden Palace",
    Book.STARCASINO_SPORT: "StarCasino",
    Book.CIRCUS_BE: "Circus",
    Book.SEVEN_ELEVEN_BE: "711",
    Book.BINGOAL_BE: "Bingoal",
    Book.SCOOORE_BE: "Scooore",
    Book.MERIDIAN_BE: "MeridianBet",
    Book.NAPOLEON_BE: "Napoleon",
    Book.BETCENTER: "Betcenter",
    Book.SMARKETS: "Smarkets",
    Book.MAGICBETTING: "MagicBetting",
    Book.ELITESPORTS: "EliteSports",
}

# Sport key -> emoji prepended to the matchup line in alerts. Keeps the chat
# scannable when value bets and surebets land back-to-back across sports.
_SPORT_EMOJIS = {
    "soccer": "⚽",
    "football": "⚽",     # accept either alias
    "tennis": "🎾",
    "basketball": "🏀",
    "basket": "🏀",
    "hockey": "🏒",
    "ice_hockey": "🏒",
    "esports": "🎮",
}


def _sport_prefix(sport: str | None) -> str:
    if not sport:
        return ""
    emoji = _SPORT_EMOJIS.get(sport.lower())
    return f"{emoji} " if emoji else ""


def _ht(x) -> str:
    """Un texte venu d'un flux, rendu inoffensif pour `parse_mode=HTML`.

    ⚠️ CE QUI ARRIVE SANS ELLE. Telegram refuse le message ENTIER avec un
    400 « can't parse entities » dès qu'un `&` nu ou un `<` traîne dedans —
    et « Brighton & Hove Albion », « Bosnia & Herzegovina », « Guinée-Bissau
    U<19 » sont des noms parfaitement ordinaires. Le message n'est pas
    tronqué, il n'existe pas.

    Et l'échec est PERMANENT, pas transitoire : `send_value_bet` ne marque le
    pari comme notifié que si l'envoi réussit. Un pari dont le nom casse le
    HTML est donc réessayé à CHAQUE cycle, indéfiniment, en payant sa pause
    de `min_send_interval_s` à chaque fois. Ces paris s'accumulent pendant que
    les envois valides, eux, se marquent et sortent de la file : au bout de
    quelques cycles, la file ne contient plus QUE des messages impossibles.
    Mesuré le 04/09 : 34 à 37 pauses par cycle de soccer, soit 109 à 119 s de
    cycle, pour zéro alerte reçue.

    `quote=False` : on n'échappe pas les guillemets. Le texte n'est jamais
    placé dans un attribut, et `&quot;` s'afficherait tel quel dans le
    message."""
    from html import escape
    return escape(str(x), quote=False)


def _nom_book(b) -> str:
    """Le nom lisible d'un book, échappé. Passe par `_ht` comme tout le
    reste : `_BOOK_NAMES` est écrit à la main aujourd'hui, mais le repli
    `b.value` vient du modèle et rien ne garantit qu'il le restera."""
    return _ht(_BOOK_NAMES.get(b, getattr(b, "value", b)))


def _prettify_team_name(normalized: str) -> str:
    """Resolve a space-stripped event-key fragment back to its human-readable
    name via the teams registry (populated by every scraper when it sees an
    original team string). Falls back to the old title-case behaviour when
    the registry doesn't know the team yet — typically only on the very
    first scan of an event before a scraper records it."""
    return teams.display(normalized)


def _time_to_kickoff(start: datetime, now: datetime | None = None) -> str:
    """Délai avant le coup d'envoi, en clair.

    Purement informatif — rien n'est filtré ici. Le délai reste utile à voir :
    mesuré sur 296 opportunités du canal premium, le CLV réel tombe de +11,2 %
    à +4,6 % au-delà de 24 h, la ligne de référence étant elle-même trop
    bruitée loin du match pour qu'un écart détecté veuille dire grand-chose.

    Aucun marqueur visuel de seuil : le premier essai en plaçait un à 12 h,
    or les données ont ensuite montré que la tranche 12-24 h est aussi bonne
    que les plus courtes. Signaler un seuil que la mesure ne soutient pas
    revient à pousser vers la mauvaise décision."""
    now = now or datetime.now(timezone.utc)
    hours = (start - now).total_seconds() / 3600
    if hours < 0:
        return ""
    if hours < 1:
        return f" — dans {hours * 60:.0f} min"
    return f" — dans {int(hours)} h"


def _format_kickoff(start: datetime, now: datetime | None = None) -> str:
    """'Aujourd'hui 21:00' / 'Demain 14:30' / 'Mardi 18 juin 19:00' in
    Belgian local time — no platform-locale dance, just a small FR table."""
    local = start.astimezone(_LOCAL_TZ)
    today_local = (now or datetime.now(timezone.utc)).astimezone(_LOCAL_TZ).date()
    delta_days = (local.date() - today_local).days
    hhmm = local.strftime("%H:%M")
    if delta_days == 0:
        return f"Aujourd'hui {hhmm}"
    if delta_days == 1:
        return f"Demain {hhmm}"
    weekday = _FR_WEEKDAYS[local.weekday()]
    month = _FR_MONTHS[local.month - 1]
    return f"{weekday} {local.day} {month} {hhmm}"


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str                          # main chat — value bets land here
    surebet_chat_id: str | None = None    # prematch surebets
    live_surebet_chat_id: str | None = None  # live surebets (match already started)
    min_minutes_to_kickoff: int = 15      # value bets : fenêtre morte avant le coup d'envoi
    # Surebets et middles gardent leur PROPRE fenêtre, volontairement plus
    # large. Le risque n'y est pas le même : un value bet se mesure contre une
    # référence sharp, qui le protège d'une cote périmée, alors qu'un surebet
    # est une incohérence ENTRE deux books — une seule ligne figée chez l'un
    # suffit à fabriquer un arbitrage qui n'existe pas. Mesuré le 17/08, la
    # tranche 5-15 min est la meilleure du système POUR LES VALUE BETS ; rien
    # n'a été mesuré côté surebets, et les ouvrir sur la même décision serait
    # étendre une conclusion au-delà de ce qui la soutient.
    surebet_min_minutes_to_kickoff: int = 15
    min_ev_pct: float = 5.0               # main chat: value bets below this stay silent
    main_max_ev_pct: float = 8.0          # main chat: value bets above this go to premium instead
    main_min_odd: float = 1.5             # main chat: only value bets within this odds band
    main_max_odd: float = 4.0
    min_surebet_margin_pct: float = 1.0   # surebets below this margin stay silent
    include_suspicious_surebets: bool = False  # opt-in to see flagged ones too
    surebet_dedup: bool = True            # off -> alert every scan even if seen before
    surebet_max_alerts: int = 2           # hard cap on alerts per surebet (jitter-proof)
    valuebet_dedup: bool = True           # off -> alert every scan even on stale bets
    surebet_roi_delta_pct: float = 0.5    # re-alert when ROI shifts by this many points
    valuebet_ev_delta_pct: float = 2.0    # re-alert when EV% shifts by this many points
    valuebet_max_alerts: int = 2          # hard cap on alerts per bet (jitter-proof)
    clv_chat_id: str | None = None        # toutes les CLV (>= min_clv_pct) atterrissent ici
    min_clv_pct: float = 5.0             # en dessous : aucune alerte CLV
    min_high_clv_pct: float = 15.0       # au-dessus : header 🔥 (même canal, juste un libellé)
    clv_window_minutes: int = 15          # send CLV alert when kickoff is within this many minutes
    clv_min_odd: float = 1.5             # CLV alerts only when the bet odd is >= this
    clv_max_odd: float = 4.0             # CLV alerts only when the bet odd is <= this
    bankroll: float = 1000.0              # base used to turn Kelly% into a € stake in alerts
    # Critical channel — receives a copy of every alert that clears the higher thresholds.
    # If not set, critical alerts are still sent to the normal channels only.
    critical_chat_id: str | None = None
    # Canal critique — la voie de DÉBORDEMENT, pas un doublon du premium.
    # Aucune limite de cote et aucun plafond d'EV : c'est précisément là que
    # doivent atterrir les cotes 14, 21, 40 à EV énorme, qu'aucune bande premium
    # n'accepte. En revanche il ne reçoit rien de ce que le premium a déjà pris
    # — une cote 1.82 à 53 % est du premium, et une seule alerte suffit.
    min_critical_ev_pct: float = 35.0    # value bets above this also go to critical channel
    min_critical_surebet_pct: float = 10.0  # surebets (margin%) above this also go to critical
    min_critical_clv_pct: float = 25.0   # CLV% above this also goes to critical channel
    # Premium channel — curated "best opportunities" copy: big value bets (incl.
    # the suspiciously-huge ones) within a sane odds band, plus juicy prematch
    # surebets. Opt-in via TELEGRAM_PREMIUM_CHAT_ID; no fallback when unset.
    # Canal MAINTENANCE — les pannes, pas les opportunites.
    #
    # Avant lui, `send_system_alert` partait sur le canal critique. Les deux
    # sont urgents mais ne demandent pas la meme chose : un value bet
    # exceptionnel se joue dans la minute, un book muet se repare. Les meler
    # oblige a trier a l'oeil au pire moment, et une panne noyee dans le flux
    # des grosses cotes est une panne qu'on rate — l'incident Pinnacle de
    # 1 h 30 (§12) n'avait ete vu par personne.
    #
    # Non defini : le comportement ne change PAS (canal critique, puis chat
    # principal). Un reglage absent ne doit jamais modifier la production.
    maintenance_chat_id: str | None = None
    # Seconde voie du critique : les GROSSES COTES que le premium refuse.
    #
    # Sans elle, un pari en cote 4-6 ecarte du premium (exclusion tennis, ou
    # canal premium absent) devait atteindre 35 % d'EV pour reapparaitre
    # quelque part. Entre 20 et 35 % il ne partait NULLE PART : ni joue, ni
    # observe. Fermer une bande au premium devait deplacer ces paris, pas les
    # effacer — on veut continuer a les mesurer.
    #
    # Ce n'est PAS un doublon : tout le bloc critique reste conditionne a
    # `not _premium_takes`. Un pari deja parti en premium ne repart jamais ici.
    critical_hi_min_ev: float = 20.0     # meme seuil que la bande longue du premium
    critical_hi_min_odd: float = 4.0     # au-dessus de la bande premium standard
    premium_chat_id: str | None = None
    min_premium_ev_pct: float = 8.0      # value bets at/above this go to premium channel
    # NB : le premium ne reçoit plus aucun surebet, quelle que soit la marge.
    # Ils vont sur le canal surebet et nulle part ailleurs (hors copie critique).
    premium_min_odd: float = 1.5         # premium value bets only within this odds band
    premium_max_odd: float = 4.0
    # Second premium lane: high odds only when the EV is big. Lets curated
    # long-shot value (cotes 4–6) reach premium *without* opening the main band
    # above 4.0. A bet qualifies for premium if it clears EITHER lane.
    premium_hi_min_ev: float = 20.0      # cotes premium_hi_min_odd..max only if EV >= this
    premium_hi_min_odd: float = 4.0
    premium_hi_max_odd: float = 6.0
    # Sports EXCLUS de la seule bande longue. Vide par défaut : sans réglage,
    # rien ne change.
    #
    # POURQUOI PAR SPORT ET NON GLOBALEMENT. Mesuré le 30/08 sur les
    # détections closes : la tranche 4,0-6,0 rend −20,34 % de ROI (n=406,
    # −2,3σ) et la tranche > 6,0 rend −16,20 % (n=230). Mais cette population
    # est à 96 % du TENNIS — 1 119 paris contre 34 au soccer. Fermer la bande
    # pour tous les sports appliquerait donc au soccer une conclusion tirée
    # d'un échantillon qui n'en contient presque pas.
    #
    # Le CLV dit l'inverse sur ces mêmes tranches (unibet > 6,0 à +17,15 %).
    # On tranche pour le P&L à cause d'un mécanisme connu : la marge d'un
    # book n'est pas répartie uniformément, elle se concentre sur les longues
    # cotes. Un devig qui la suppose uniforme surestime la probabilité juste
    # des outsiders — tout paraît value, le CLV s'illumine, la probabilité
    # réelle n'y est pas. C'est le biais favori-outsider, et c'est pour ça que
    # `pnl_detections` imprime déjà « décroissance régulière = devig suspect ».
    premium_hi_sports_exclus: tuple = ()
    # Totals middles — back Over low_line + Under high_line on two books; the
    # gap between the lines is profit. EV is priced against Pinnacle's devigged
    # totals ladder. Routed to the CLV channel (effective_clv_chat_id) per the
    # user's choice — no dedicated chat id.
    min_middle_ev_pct: float = 2.0       # middles below this EV stay silent
    middle_dedup: bool = True            # off -> re-alert every cycle even on stale middles
    middle_ev_delta_pct: float = 2.0     # re-alert when a middle's EV shifts by this many points
    middle_max_alerts: int = 2           # hard cap on alerts per middle (jitter-proof)
    middle_max_gap: float = 3.0          # ignore middles whose lines are more than this apart
    middle_stake_eur: float = 100.0      # TOTAL € spread across both legs of a middle (not the value-bet bankroll)
    # Rate limiting — Telegram caps bots at ~20 messages/min per group. We pace
    # sends per chat and honour 429 cooldowns so a burst can't get the bot
    # throttled for hours.
    min_send_interval_s: float = 3.2     # min gap between two messages to the same chat
    max_defer_s: float = 45.0            # if a chat's send queue is backed up past this, defer to next cycle
    parse_mode: str = "HTML"

    @classmethod
    def from_env(cls) -> "TelegramConfig | None":
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat = os.getenv("TELEGRAM_CHAT_ID")
        if not (token and chat):
            return None
        return cls(
            bot_token=token,
            chat_id=chat,
            surebet_chat_id=os.getenv("TELEGRAM_SUREBET_CHAT_ID") or None,
            live_surebet_chat_id=os.getenv("TELEGRAM_LIVE_SUREBET_CHAT_ID") or None,
            min_minutes_to_kickoff=int(os.getenv("TELEGRAM_MIN_MINUTES_TO_KICKOFF", "15")),
            surebet_min_minutes_to_kickoff=int(
                os.getenv("TELEGRAM_SUREBET_MIN_MINUTES_TO_KICKOFF", "15")),
            min_ev_pct=float(os.getenv("TELEGRAM_MIN_EV", "5.0")),
            main_max_ev_pct=float(os.getenv("TELEGRAM_MAIN_MAX_EV", "8.0")),
            main_min_odd=float(os.getenv("TELEGRAM_MAIN_MIN_ODD", "1.5")),
            main_max_odd=float(os.getenv("TELEGRAM_MAIN_MAX_ODD", "4.0")),
            min_surebet_margin_pct=float(os.getenv("TELEGRAM_MIN_SUREBET", "1.0")),
            include_suspicious_surebets=os.getenv("TELEGRAM_INCLUDE_SUSPICIOUS", "0") == "1",
            surebet_dedup=os.getenv("TELEGRAM_SUREBET_DEDUP", "1") == "1",
            surebet_max_alerts=int(os.getenv("TELEGRAM_SUREBET_MAX_ALERTS", "2")),
            valuebet_dedup=os.getenv("TELEGRAM_VALUEBET_DEDUP", "1") == "1",
            surebet_roi_delta_pct=float(os.getenv("TELEGRAM_SUREBET_ROI_DELTA", "0.5")),
            valuebet_ev_delta_pct=float(os.getenv("TELEGRAM_VALUEBET_EV_DELTA", "2.0")),
            valuebet_max_alerts=int(os.getenv("TELEGRAM_VALUEBET_MAX_ALERTS", "2")),
            clv_chat_id=os.getenv("TELEGRAM_CLV_CHAT_ID") or None,
            min_clv_pct=float(os.getenv("TELEGRAM_MIN_CLV", "5.0")),
            min_high_clv_pct=float(os.getenv("TELEGRAM_MIN_HIGH_CLV", "15.0")),
            clv_window_minutes=int(os.getenv("TELEGRAM_CLV_WINDOW_MINUTES", "15")),
            clv_min_odd=float(os.getenv("TELEGRAM_CLV_MIN_ODD", "1.5")),
            clv_max_odd=float(os.getenv("TELEGRAM_CLV_MAX_ODD", "4.0")),
            bankroll=float(os.getenv("TELEGRAM_BANKROLL", "1000.0")),
            min_send_interval_s=float(os.getenv("TELEGRAM_MIN_SEND_INTERVAL", "3.2")),
            max_defer_s=float(os.getenv("TELEGRAM_MAX_DEFER", "45.0")),
            critical_chat_id=os.getenv("TELEGRAM_CRITICAL_CHAT_ID") or None,
            min_critical_ev_pct=float(os.getenv("TELEGRAM_MIN_CRITICAL_EV", "35.0")),
            min_critical_surebet_pct=float(os.getenv("TELEGRAM_MIN_CRITICAL_SUREBET", "10.0")),
            min_critical_clv_pct=float(os.getenv("TELEGRAM_MIN_CRITICAL_CLV", "25.0")),
            maintenance_chat_id=os.getenv("TELEGRAM_MAINTENANCE_CHAT_ID") or None,
            critical_hi_min_ev=float(os.getenv("TELEGRAM_CRITICAL_HI_EV", "20.0")),
            critical_hi_min_odd=float(os.getenv("TELEGRAM_CRITICAL_HI_MIN_ODD", "4.0")),
            premium_chat_id=os.getenv("TELEGRAM_PREMIUM_CHAT_ID") or None,
            min_premium_ev_pct=float(os.getenv("TELEGRAM_MIN_PREMIUM_EV", "8.0")),
            premium_min_odd=float(os.getenv("TELEGRAM_PREMIUM_MIN_ODD", "1.5")),
            premium_max_odd=float(os.getenv("TELEGRAM_PREMIUM_MAX_ODD", "4.0")),
            premium_hi_min_ev=float(os.getenv("TELEGRAM_PREMIUM_HI_EV", "20.0")),
            premium_hi_sports_exclus=tuple(
                x.strip().lower()
                for x in os.getenv("TELEGRAM_PREMIUM_HI_EXCLUT", "").split(",")
                if x.strip()),
            premium_hi_min_odd=float(os.getenv("TELEGRAM_PREMIUM_HI_MIN_ODD", "4.0")),
            premium_hi_max_odd=float(os.getenv("TELEGRAM_PREMIUM_HI_MAX_ODD", "6.0")),
            min_middle_ev_pct=float(os.getenv("TELEGRAM_MIN_MIDDLE_EV", "2.0")),
            middle_dedup=os.getenv("TELEGRAM_MIDDLE_DEDUP", "1") == "1",
            middle_ev_delta_pct=float(os.getenv("TELEGRAM_MIDDLE_EV_DELTA", "2.0")),
            middle_max_alerts=int(os.getenv("TELEGRAM_MIDDLE_MAX_ALERTS", "2")),
            middle_max_gap=float(os.getenv("TELEGRAM_MIDDLE_MAX_GAP", "3.0")),
            middle_stake_eur=float(os.getenv("TELEGRAM_MIDDLE_STAKE", "100.0")),
        )

    @property
    def effective_surebet_chat_id(self) -> str:
        """Prematch surebets — falls back to main chat if not set."""
        return self.surebet_chat_id or self.chat_id

    @property
    def effective_live_surebet_chat_id(self) -> str:
        """Live surebets — falls back to prematch chat if no dedicated live chat is set."""
        return self.live_surebet_chat_id or self.effective_surebet_chat_id

    @property
    def effective_clv_chat_id(self) -> str:
        """Toutes les CLV (≥ min_clv_pct) — fallback vers le chat principal si non défini."""
        return self.clv_chat_id or self.chat_id

    @property
    def effective_critical_chat_id(self) -> str | None:
        """Critical channel — None when not configured (no fallback: critical is opt-in)."""
        return self.critical_chat_id

    @property
    def effective_maintenance_chat_id(self) -> str:
        """Les pannes. Retombe sur le critique, puis sur le chat principal —
        une alerte d'exploitation ne doit JAMAIS disparaitre faute de canal."""
        return (self.maintenance_chat_id or self.effective_critical_chat_id
                or self.chat_id)

    @property
    def effective_premium_chat_id(self) -> str | None:
        """Premium channel — None when not configured (no fallback: premium is opt-in)."""
        return self.premium_chat_id


def format_surebet(sb: Surebet, sport: str | None = None, is_live: bool = False) -> str:
    """Surebet messages need to list every leg with its book — that's the
    whole point — so the format is taller than a value bet alert. Visually
    distinct (💰 vs 🎯) so the user can tell them apart in the chat preview.
    Suspicious flagged surebets are prefixed with ⚠️ so the user knows the
    pipeline thinks this is probably a phantom — verify before acting.
    Optionally takes the sport string to surface a per-sport emoji."""
    parsed = parse_event_key(sb.event_key)
    if parsed is not None:
        start, home_norm, away_norm = parsed
        matchup = (f"{_ht(_prettify_team_name(home_norm))} vs "
                   f"{_ht(_prettify_team_name(away_norm))}")
        when_line = f"📅 {_format_kickoff(start)}\n"
    else:
        matchup = _ht(sb.event_key)
        when_line = ""

    line_suffix = f" {sb.line}" if sb.line is not None else ""
    legs_lines = "\n".join(
        f"  • <b>{_ht(label)}</b> @ {odd:.2f} — {_nom_book(book)}"
        for label, (odd, book) in sb.legs.items()
    )
    if sb.suspicious:
        header_emoji = "⚠️ <b>SUREBET SUSPECT</b>"
    elif is_live:
        header_emoji = "🔴 <b>SUREBET LIVE</b>"
    else:
        header_emoji = "💰 <b>SUREBET</b>"
    suspect_footer = (
        "\n<i>⚠️ Marge inhabituelle — vérifie les équipes et les cotes "
        "avant de jouer.</i>"
        if sb.suspicious else ""
    )

    return (
        f"{header_emoji} +{sb.margin * 100:.2f}% (ROI {sb.roi * 100:.2f}%)\n"
        f"{_sport_prefix(sport)}{matchup} — {_ht(sb.market.value)}{line_suffix}\n"
        f"{when_line}"
        f"{legs_lines}"
        f"{suspect_footer}"
    )


def format_middle(m: Middle, sport: str | None = None, total_stake: float = 100.0) -> str:
    """Totals middle alert: both legs with their book and a balanced € stake,
    the gap value(s) that win both, the Pinnacle-priced gap probability and the
    resulting EV. `total_stake` is the TOTAL € spread across the two legs (not
    the value-bet bankroll) — a middle ties up money on both sides, so it has
    its own, smaller budget. Mises are rounded (camouflage) like everywhere."""
    parsed = parse_event_key(m.event_key)
    if parsed is not None:
        start, home_norm, away_norm = parsed
        matchup = (f"{_ht(_prettify_team_name(home_norm))} vs "
                   f"{_ht(_prettify_team_name(away_norm))}")
        when_line = f"📅 {_format_kickoff(start)}\n"
    else:
        matchup = _ht(m.event_key)
        when_line = ""

    o_odd, o_book = m.over_leg
    u_odd, u_book = m.under_leg
    stakes = m.stakes(total_stake)
    s_over = _round_stake5(stakes["over"])
    s_under = _round_stake5(stakes["under"])
    total_bet = s_over + s_under

    # Perte/gain net de la position sur les deux scénarios "raté" (hors écart),
    # calculé sur les mises rondes réellement jouées :
    #   total > ligne haute -> Over gagne, Under perd  -> on récupère s_over*cote_over
    #   total < ligne basse  -> Under gagne, Over perd  -> on récupère s_under*cote_under
    # La moyenne des deux donne la perte moyenne attendue si le middle ne tombe pas.
    miss_over_wins = s_over * o_odd - total_bet
    miss_under_wins = s_under * u_odd - total_bet
    avg_miss = (miss_over_wins + miss_under_wins) / 2.0
    if avg_miss < 0:
        miss_line = f"\n💸 Perte moyenne si raté : −{abs(avg_miss):.0f}€ (sur {total_bet:.0f}€ misés)"
    else:
        miss_line = f"\n✅ Gain minimum même si raté : +{avg_miss:.0f}€ (surebet-middle)"

    gap_txt = " ou ".join(str(v) for v in m.gap_values) or "—"

    return (
        f"🎯 <b>MIDDLE +{m.ev_pct:.2f}% EV</b> — proba gap {m.mid_prob * 100:.1f}%\n"
        f"{_sport_prefix(sport)}{matchup}\n"
        f"{when_line}"
        f"  • <b>Over {m.low_line:g}</b> @ {o_odd:.2f} — {_nom_book(o_book)}  → {s_over:.0f}€\n"
        f"  • <b>Under {m.high_line:g}</b> @ {u_odd:.2f} — {_nom_book(u_book)}  → {s_under:.0f}€\n"
        f"<i>Middle (les deux gagnent) si total = {gap_txt}</i>"
        f"{miss_line}"
    )


def _clv_bet_kelly_pct(bet: sqlite3.Row) -> float | None:
    """Read the stored Kelly% from a value_bets row if present. The row may be a
    plain dict (alert-test) or a sqlite3.Row — both expose .keys() — so we probe
    for the column and return None when it's absent."""
    if "kelly_pct" in bet.keys():
        val = bet["kelly_pct"]
        return float(val) if val is not None else None
    return None


def format_clv_alert(
    bet: sqlite3.Row,
    clv_pct: float,
    current_pin_odd: float,
    mins_to_kickoff: int,
    sport: str | None = None,
    is_high: bool = False,
    bankroll: float = 1000.0,
) -> str:
    """Message envoyé peu avant le coup d'envoi quand la CLV est confirmée positive.
    is_high=True → header 🔥 et libellé différent pour le groupe prioritaire.
    bankroll sert à convertir le Kelly% stocké en mise conseillée en €.

    current_pin_odd est la ligne Pinnacle DÉVIGÉE, pas son prix affiché : le
    prix affiché contient la commission, et mesurer la CLV contre lui ajoute
    cette commission à chaque pari."""
    from .models import Book as _Book  # local import to avoid circular at module level
    parsed = parse_event_key(bet["event_key"])
    if parsed is not None:
        start, home_norm, away_norm = parsed
        matchup = (f"{_ht(_prettify_team_name(home_norm))} vs "
                   f"{_ht(_prettify_team_name(away_norm))}")
        when_line = f"📅 {_format_kickoff(start)} (dans {mins_to_kickoff} min)\n"
    else:
        matchup = _ht(bet["event_key"])
        when_line = f"📅 Dans {mins_to_kickoff} min\n"

    try:
        book_name = _nom_book(_Book(bet["book"]))
    except ValueError:
        book_name = _ht(bet["book"])

    header = (
        f"🔥 <b>CLV ÉLEVÉ {clv_pct:+.2f}% confirmé</b> — {book_name}"
        if is_high else
        f"⏰ <b>CLV {clv_pct:+.2f}% confirmé</b> — {book_name}"
    )
    line_suffix = f" {bet['line']}" if bet["line"] is not None else ""

    ev_pct = bet["ev_pct"] if "ev_pct" in bet.keys() else None
    _s = _advised_stake_line(ev_pct, _clv_bet_kelly_pct(bet), bankroll)
    stake_line = ("\n" + _s) if _s else ""

    return (
        f"{header}\n"
        f"{_sport_prefix(sport)}{matchup}\n"
        f"{when_line}"
        f"Pari : <b>{_ht(bet['outcome_label'])}{line_suffix}</b> @ "
        f"{float(bet['odd_taken']):.2f}\n"
        f"Ligne juste actuelle : {current_pin_odd:.2f}"
        f"{stake_line}"
    )


def format_value_bet(bet: ValueBet, sport: str | None = None,
                     bankroll: float = 1000.0) -> str:
    """Human-readable single message per bet — kept compact so a phone
    notification preview shows EV%, opponent and kickoff before the user
    needs to expand the chat. Dates are localised to Brussels time.
    Optionally takes the sport string to surface a per-sport emoji.
    bankroll converts the stored quarter-Kelly% into a concrete € stake."""
    book_name = " / ".join(
        _nom_book(b) for b in (bet.book, *bet.also_books)
    )

    # Try to extract a readable home/away + kickoff from the event_key.
    parsed = parse_event_key(bet.event_key)
    if parsed is not None:
        start, home_norm, away_norm = parsed
        matchup = (f"{_ht(_prettify_team_name(home_norm))} vs "
                   f"{_ht(_prettify_team_name(away_norm))}")
        when_line = f"📅 {_format_kickoff(start)}{_time_to_kickoff(start)}\n"
    else:
        # Fall back to the raw key if it doesn't parse — better than crashing.
        matchup = _ht(bet.event_key)
        when_line = ""

    line_suffix = f" {bet.outcome.line}" if bet.outcome.line is not None else ""
    # Only some sources carry a competition name, so this line is conditional
    # rather than showing an empty placeholder.
    league_line = f"🏆 {_ht(bet.league)}\n" if bet.league else ""
    # Only flagged when it isn't Pinnacle. A fallback reference is thinner, so
    # the same EV% deserves less confidence — and silently presenting the two
    # as equivalent is how a shaky number gets treated as a sure thing.
    # Marqué à DEUX endroits quand la référence n'est pas Pinnacle : collé à la
    # fair odd, pour qu'on sache d'où sort ce chiffre au moment où on le lit, et
    # sur sa propre ligne, pour que ce soit visible d'un coup d'œil sans lire.
    #
    # Le repli se décide au niveau du MARCHÉ, pas du match : Pinnacle peut très
    # bien pricer le 1X2 de cette rencontre sans pricer son over/under. Dire
    # « ne price pas ce match » serait donc faux une fois sur deux.
    ref_suffix = ""
    ref_line = ""
    if bet.reference_book is not None and bet.reference_book is not Book.PINNACLE:
        ref_name = _nom_book(bet.reference_book)
        ref_suffix = f" · réf. <b>{ref_name}</b>"
        ref_line = (
            f"🔵 <b>Fair odd calculée sur {ref_name}</b> — Pinnacle ne price pas "
            f"ce marché. EV à prendre avec plus de prudence.\n"
        )

    # Bandeau d'EV extrême. Ces alertes ne sont PAS supprimées — elles sont les
    # plus rentables quand elles sont vraies, et les plus coûteuses quand elles
    # ne le sont pas. Mais au-delà de 100 % d'EV, la cause la plus fréquente
    # n'est pas un cadeau du book : c'est la référence qui déraille.
    #
    # Mesuré le 16/08 : cinq détections entre +146 % et +260 %, toutes des
    # totaux over 6,5 calculés sur une ligne Smarkets à 144 % de marge, elle-
    # même contredite par la ligne 5,5 du même carnet. Le contrôle de marge
    # empêche désormais ce cas précis, mais il ne peut pas tout attraper — une
    # référence peut être fausse en restant cohérente.
    #
    # D'où ce bandeau plutôt qu'un plafond : couper ces alertes masquerait le
    # défaut sans le corriger, et supprimerait aussi les vraies.
    extreme = ""
    if bet.ev_pct >= 100:
        seuil = 130 if bet.ev_pct >= 130 else (120 if bet.ev_pct >= 120 else 100)
        extreme = (
            f"🛑 <b>EV &gt; {seuil} % — vérifier avant de miser.</b> À ce niveau, "
            f"c'est presque toujours la ligne de référence qui est fausse, pas "
            f"le book qui est généreux. Compare la cote aux autres softbooks : "
            f"s'ils sont tous d'accord entre eux et loin de la « juste », "
            f"n'y va pas.\n"
        )

    return (
        f"{extreme}"
        f"🎯 <b>+{bet.ev_pct:.2f}% EV</b> — {book_name}\n"
        f"{_sport_prefix(sport)}{matchup}\n"
        f"{league_line}"
        f"{when_line}"
        f"Pari : <b>{_ht(bet.outcome.label)}{line_suffix}</b> @ {bet.odd_taken:.2f} "
        f"(fair {bet.fair_odd:.2f}{ref_suffix})\n"
        f"{ref_line}"
        f"{_advised_stake_line(bet.ev_pct, bet.kelly_stake_pct, bankroll)}"
    )


_PLAYS_DB = Path(__file__).resolve().parent.parent / "data" / "valuebet.db"

# Importe ICI et pas en tete de fichier : `channels` importe `routing`, et
# `routing` n'importe rien — aucun cycle possible. L'import est au niveau du
# MODULE (jamais dans une fonction) : un import local rendrait le nom local a
# toute la fonction, y compris sur les chemins ou le bloc ne s'execute pas.
from src.channels import PREMIUM as _ROUTE_PREMIUM, CRITIQUE as _ROUTE_CRITIQUE
from src.channels import PRINCIPAL as _ROUTE_PRINCIPAL
from src.channels import charger as charger_canaux
from src.routing import canaux_pour
from src.storage import Storage

# Presentation PAR ROUTE, le temps de la migration. Les trois routes traduites
# de .env gardent exactement l'en-tete et le bouton qu'elles ont aujourd'hui.
# Un canal cree par l'utilisateur n'en a pas — et rien ne permet encore d'en
# creer. A porter sur une colonne de `channels` quand les commandes arriveront.
_PRESENTATION = {
    _ROUTE_PRINCIPAL: ("", False),
    _ROUTE_PREMIUM: ("💎 <b>VALUE PREMIUM</b>\n", True),
    _ROUTE_CRITIQUE: ("🚨 <b>VALUE BET EXCEPTIONNEL</b>\n", False),
}


def _record_pending_play(payload: dict) -> str:
    """Persist the data needed to log a played bet, keyed by a short token that
    rides in the Telegram button's callback_data. bot_listener.py reads this
    table when the user taps Jouer. Kept separate from value_bets because the
    alerter doesn't carry the inserted row id."""
    import uuid

    token = uuid.uuid4().hex[:12]
    con = sqlite3.connect(str(_PLAYS_DB), timeout=SQLITE_BUSY_TIMEOUT_SEC)
    try:
        con.execute(f"PRAGMA busy_timeout={int(SQLITE_BUSY_TIMEOUT_SEC * 1000)}")
        con.execute(
            "CREATE TABLE IF NOT EXISTS pending_plays("
            "token TEXT PRIMARY KEY, sport TEXT, match TEXT, book TEXT, "
            "selection TEXT, cote REAL, mise REAL, ev REAL, created_at TEXT, dedup_key TEXT)"
        )
        try:
            con.execute("ALTER TABLE pending_plays ADD COLUMN dedup_key TEXT")
        except sqlite3.OperationalError:
            pass
        con.execute(
            "INSERT INTO pending_plays(token, sport, match, book, selection, "
            "cote, mise, ev, created_at, dedup_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (token, payload["sport"], payload["match"], payload["book"],
             payload["selection"], payload["cote"], payload["mise"], payload["ev"],
             datetime.now(timezone.utc).isoformat(), payload.get("dedup_key")),
        )
        con.commit()
    finally:
        con.close()
    return token


def _value_bet_play_payload(bet: ValueBet, sport: str | None, bankroll: float) -> dict:
    """Flatten a ValueBet into the row bot_listener.py writes to Excel."""
    parsed = parse_event_key(bet.event_key)
    if parsed is not None:
        _, home_norm, away_norm = parsed
        match = f"{_prettify_team_name(home_norm)} vs {_prettify_team_name(away_norm)}"
    else:
        match = bet.event_key
    line_suffix = f" {bet.outcome.line}" if bet.outcome.line is not None else ""
    book_name = " / ".join(
        _BOOK_NAMES.get(b, b.value) for b in (bet.book, *bet.also_books)
    )
    return {
        "sport": sport or "",
        "match": match,
        "book": book_name,
        "selection": f"{bet.outcome.label}{line_suffix}",
        "cote": round(bet.odd_taken, 3),
        "mise": _advised_stake_eur(bet.ev_pct, bet.kelly_stake_pct, bankroll) or 0.0,
        "ev": round(bet.ev_pct, 2),
        "dedup_key": f"{bet.event_key}|{bet.market.value}|{bet.outcome.label}|{bet.outcome.line}",
    }


def _market_key(dedup_key: str) -> str | None:
    """event|market|line from an event|market|label|line dedup key.

    Identifies the whole betting market, so playing one side silences the
    others: once 1 is backed on a 1X2, alerts on X and 2 are noise — they're
    the opposite side of a position already taken. Same for over/under on a
    given line.

    The line stays in the key on purpose. Over 2.5 and under 3.5 are different
    bets, and deliberately holding both is a middle — which this engine hunts
    for — so collapsing across lines would suppress its own signal."""
    parts = dedup_key.split("|")
    if len(parts) != 4:
        return None
    event, market, _label, line = parts
    return f"{event}|{market}|{line}"


def _load_books_alert_off() -> set[str]:
    """Books dont l'utilisateur a coupé les alertes via /book.

    Relu à chaque construction d'alerter, donc à chaque cycle : une bascule
    depuis Telegram prend effet immédiatement, sans redémarrer le daemon.

    ⚠️ Ne coupe QUE la notification. Les cotes de ces books continuent d'être
    collectées, stockées, suivies en courbe et exportées — c'est tout l'intérêt
    du réglage : faire taire sans jamais cesser de mesurer."""
    try:
        con = sqlite3.connect(str(_PLAYS_DB), timeout=SQLITE_BUSY_TIMEOUT_SEC)
        con.execute(f"PRAGMA busy_timeout={int(SQLITE_BUSY_TIMEOUT_SEC * 1000)}")
        con.execute(
            "CREATE TABLE IF NOT EXISTS book_alerts_off "
            "(book TEXT PRIMARY KEY, disabled_at TEXT NOT NULL)"
        )
        rows = con.execute("SELECT book FROM book_alerts_off").fetchall()
        con.close()
        return {r[0] for r in rows}
    except Exception:
        # En cas de doute on alerte : un book muet par accident est bien plus
        # coûteux qu'une alerte de trop.
        return set()


def _load_channels(print_fn=print) -> list:
    """Les canaux configures en base, relus a chaque construction d'alerter —
    donc a chaque cycle, comme `_load_books_alert_off`.

    Liste VIDE = aucune configuration persistee, et le routage historique
    s'applique tel quel. C'est volontaire : sans canal en base il n'y a rien a
    router, et deduire une configuration a partir des variables .env serait
    exactement la deduction silencieuse qu'on s'interdit. La bascule est donc
    un acte explicite (`scripts.canaux --installer`), et revocable en
    supprimant les lignes.

    Une base sans les tables (jamais rouverte par `Storage`) rend [] elle
    aussi : le daemon continue de router comme avant plutot que de s'arreter.
    """
    class _Source:
        @staticmethod
        def load_channel_rows():
            con = sqlite3.connect(str(_PLAYS_DB), timeout=SQLITE_BUSY_TIMEOUT_SEC)
            con.row_factory = sqlite3.Row
            try:
                return (
                    con.execute("SELECT * FROM channels ORDER BY priorite, nom, id").fetchall(),
                    con.execute("SELECT * FROM channel_rules ORDER BY channel_id, id").fetchall(),
                    con.execute("SELECT * FROM channel_rule_values").fetchall(),
                )
            finally:
                con.close()

    try:
        return charger_canaux(_Source, print_fn=print_fn)
    except Exception:
        return []


def routage_par_canaux_actif() -> bool:
    """Y a-t-il une configuration persistee ? `main` s'en sert pour savoir qui
    tient le dedoublonnage : lui (historique, global) ou `send_value_bet`
    (par canal)."""
    return bool(_load_channels(print_fn=lambda _s: None))


def _load_played_keys() -> tuple[set, set]:
    """(selections, markets) already played — neither should alert again.

    Markets are returned alongside the exact selections so one click silences
    every competing outcome, not just the one that was backed."""
    try:
        con = sqlite3.connect(str(_PLAYS_DB), timeout=SQLITE_BUSY_TIMEOUT_SEC)
        con.execute(f"PRAGMA busy_timeout={int(SQLITE_BUSY_TIMEOUT_SEC * 1000)}")
        con.execute(
            "CREATE TABLE IF NOT EXISTS played_bets (dedup_key TEXT PRIMARY KEY, played_at TEXT)"
        )
        rows = con.execute("SELECT dedup_key FROM played_bets").fetchall()
        con.close()
        keys = {r[0] for r in rows}
        return keys, {mk for mk in (_market_key(k) for k in keys) if mk}
    except Exception:
        return set(), set()


class TelegramAlerter:
    """Thin wrapper around the Telegram Bot API. send_value_bet is best-effort:
    a network failure is logged via the supplied print_fn but doesn't abort
    the scan, since alerting is informational.

    Built-in rate limiting: Telegram throttles bots to ~20 messages/min per
    group and answers a flood with a 429 + retry_after that can reach hours.
    The limiter state is class-level (shared across the per-sport threads and
    the fresh alerter instance each cycle creates) and guarded by a lock:
      - _next_slot paces sends per chat (min_send_interval_s apart),
      - _cooldown_until parks a chat after a 429 until its retry_after elapses,
      - sends that would queue past max_defer_s are dropped for this cycle and
        retried later (the daemon only marks as notified what actually sent)."""

    API_BASE = "https://api.telegram.org"

    # Shared across every instance in the process so parallel sport threads and
    # successive cycles all pace against the same per-chat budget.
    _rl_lock = threading.Lock()
    _next_slot: dict[str, float] = {}        # chat_id -> earliest epoch we may send next
    _cooldown_until: dict[str, float] = {}   # chat_id -> epoch to skip sends until (post-429)

    def __init__(self, config: TelegramConfig, *, client: httpx.Client | None = None,
                 print_fn=print, canaux=None, storage=None):
        self.config = config
        self._client = client or httpx.Client(timeout=10.0)
        self._owns_client = client is None
        self._print = print_fn
        self._played_keys, self._played_markets = _load_played_keys()
        self._books_off = _load_books_alert_off()
        # `canaux=None` lit la base. `canaux=()` FORCE le chemin historique —
        # c'est ainsi que le harnais de comparaison obtient les deux routages
        # depuis la MEME fonction de production, sans en reimplementer aucun.
        self._canaux = _load_channels(print_fn) if canaux is None else list(canaux)
        self._storage = storage

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "TelegramAlerter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def send_value_bet(self, bet: ValueBet, *, sport: str | None = None) -> bool:
        """Route a value bet to the channels it qualifies for. Each channel is
        sent independently (a rate-limited main chat no longer suppresses the
        premium/critical copies, which live on their own chats with their own
        budgets):
          - main chat:     min_ev_pct <= EV < main_max_ev_pct, odd within the main band
          - premium chat:  EV >= min_premium_ev_pct within 1.5-4, or
                           EV >= premium_hi_min_ev within 4-6. No EV ceiling.
          - critical chat: EV >= min_critical_ev_pct at any odds, OR
                           EV >= critical_hi_min_ev above critical_hi_min_odd —
                           but in both cases ONLY when premium did not already
                           take the bet. Critical is the overflow lane for what
                           no premium band accepts, not a second copy of
                           premium: a bet never lands in both.

        Returns True if at least one message was actually delivered, so the
        daemon marks the bet notified only when something went out — a
        rate-limited send leaves it unmarked to be retried next cycle, without
        double-posting a channel that already succeeded (main and premium are
        disjoint EV bands, so a bet normally targets a single channel)."""
        cfg = self.config
        # Market-level, not selection-level: backing 1 on a 1X2 should also
        # silence X and 2 on that match, since they're the other side of a
        # position already held.
        if f"{bet.event_key}|{bet.market.value}|{bet.outcome.line}" in self._played_markets:
            return False
        # Book mis en sourdine via /book. La détection a bien eu lieu et reste
        # en base ; seul l'envoi est supprimé.
        if bet.book.value in self._books_off:
            return False
        # Mi-temps : AUCUN canal, ni principal, ni premium, ni critique.
        #
        # Même principe que la sourdine par book (§21.8) : la détection a lieu,
        # elle est écrite dans `value_bets`, sa clôture est capturée et sa CLV
        # mesurée — seul l'envoi est supprimé. Ces marchés viennent d'être
        # ouverts (§21.14) et leur CLV est inconnue ; les alerter avant de
        # l'avoir mesurée mettrait dans les canaux un flux dont personne ne
        # sait ce qu'il vaut. À lever quand `clv_split --by market` aura tranché.
        if is_half_time(bet.market):
            return False
        ev = bet.ev_pct
        text = format_value_bet(bet, sport=sport, bankroll=cfg.bankroll)
        delivered = False

        # Premium is a prematch-only channel: a value bet whose kickoff has
        # already passed is "live" and must not reach the premium chat (mirrors
        # the prematch-only rule on premium surebets).
        now = datetime.now(timezone.utc)
        # La plus précoce des heures connues. Au tennis, le rapprochement
        # tolère 3 h d'écart entre l'heure de la référence et celle du book —
        # un match déjà commencé selon le book paraîtrait à venir si on ne
        # regardait que la clé réalignée.
        starts = []
        for _k in (bet.event_key, getattr(bet, "book_event_key", None)):
            _p = parse_event_key(_k) if _k else None
            if _p is not None:
                starts.append(_p[0])
        start = min(starts) if starts else None
        is_live = start is not None and start <= now

        # Drop prematch alerts firing in the final minutes before kickoff —
        # those last-minute value bets are disproportionately stale-line/data
        # errors. (Live bets, kickoff already passed, are unaffected.)
        if start is not None and not is_live:
            mins_to_kickoff = (start - now).total_seconds() / 60
            if mins_to_kickoff < cfg.min_minutes_to_kickoff:
                return False

        # ══ Routage par canaux configures ══════════════════════════════
        # Place APRES les securites globales (marche deja joue, book en
        # sourdine, mi-temps, fenetre morte) et apres le calcul de `is_live` :
        # ces garde-fous s'appliquent a tous les canaux et ne sont pas
        # negociables par configuration.

        # ══ Routage par canaux configures ══════════════════════════════
        # Place APRES les securites globales (marche deja joue, book en
        # sourdine, mi-temps, fenetre morte) et apres le calcul de `is_live` :
        # ces garde-fous s'appliquent a tous les canaux et ne sont pas
        # negociables par configuration.
        if self._canaux:
            return self._router(bet, text, sport=sport, is_live=is_live)

        if (
            cfg.min_ev_pct <= ev < cfg.main_max_ev_pct
            and cfg.main_min_odd <= bet.odd_taken <= cfg.main_max_odd
        ):
            delivered |= self._send(text, chat_id=cfg.chat_id)

        _prem_standard = (
            ev >= cfg.min_premium_ev_pct
            and cfg.premium_min_odd <= bet.odd_taken <= cfg.premium_max_odd
        )
        _prem_high_odds = (
            ev >= cfg.premium_hi_min_ev
            and cfg.premium_hi_min_odd <= bet.odd_taken <= cfg.premium_hi_max_odd
            # Un sport exclu ne franchit QUE cette bande-là. La bande standard
            # (cotes 1,5-4) n'est pas touchée : c'est elle qui porte le
            # rendement, +19,92 % sur la tranche 1,8-2,3.
            #
            # ⚠️ `sport is None` NE déclenche PAS l'exclusion. La production
            # passe toujours le sport (`send_alerts(..., sport=current_sport)`),
            # donc le cas ne se présente pas ; mais si un appel l'omettait,
            # écarter par défaut supprimerait des paris d'un sport qu'on n'a
            # jamais voulu couper.
            and (sport or "").lower() not in cfg.premium_hi_sports_exclus
        )
        # Le premium a-t-il vraiment pris ce pari ? La question porte sur la
        # livraison, pas sur la seule éligibilité : si le canal n'est pas
        # configuré, rien n'a été envoyé et le critique doit encore pouvoir
        # rattraper le pari plutôt que de le laisser disparaître en silence.
        _premium_takes = bool(
            not is_live
            and cfg.effective_premium_chat_id
            and (_prem_standard or _prem_high_odds)
        )
        if _premium_takes:
            button = None
            try:
                token = _record_pending_play(
                    _value_bet_play_payload(bet, sport, cfg.bankroll)
                )
                button = {"inline_keyboard": [[{
                    "text": "\u25b6\ufe0f Jouer", "callback_data": f"play:{token}"
                }]]}
            except Exception as e:  # never let bet-logging break alerting
                self._print(f"pending_play record failed: {e}")
            delivered |= self._send(
                "💎 <b>VALUE PREMIUM</b>\n" + text,
                chat_id=cfg.effective_premium_chat_id,
                reply_markup=button,
            )

        # Deux entrees, un seul canal : l'EV extreme quelle que soit la cote,
        # ou une EV plus modeste mais sur une grosse cote que le premium a
        # refusee. Les deux restent barrees par `not _premium_takes`.
        _crit = ev >= cfg.min_critical_ev_pct or (
            ev >= cfg.critical_hi_min_ev and bet.odd_taken > cfg.critical_hi_min_odd
        )
        if (
            not is_live
            and cfg.effective_critical_chat_id
            and _crit
            and not _premium_takes
        ):
            delivered |= self._send(
                "🚨 <b>VALUE BET EXCEPTIONNEL</b>\n" + text, chat_id=cfg.effective_critical_chat_id
            )

        return delivered

    def _bouton_jouer(self, bet: ValueBet, sport: str | None) -> dict | None:
        try:
            token = _record_pending_play(
                _value_bet_play_payload(bet, sport, self.config.bankroll))
        except Exception as e:  # never let bet-logging break alerting
            self._print(f"pending_play record failed: {e}")
            return None
        return {"inline_keyboard": [[{
            "text": "\u25b6\ufe0f Jouer", "callback_data": f"play:{token}"}]]}

    def _base(self) -> Storage:
        if self._storage is None:
            self._storage = Storage(str(_PLAYS_DB))
        return self._storage

    def _doit_notifier(self, bet: ValueBet, chat_id: str) -> bool:
        """Le dedoublonnage, PAR CANAL. Memes reglages qu'avant
        (`valuebet_max_alerts`, `valuebet_dedup`, `valuebet_ev_delta_pct`) et
        memes fonctions de base — seul le perimetre change.

        Un pari deja parti sur le canal Tennis doit pouvoir partir sur le
        canal Grosses Cotes : c'est tout l'objet du commit 1."""
        cfg, st = self.config, self._base()
        cle = (bet.event_key, bet.book.value, bet.market.value,
               bet.outcome.label, bet.outcome.line)
        if st.value_bet_notify_count(*cle, chat_id=chat_id) >= cfg.valuebet_max_alerts:
            return False
        if cfg.valuebet_dedup and st.value_bet_already_notified(
                *cle, current_ev_pct=bet.ev_pct,
                ev_delta_pct=cfg.valuebet_ev_delta_pct, chat_id=chat_id):
            return False
        return True

    def _router(self, bet: ValueBet, text: str, *, sport: str | None,
                is_live: bool) -> bool:
        """Envoie le pari a chaque canal dont les regles correspondent.

        Zero, un ou plusieurs canaux — ils sont independants. Une erreur sur
        un canal n'empeche PAS les suivants : sans ce `try`, un chat_id
        devenu invalide ferait taire toute la liste."""
        cibles = canaux_pour(bet, sport=sport, league=bet.league,
                             is_live=is_live, canaux=self._canaux)
        livre = False
        for canal in cibles:
            entete, avec_bouton = _PRESENTATION.get(canal.nom, ("", False))
            try:
                if not self._doit_notifier(bet, canal.chat_id):
                    continue
                bouton = self._bouton_jouer(bet, sport) if avec_bouton else None
                if self._send(entete + text, chat_id=canal.chat_id,
                              reply_markup=bouton):
                    livre = True
                    self._base().mark_value_bet_notified(
                        bet.event_key, bet.book.value, bet.market.value,
                        bet.outcome.label, bet.outcome.line, bet.ev_pct,
                        datetime.now(timezone.utc), chat_id=canal.chat_id)
            except Exception as e:  # noqa: BLE001
                self._print(f"canal {canal.nom} : envoi impossible ({type(e).__name__}: {e})")
        return livre

    def send_clv_alert(
        self,
        bet: sqlite3.Row,
        clv_pct: float,
        current_pin_odd: float,
        mins_to_kickoff: int,
        *,
        sport: str | None = None,
    ) -> bool:
        # All CLV alerts land in the single CLV channel now; is_high only drives
        # the message header (🔥) so high-CLV confirmations still stand out.
        cfg = self.config
        is_high = clv_pct >= cfg.min_high_clv_pct
        text = format_clv_alert(bet, clv_pct, current_pin_odd, mins_to_kickoff,
                                sport=sport, is_high=is_high, bankroll=cfg.bankroll)
        delivered = self._send(text, chat_id=cfg.effective_clv_chat_id)
        # CLV n'est jamais copiee sur le canal critique (choix utilisateur).
        return delivered

    def send_surebet(self, sb: Surebet, *, sport: str | None = None, is_live: bool = False) -> bool:
        # The suspicious flag is normally a "phantom surebet" canary (matching
        # bug, label mismatch, ...), but the user can opt into seeing them to
        # verify themselves before acting.
        if sb.suspicious and not self.config.include_suspicious_surebets:
            return False
        if sb.margin * 100 < self.config.min_surebet_margin_pct:
            return False
        cfg = self.config
        chat = (
            cfg.effective_live_surebet_chat_id
            if is_live
            else cfg.effective_surebet_chat_id
        )
        text = format_surebet(sb, sport=sport, is_live=is_live)
        margin_pct = sb.margin * 100
        delivered = self._send(text, chat_id=chat)
        # Premium and critical are curated channels: a suspicious (phantom)
        # surebet may reach the main surebet chat for manual review when the
        # user opted in, but it must never pollute premium/critical.
        # Critical copy: any surebet (prematch or live) above the critical margin.
        if (
            not sb.suspicious
            and not is_live
            and cfg.effective_critical_chat_id
            and margin_pct >= cfg.min_critical_surebet_pct
        ):
            delivered |= self._send(
                "🚨 <b>SUREBET EXCEPTIONNEL</b>\n" + text, chat_id=cfg.effective_critical_chat_id
            )
        # Aucune copie premium, quel que soit le pourcentage : les surebets ont
        # leur canal et n'ont rien à faire dans un canal de value bets. Le
        # premium reste réservé aux value bets, ce qui le rend lisible d'un coup
        # d'œil — un surebet et un value bet ne se jouent pas de la même façon.
        return delivered

    def send_middle(self, m: Middle, *, sport: str | None = None) -> bool:
        """Send a totals-middle alert to the CLV channel (per user choice).
        Best-effort like the others: a rate-limited send returns False and the
        daemon retries next cycle."""
        text = format_middle(m, sport=sport, total_stake=self.config.middle_stake_eur)
        return self._send(text, chat_id=self.config.effective_clv_chat_id)

    @staticmethod
    def _retry_after_seconds(r: httpx.Response) -> float:
        """Pull Telegram's requested back-off from a 429 response."""
        try:
            params = r.json().get("parameters", {})
            return float(params.get("retry_after", 30))
        except Exception:
            hdr = r.headers.get("Retry-After")
            try:
                return float(hdr) if hdr else 30.0
            except (TypeError, ValueError):
                return 30.0

    def _reserve_slot(self, chat_id: str) -> float | None:
        """Reserve the next send slot for a chat under the shared lock. Returns
        the epoch at which the caller may send, or None to skip this send (the
        chat is in a 429 cooldown, or its queue is backed up past max_defer_s)."""
        with TelegramAlerter._rl_lock:
            now = _time.time()
            cooldown = TelegramAlerter._cooldown_until.get(chat_id, 0.0)
            if now < cooldown:
                self._print(
                    f"Telegram cooldown {int(cooldown - now)}s restant [chat={chat_id}] — message reporté"
                )
                return None
            slot = max(now, TelegramAlerter._next_slot.get(chat_id, 0.0))
            if slot - now > self.config.max_defer_s:
                # Too many queued for this chat right now — leave it unsent so the
                # daemon doesn't mark it notified; it'll be retried next cycle.
                return None
            TelegramAlerter._next_slot[chat_id] = slot + self.config.min_send_interval_s
            return slot

    def _send(self, text: str, chat_id: str, reply_markup: dict | None = None) -> bool:
        slot = self._reserve_slot(chat_id)
        if slot is None:
            return False
        wait = slot - _time.time()
        if wait > 0:
            _time.sleep(wait)
        try:
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": self.config.parse_mode,
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            r = self._client.post(
                f"{self.API_BASE}/bot{self.config.bot_token}/sendMessage",
                json=payload,
            )
            if r.status_code == 429:
                retry_after = self._retry_after_seconds(r)
                with TelegramAlerter._rl_lock:
                    TelegramAlerter._cooldown_until[chat_id] = _time.time() + retry_after
                self._print(
                    f"Telegram 429 [chat={chat_id}] — pause {int(retry_after)}s avant le prochain envoi"
                )
                return False
            if r.status_code != 200:
                self._print(f"Telegram non-200 ({r.status_code}) [chat={chat_id}]: {r.text[:200]}")
                return False
            return True
        except httpx.HTTPError as e:
            self._print(f"Telegram send failed: {e}")
            return False


def _duree(x, unite="s") -> str:
    """Une duree, ou N/A. JAMAIS zero a la place d'une valeur inconnue."""
    return "N/A" if x is None else f"{x:.1f} {unite}"


#: Au-dela, on le dit dans le message. Ce n'est pas un filtre — le moteur a
#: deja tranche en amont ; c'est un avertissement pour l'oeil humain qui lira
#: l'alerte, parce qu'une fraicheur mediocre ne se voit pas dans une EV.
FRAICHEUR_SUSPECTE_SEC = 30.0


def format_live_observation(o) -> str:
    """Une occasion LIVE, en observation. AUCUN bouton, aucune action.

    Le score vient d'AsianOdds et de nulle part ailleurs : c'est le score qui
    a servi a fabriquer la fair utilisee dans ce calcul. L'afficher avec son
    AGE n'est pas decoratif — une fair de 90 s sur un match en direct peut
    porter un etat de jeu revolu, et c'est la seule chose qui permet a un
    lecteur de s'en apercevoir.
    """
    from html import escape
    match = f"{_prettify_team_name(o.home)} vs {_prettify_team_name(o.away)}"
    score = (o.feed_score or "").replace(":", "-") or "N/A"
    ligne = "" if o.line is None else f" {o.line:g}"
    minute = ("" if o.minute_ecoulee is None
              else f"  🕐 {o.minute_ecoulee:.0f} min")
    parties = [
        "🧪 <b>LIVE OBSERVATION</b>  —  aucune action, aucun pari",
        "",
        f"⚽ <b>{escape(match)}</b>",
        f"⚽ <b>Score : {score}</b>{minute}  (AsianOdds, il y a "
        f"{_duree(o.age_fair_sec)})",
        "",
        f"📊 Marché : <b>{o.market.value}{ligne}</b>",
        f"🎯 Sélection : <b>{escape(o.outcome)}</b>",
        "",
        f"💰 Cote {o.book.value} : <b>{o.cote_preneur:.2f}</b>",
        f"📐 Fair AsianOdds : <b>{o.fair_cote:.2f}</b>",
        f"📈 EV : <b>{o.ev_pct:+.1f} %</b>",
        "",
        f"⏱ Âge fair AsianOdds : {_duree(o.age_fair_sec)}",
        f"⏱ Âge cote {o.book.value} : {_duree(o.age_preneur_sec)}",
        f"⚡ Temps de détection : {_duree(o.delai_calcul_sec)}",
    ]
    # Une jambe absente n'invalide pas le calcul, mais le lecteur doit le
    # savoir : le prix affiche peut etre celui d'un marche en cours de
    # suspension.
    if o.partiel:
        parties.append(f"⚠️ <b>Marché partiel</b> — issue(s) manquante(s) : "
                       f"{escape(', '.join(o.issues_manquantes))}")
    vieux = [n for n, v in (("fair AsianOdds", o.age_fair_sec),
                            (f"cote {o.book.value}", o.age_preneur_sec))
             if v is not None and v > FRAICHEUR_SUSPECTE_SEC]
    if vieux:
        parties.append(f"⚠️ <b>Fraîcheur limite</b> : {escape(', '.join(vieux))}")
    if o.statut.value.startswith("REJET_"):
        parties.append(f"ℹ️ statut : {escape(o.statut.value)}"
                       + (f" — {escape(o.motif)}" if o.motif else ""))
    return "\n".join(parties)


def send_live_observation(opportunites, config: "TelegramConfig | None",
                          *, alerter=None, log=print) -> int:
    """Envoyer les observations LIVE. Renvoie le nombre de messages partis.

    ⚠️ REFUSE D'ENVOYER si aucun canal LIVE dedie n'est configure. Ce n'est
    pas de la prudence excessive : `effective_live_surebet_chat_id` retombe
    silencieusement sur le canal des surebets prematch, donc sans ce refus,
    un `TELEGRAM_LIVE_SUREBET_CHAT_ID` absent enverrait les observations LIVE
    dans le canal prematch — exactement ce qu'on a interdit.

    Aucun `reply_markup` n'est passe : pas de bouton, donc aucune action
    bookmaker possible depuis le message.
    """
    if config is None or not config.bot_token:
        log("[live] Telegram non configuré — aucune alerte")
        return 0
    if not config.live_surebet_chat_id:
        log("[live] TELEGRAM_LIVE_SUREBET_CHAT_ID non défini — AUCUN envoi. "
            "Le repli irait vers le canal prématch.")
        return 0
    chat = config.live_surebet_chat_id
    envoi = alerter or TelegramAlerter(config)
    n = 0
    for o in opportunites:
        if envoi._send(format_live_observation(o), chat):
            n += 1
    return n


def _edge_key(q) -> tuple:
    """Identifie une cote dans la table des écarts."""
    return (q.market.value, q.outcome.label, q.outcome.line)


def format_late_market(event_key: str, book: Book, quotes: list,
                       minutes_late: float, sport: str | None = None,
                       score: tuple | None = None, is_goal: bool = False,
                       edges: dict | None = None) -> str:
    """Message d'un marché prématch resté ouvert sur un match commencé.

    `edges` donne l'écart mesuré, par cote, contre le consensus des books qui
    pricent déjà en direct. C'est la seule chose qui distingue une occasion
    d'un book simplement lent, donc elle passe en tête du message."""
    parsed = parse_event_key(event_key)
    if parsed is not None:
        _, home_norm, away_norm = parsed
        matchup = (f"{_ht(_prettify_team_name(home_norm))} vs "
                   f"{_ht(_prettify_team_name(away_norm))}")
    else:
        matchup = _ht(event_key)
    edges = edges or {}
    # Une ligne par issue retenue, la plus grosse d'abord : c'est celle-là
    # qu'on joue. Le marché seul (« totals 2.5 ») ne dit ni de quel côté ni
    # combien, ce qui obligeait à rouvrir le book pour le savoir.
    lines = []
    for q in sorted(quotes, key=lambda x: -(edges.get(_edge_key(x), 0.0))):
        label = q.outcome.label + (f" {q.outcome.line:g}"
                                   if q.outcome.line is not None else "")
        gap = edges.get(_edge_key(q))
        gap_txt = f"  <b>+{gap:.0f}%</b> vs live" if gap is not None else ""
        lines.append(f"• {_ht(q.market.value)} <b>{_ht(label)}</b> "
                     f"@ {q.decimal_odd:.2f}{gap_txt}")
    best = max(edges.values(), default=None)
    # Le score change tout : « les deux équipes marquent » sur un 1-1 est déjà
    # gagné, et c'est invisible sans cette ligne. Absent quand le flux live
    # Betano ne couvre pas ce match — on le dit plutôt que de laisser croire
    # à un 0-0.
    if score is not None:
        h, a, minute = score
        score_line = f"⚽ <b>Score : {_ht(h)}-{_ht(a)}</b>  ({_ht(minute)}')\n"
    else:
        score_line = "❔ Score inconnu — vérifie avant de jouer.\n"
    titre = ("🥅 <b>BUT — MARCHÉ TOUJOURS OUVERT</b>" if is_goal
             else "⏱️ <b>MARCHÉ EN RETARD</b>")
    return (
        f"{titre} — {_nom_book(book)}\n"
        f"{_sport_prefix(sport)}{matchup}\n"
        f"{score_line}"
        f"Commencé depuis <b>{minutes_late:.0f} min</b> selon Pinnacle, "
        f"encore proposé en prématch.\n"
        + (f"📈 Écart max <b>+{best:.0f}%</b> contre le prix live des autres books\n"
           if best is not None else "")
        + "\n".join(lines) + "\n"
        + "⚠️ Vérifie le score avant de jouer — et sache que le book peut "
        "annuler un pari pris sur un marché qu'il aurait dû suspendre."
    )


def send_late_market_alerts(
    items: list[tuple], config: TelegramConfig | None,
    *, print_fn=print, sport: str | None = None,
) -> list[tuple]:
    """Router les marchés en retard vers le canal critique.

    Canal critique et non premium : ce n'est pas un value bet mesuré contre une
    ligne juste, c'est une erreur d'exploitation du book. Le mélanger aux
    alertes de value fausserait le CLV, qui est la mesure d'edge du système.

    Chaque élément : (event_key, book, quotes, minutes_late, score, is_goal),
    où `score` vaut (dom, ext, minute) ou None si le flux live ne couvre pas ce
    match. Les deux derniers champs sont optionnels : un appelant qui n'a pas de
    score envoie un quadruplet et le message le dit. Renvoie les éléments
    réellement envoyés, pour que l'appelant ne marque que ceux-là."""
    if config is None or not items:
        return []
    chat = config.effective_critical_chat_id
    if not chat:
        return []
    sent: list[tuple] = []
    with TelegramAlerter(config, print_fn=print_fn) as alerter:
        for item in items:
            ek, book, quotes, late = item[:4]
            score = item[4] if len(item) > 4 else None
            is_goal = bool(item[5]) if len(item) > 5 else False
            edges = item[6] if len(item) > 6 else None
            text = format_late_market(ek, book, quotes, late, sport=sport,
                                      score=score, is_goal=is_goal, edges=edges)
            if alerter._send(text, chat_id=chat):
                sent.append(item)
    return sent


def send_alerts(bets: list[ValueBet], config: TelegramConfig | None,
                *, print_fn=print, sport: str | None = None) -> list[ValueBet]:
    """Fire a Telegram message for each bet that clears the EV threshold.
    Returns the bets actually delivered (so the caller marks only those as
    notified — a rate-limited/failed send stays unmarked and is retried).
    No-op if config is None (env not set)."""
    if config is None or not bets:
        return []
    sent: list[ValueBet] = []
    with TelegramAlerter(config, print_fn=print_fn) as alerter:
        for b in bets:
            if alerter.send_value_bet(b, sport=sport):
                sent.append(b)
    return sent


def send_surebet_alerts(
    surebets: list[Surebet], config: TelegramConfig | None,
    *, print_fn=print, sport: str | None = None,
) -> list[Surebet]:
    """Same shape as send_alerts but for surebets. Routes each surebet to the
    prematch or live Telegram channel based on whether the event's kickoff time
    has already passed. Suspicious surebets and sub-threshold margins are
    silently skipped. Returns the surebets actually delivered."""
    if config is None or not surebets:
        return []
    now = datetime.now(timezone.utc)
    sent: list[Surebet] = []
    with TelegramAlerter(config, print_fn=print_fn) as alerter:
        for sb in surebets:
            parsed = parse_event_key(sb.event_key)
            is_live = parsed is not None and parsed[0] <= now
            # Drop prematch surebets firing within N min of kickoff — those
            # last-minute arbs are usually stale-line/data errors, not real.
            if parsed is not None and not is_live:
                mins_to_kickoff = (parsed[0] - now).total_seconds() / 60
                if mins_to_kickoff < config.surebet_min_minutes_to_kickoff:
                    continue
            if alerter.send_surebet(sb, sport=sport, is_live=is_live):
                sent.append(sb)
    return sent


def send_middle_alerts(
    middles: list[Middle], config: TelegramConfig | None,
    *, print_fn=print, sport: str | None = None,
) -> list[Middle]:
    """Send a totals-middle alert per middle to the CLV channel. Prematch-only:
    middles on events whose kickoff has passed (live) or that fire within
    min_minutes_to_kickoff are dropped — in-play/last-minute totals are the
    stale-line/data-error zone, same rule as surebets. Returns the middles
    actually delivered (so only those get marked notified)."""
    if config is None or not middles:
        return []
    now = datetime.now(timezone.utc)
    sent: list[Middle] = []
    with TelegramAlerter(config, print_fn=print_fn) as alerter:
        for m in middles:
            parsed = parse_event_key(m.event_key)
            if parsed is not None:
                kickoff = parsed[0]
                if kickoff <= now:
                    continue  # live — skip
                mins_to_kickoff = (kickoff - now).total_seconds() / 60
                if mins_to_kickoff < config.surebet_min_minutes_to_kickoff:
                    continue
            if alerter.send_middle(m, sport=sport):
                sent.append(m)
    return sent


def send_clv_alerts(
    clv_items: list[tuple],  # (bet_row, clv_pct, current_pin_odd, mins_to_kickoff)
    config: TelegramConfig | None,
    *,
    print_fn=print,
    sport: str | None = None,
) -> list[tuple]:
    """Send one CLV confirmation alert per near-kickoff value bet. Returns the
    clv_items actually delivered. No-op if config is None or the list is empty."""
    if config is None or not clv_items:
        return []
    sent: list[tuple] = []
    with TelegramAlerter(config, print_fn=print_fn) as alerter:
        for item in clv_items:
            bet, clv_pct, pin_odd, mins = item
            if alerter.send_clv_alert(bet, clv_pct, pin_odd, mins, sport=sport):
                sent.append(item)
    return sent


def send_system_alert(config: TelegramConfig | None, text: str,
                      *, print_fn=print) -> bool:
    """Alerte d'exploitation — panne d'un composant, pas une opportunité.

    Part sur le canal MAINTENANCE quand il existe, sinon le critique, sinon le
    chat principal : une panne de la référence sharp mérite d'interrompre, pas
    de se noyer dans le flux des value bets. Le seul incident de ce type
    observé a duré 1 h 30 sans que rien ne le signale, et chaque heure sans
    Pinnacle est une heure de lignes de clôture perdues pour de bon — donc de
    CLV non mesurable.

    La cascade n'a pas de sortie vide : une alerte de panne qui n'atterrit
    nulle part vaut exactement l'incident silencieux qu'elle existe pour
    éviter."""
    if config is None:
        return False
    chat = config.effective_maintenance_chat_id
    if not chat:
        return False
    with TelegramAlerter(config, print_fn=print_fn) as alerter:
        return alerter._send(text, chat_id=chat)
