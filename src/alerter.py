from __future__ import annotations

import os
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
    Book.UNIBET_BE: "Unibet",
    Book.BETANO_BE: "Betano",
    Book.BETFIRST: "BetFirst",
    Book.LADBROKES_BE: "Ladbrokes",
    Book.GOLDEN_PALACE: "Golden Palace",
    Book.STARCASINO_SPORT: "StarCasino",
    Book.MAGIC_BETTING: "Magic Betting",
    Book.CIRCUS_BE: "Circus",
    Book.BETCENTER: "Betcenter",
    Book.SMARKETS: "Smarkets",
}


def _prettify_team_name(normalized: str) -> str:
    """Best-effort title-case of the normalized event-key fragment. Spaces are
    stripped during normalize_team so multi-word names like 'manchestercity'
    can't be recovered cleanly — single-word names look fine, compound ones
    stay as-is in title case (good enough for an alert preview)."""
    if not normalized:
        return ""
    return normalized.capitalize()


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
    chat_id: str
    min_ev_pct: float = 3.0              # value bets below this stay silent
    min_surebet_margin_pct: float = 1.0  # surebets below this margin stay silent
    include_suspicious_surebets: bool = False  # opt-in to see flagged ones too
    surebet_dedup: bool = True           # off -> alert every scan even if seen before
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
            min_ev_pct=float(os.getenv("TELEGRAM_MIN_EV", "3.0")),
            min_surebet_margin_pct=float(os.getenv("TELEGRAM_MIN_SUREBET", "1.0")),
            include_suspicious_surebets=os.getenv("TELEGRAM_INCLUDE_SUSPICIOUS", "0") == "1",
            surebet_dedup=os.getenv("TELEGRAM_SUREBET_DEDUP", "1") == "1",
        )


def format_surebet(sb: Surebet) -> str:
    """Surebet messages need to list every leg with its book — that's the
    whole point — so the format is taller than a value bet alert. Visually
    distinct (💰 vs 🎯) so the user can tell them apart in the chat preview.
    Suspicious flagged surebets are prefixed with ⚠️ so the user knows the
    pipeline thinks this is probably a phantom — verify before acting."""
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
    header_emoji = "⚠️ <b>SUREBET SUSPECT</b>" if sb.suspicious else "💰 <b>SUREBET</b>"
    suspect_footer = (
        "\n<i>⚠️ Marge inhabituelle — vérifie les équipes et les cotes "
        "avant de jouer.</i>"
        if sb.suspicious else ""
    )

    return (
        f"{header_emoji} +{sb.margin * 100:.2f}% (ROI {sb.roi * 100:.2f}%)\n"
        f"{matchup} — {sb.market.value}{line_suffix}\n"
        f"{when_line}"
        f"{legs_lines}"
        f"{suspect_footer}"
    )


def format_value_bet(bet: ValueBet) -> str:
    """Human-readable single message per bet — kept compact so a phone
    notification preview shows EV%, opponent and kickoff before the user
    needs to expand the chat. Dates are localised to Brussels time."""
    book_name = _BOOK_NAMES.get(bet.book, bet.book.value)

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
        f"{matchup}\n"
        f"{when_line}"
        f"Pari : <b>{bet.outcome.label}{line_suffix}</b> @ {bet.odd_taken:.2f} (fair {bet.fair_odd:.2f})\n"
        f"Mise conseillée : {bet.kelly_stake_pct:.2f}%"
    )


class TelegramAlerter:
    """Thin wrapper around the Telegram Bot API. send_value_bet is best-effort:
    a network failure is logged via the supplied print_fn but doesn't abort
    the scan, since alerting is informational."""

    API_BASE = "https://api.telegram.org"

    def __init__(self, config: TelegramConfig, *, client: httpx.Client | None = None,
                 print_fn=print):
        self.config = config
        self._client = client or httpx.Client(timeout=10.0)
        self._owns_client = client is None
        self._print = print_fn

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "TelegramAlerter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def send_value_bet(self, bet: ValueBet) -> bool:
        if bet.ev_pct < self.config.min_ev_pct:
            return False
        return self._send(format_value_bet(bet))

    def send_surebet(self, sb: Surebet) -> bool:
        # The suspicious flag is normally a "phantom surebet" canary (matching
        # bug, label mismatch, ...), but the user can opt into seeing them to
        # verify themselves before acting.
        if sb.suspicious and not self.config.include_suspicious_surebets:
            return False
        if sb.margin * 100 < self.config.min_surebet_margin_pct:
            return False
        return self._send(format_surebet(sb))

    def _send(self, text: str) -> bool:
        try:
            r = self._client.post(
                f"{self.API_BASE}/bot{self.config.bot_token}/sendMessage",
                json={
                    "chat_id": self.config.chat_id,
                    "text": text,
                    "parse_mode": self.config.parse_mode,
                    "disable_web_page_preview": True,
                },
            )
            if r.status_code != 200:
                self._print(f"Telegram non-200 ({r.status_code}): {r.text[:200]}")
                return False
            return True
        except httpx.HTTPError as e:
            self._print(f"Telegram send failed: {e}")
            return False


def send_alerts(bets: list[ValueBet], config: TelegramConfig | None,
                *, print_fn=print) -> int:
    """Fire a Telegram message for each bet that clears the EV threshold.
    Returns the number actually sent. No-op if config is None (env not set)."""
    if config is None or not bets:
        return 0
    sent = 0
    with TelegramAlerter(config, print_fn=print_fn) as alerter:
        for b in bets:
            if alerter.send_value_bet(b):
                sent += 1
    return sent


def send_surebet_alerts(
    surebets: list[Surebet], config: TelegramConfig | None,
    *, print_fn=print,
) -> int:
    """Same shape as send_alerts but for surebets. Suspicious surebets and
    sub-threshold margins are silently skipped — only plausible
    above-threshold opportunities make it to the chat."""
    if config is None or not surebets:
        return 0
    sent = 0
    with TelegramAlerter(config, print_fn=print_fn) as alerter:
        for sb in surebets:
            if alerter.send_surebet(sb):
                sent += 1
    return sent
