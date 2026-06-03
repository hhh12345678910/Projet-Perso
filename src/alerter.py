from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from .models import ValueBet


@dataclass
class TelegramConfig:
    bot_token: str
    chat_id: str
    min_ev_pct: float = 3.0          # silence sub-threshold noise
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
        )


def format_value_bet(bet: ValueBet) -> str:
    """Single message per bet. Kept compact so a phone notification preview
    shows the essentials (EV%, odd, book) without needing to expand the chat."""
    line = f" {bet.outcome.line}" if bet.outcome.line is not None else ""
    return (
        f"🎯 <b>+{bet.ev_pct:.2f}% EV</b>  {bet.book.value}\n"
        f"<code>{bet.event_key}</code>\n"
        f"{bet.market.value} — <b>{bet.outcome.label}{line}</b> @ {bet.odd_taken:.2f}\n"
        f"fair {bet.fair_odd:.2f}  ·  Kelly {bet.kelly_stake_pct:.2f}%"
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
        try:
            r = self._client.post(
                f"{self.API_BASE}/bot{self.config.bot_token}/sendMessage",
                json={
                    "chat_id": self.config.chat_id,
                    "text": format_value_bet(bet),
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
