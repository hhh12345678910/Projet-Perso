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

from .matcher import parse_event_key
from .models import Book, ValueBet
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


def _prettify_team_name(normalized: str) -> str:
    """Resolve a space-stripped event-key fragment back to its human-readable
    name via the teams registry (populated by every scraper when it sees an
    original team string). Falls back to the old title-case behaviour when
    the registry doesn't know the team yet — typically only on the very
    first scan of an event before a scraper records it."""
    return teams.display(normalized)


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
    min_minutes_to_kickoff: int = 15      # drop prematch value/surebet alerts firing within N min of kickoff (stale-line errors)
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
    min_critical_ev_pct: float = 35.0    # value bets above this also go to critical channel
    min_critical_surebet_pct: float = 10.0  # surebets (margin%) above this also go to critical
    min_critical_clv_pct: float = 25.0   # CLV% above this also goes to critical channel
    # Premium channel — curated "best opportunities" copy: big value bets (incl.
    # the suspiciously-huge ones) within a sane odds band, plus juicy prematch
    # surebets. Opt-in via TELEGRAM_PREMIUM_CHAT_ID; no fallback when unset.
    premium_chat_id: str | None = None
    min_premium_ev_pct: float = 8.0      # value bets at/above this go to premium channel
    min_premium_surebet_pct: float = 5.0  # prematch surebets (margin%) at/above this go to premium
    premium_min_odd: float = 1.5         # premium value bets only within this odds band
    premium_max_odd: float = 4.0
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
            premium_chat_id=os.getenv("TELEGRAM_PREMIUM_CHAT_ID") or None,
            min_premium_ev_pct=float(os.getenv("TELEGRAM_MIN_PREMIUM_EV", "8.0")),
            min_premium_surebet_pct=float(os.getenv("TELEGRAM_MIN_PREMIUM_SUREBET", "5.0")),
            premium_min_odd=float(os.getenv("TELEGRAM_PREMIUM_MIN_ODD", "1.5")),
            premium_max_odd=float(os.getenv("TELEGRAM_PREMIUM_MAX_ODD", "4.0")),
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
        matchup = f"{_prettify_team_name(home_norm)} vs {_prettify_team_name(away_norm)}"
        when_line = f"📅 {_format_kickoff(start)}\n"
    else:
        matchup = sb.event_key
        when_line = ""

    line_suffix = f" {sb.line}" if sb.line is not None else ""
    legs_lines = "\n".join(
        f"  • <b>{label}</b> @ {odd:.2f} — {_BOOK_NAMES.get(book, book.value)}"
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
        f"{_sport_prefix(sport)}{matchup} — {sb.market.value}{line_suffix}\n"
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
        matchup = f"{_prettify_team_name(home_norm)} vs {_prettify_team_name(away_norm)}"
        when_line = f"📅 {_format_kickoff(start)}\n"
    else:
        matchup = m.event_key
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
        f"  • <b>Over {m.low_line:g}</b> @ {o_odd:.2f} — {_BOOK_NAMES.get(o_book, o_book.value)}  → {s_over:.0f}€\n"
        f"  • <b>Under {m.high_line:g}</b> @ {u_odd:.2f} — {_BOOK_NAMES.get(u_book, u_book.value)}  → {s_under:.0f}€\n"
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
    bankroll sert à convertir le Kelly% stocké en mise conseillée en €."""
    from .models import Book as _Book  # local import to avoid circular at module level
    parsed = parse_event_key(bet["event_key"])
    if parsed is not None:
        start, home_norm, away_norm = parsed
        matchup = f"{_prettify_team_name(home_norm)} vs {_prettify_team_name(away_norm)}"
        when_line = f"📅 {_format_kickoff(start)} (dans {mins_to_kickoff} min)\n"
    else:
        matchup = bet["event_key"]
        when_line = f"📅 Dans {mins_to_kickoff} min\n"

    try:
        book_name = _BOOK_NAMES.get(_Book(bet["book"]), bet["book"])
    except ValueError:
        book_name = bet["book"]

    header = (
        f"🔥 <b>CLV ÉLEVÉ {clv_pct:+.2f}% confirmé</b> — {book_name}"
        if is_high else
        f"⏰ <b>CLV {clv_pct:+.2f}% confirmé</b> — {book_name}"
    )
    line_suffix = f" {bet['line']}" if bet["line"] is not None else ""

    kelly_pct = _clv_bet_kelly_pct(bet)
    if kelly_pct is not None:
        kelly_pct = min(kelly_pct, _MAX_STAKE_PCT)
        stake_eur = _round_stake(kelly_pct / 100.0 * bankroll)
        stake_line = f"\nMise conseillée : {stake_eur:.0f}€ ({kelly_pct:.2f}% de {bankroll:.0f}€)"
    else:
        stake_line = ""

    return (
        f"{header}\n"
        f"{_sport_prefix(sport)}{matchup}\n"
        f"{when_line}"
        f"Pari : <b>{bet['outcome_label']}{line_suffix}</b> @ {float(bet['odd_taken']):.2f}\n"
        f"Pinnacle actuel : {current_pin_odd:.2f}"
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
        _BOOK_NAMES.get(b, b.value) for b in (bet.book, *bet.also_books)
    )

    # Try to extract a readable home/away + kickoff from the event_key.
    parsed = parse_event_key(bet.event_key)
    if parsed is not None:
        start, home_norm, away_norm = parsed
        matchup = f"{_prettify_team_name(home_norm)} vs {_prettify_team_name(away_norm)}"
        when_line = f"📅 {_format_kickoff(start)}\n"
    else:
        # Fall back to the raw key if it doesn't parse — better than crashing.
        matchup = bet.event_key
        when_line = ""

    line_suffix = f" {bet.outcome.line}" if bet.outcome.line is not None else ""

    return (
        f"🎯 <b>+{bet.ev_pct:.2f}% EV</b> — {book_name}\n"
        f"{_sport_prefix(sport)}{matchup}\n"
        f"{when_line}"
        f"Pari : <b>{bet.outcome.label}{line_suffix}</b> @ {bet.odd_taken:.2f} (fair {bet.fair_odd:.2f})\n"
        f"Mise conseillée : {_round_stake(min(bet.kelly_stake_pct, _MAX_STAKE_PCT) / 100.0 * bankroll):.0f}€ "
        f"({min(bet.kelly_stake_pct, _MAX_STAKE_PCT):.2f}% de {bankroll:.0f}€)"
    )


_PLAYS_DB = Path(__file__).resolve().parent.parent / "data" / "valuebet.db"


def _record_pending_play(payload: dict) -> str:
    """Persist the data needed to log a played bet, keyed by a short token that
    rides in the Telegram button's callback_data. bot_listener.py reads this
    table when the user taps Jouer. Kept separate from value_bets because the
    alerter doesn't carry the inserted row id."""
    import uuid

    token = uuid.uuid4().hex[:12]
    con = sqlite3.connect(str(_PLAYS_DB))
    try:
        con.execute("PRAGMA busy_timeout=5000")
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
        "mise": _round_stake(min(bet.kelly_stake_pct, _MAX_STAKE_PCT) / 100.0 * bankroll),
        "ev": round(bet.ev_pct, 2),
        "dedup_key": f"{bet.event_key}|{bet.market.value}|{bet.outcome.label}|{bet.outcome.line}",
    }


def _load_played_keys() -> set:
    """Selections deja jouees (clic Jouer) a ne plus alerter, tous books/cotes."""
    try:
        con = sqlite3.connect(str(_PLAYS_DB))
        con.execute("PRAGMA busy_timeout=3000")
        con.execute(
            "CREATE TABLE IF NOT EXISTS played_bets (dedup_key TEXT PRIMARY KEY, played_at TEXT)"
        )
        rows = con.execute("SELECT dedup_key FROM played_bets").fetchall()
        con.close()
        return {r[0] for r in rows}
    except Exception:
        return set()


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
                 print_fn=print):
        self.config = config
        self._client = client or httpx.Client(timeout=10.0)
        self._owns_client = client is None
        self._print = print_fn
        self._played_keys = _load_played_keys()

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
          - premium chat:  EV >= min_premium_ev_pct, odd within the premium band
          - critical chat: EV >= min_critical_ev_pct

        Returns True if at least one message was actually delivered, so the
        daemon marks the bet notified only when something went out — a
        rate-limited send leaves it unmarked to be retried next cycle, without
        double-posting a channel that already succeeded (main and premium are
        disjoint EV bands, so a bet normally targets a single channel)."""
        cfg = self.config
        if f"{bet.event_key}|{bet.market.value}|{bet.outcome.label}|{bet.outcome.line}" in self._played_keys:
            return False
        ev = bet.ev_pct
        text = format_value_bet(bet, sport=sport)
        delivered = False

        # Premium is a prematch-only channel: a value bet whose kickoff has
        # already passed is "live" and must not reach the premium chat (mirrors
        # the prematch-only rule on premium surebets).
        now = datetime.now(timezone.utc)
        parsed = parse_event_key(bet.event_key)
        is_live = parsed is not None and parsed[0] <= now

        # Drop prematch alerts firing in the final minutes before kickoff —
        # those last-minute value bets are disproportionately stale-line/data
        # errors. (Live bets, kickoff already passed, are unaffected.)
        if parsed is not None and not is_live:
            mins_to_kickoff = (parsed[0] - now).total_seconds() / 60
            if mins_to_kickoff < cfg.min_minutes_to_kickoff:
                return False

        if (
            cfg.min_ev_pct <= ev < cfg.main_max_ev_pct
            and cfg.main_min_odd <= bet.odd_taken <= cfg.main_max_odd
        ):
            delivered |= self._send(text, chat_id=cfg.chat_id)

        if (
            not is_live
            and cfg.effective_premium_chat_id
            and ev >= cfg.min_premium_ev_pct
            and cfg.premium_min_odd <= bet.odd_taken <= cfg.premium_max_odd
        ):
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

        if not is_live and cfg.effective_critical_chat_id and ev >= cfg.min_critical_ev_pct:
            delivered |= self._send(
                "🚨 <b>VALUE BET EXCEPTIONNEL</b>\n" + text, chat_id=cfg.effective_critical_chat_id
            )

        return delivered

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
        # Premium copy: prematch-only surebets above the premium margin.
        if (
            not sb.suspicious
            and not is_live
            and cfg.effective_premium_chat_id
            and cfg.min_premium_surebet_pct <= margin_pct < cfg.min_critical_surebet_pct
        ):
            delivered |= self._send(
                "💎 <b>SUREBET PREMIUM</b>\n" + text, chat_id=cfg.effective_premium_chat_id
            )
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
                if mins_to_kickoff < config.min_minutes_to_kickoff:
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
                if mins_to_kickoff < config.min_minutes_to_kickoff:
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
