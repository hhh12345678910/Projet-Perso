from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import httpx
import pytest

from src.alerter import (
    TelegramAlerter,
    TelegramConfig,
    format_surebet,
    format_value_bet,
    send_alerts,
    send_surebet_alerts,
)
from src.models import Book, MarketType, Outcome, ValueBet
from src.surebet import Surebet


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """The TelegramAlerter rate-limit state is class-level (shared across the
    process in production). Clear it between tests so per-chat slot reservations
    and 429 cooldowns from one test never bleed into the next."""
    TelegramAlerter._next_slot.clear()
    TelegramAlerter._cooldown_until.clear()
    yield
    TelegramAlerter._next_slot.clear()
    TelegramAlerter._cooldown_until.clear()


NOW = datetime(2026, 5, 28, tzinfo=timezone.utc)


def _bet(ev_pct: float = 5.0, label: str = "home", line: float | None = None,
         odd: float = 1.86, event_key: str = "209906010000::boise__vs__sarasota") -> ValueBet:
    # Default kickoff is far in the future so the bet counts as prematch
    # regardless of when the suite runs (premium routing is prematch-only).
    return ValueBet(
        event_key=event_key,
        book=Book.UNIBET_BE,
        market=MarketType.H2H,
        outcome=Outcome(label=label, line=line),
        odd_taken=odd,
        fair_prob=0.5650,
        fair_odd=1.77,
        ev_pct=ev_pct,
        kelly_stake_pct=1.50,
        detected_at=NOW,
    )


def test_config_from_env_returns_none_when_unset(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert TelegramConfig.from_env() is None


def test_config_from_env_reads_credentials(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    monkeypatch.setenv("TELEGRAM_MIN_EV", "4.5")
    c = TelegramConfig.from_env()
    assert c is not None
    assert c.bot_token == "tok123"
    assert c.chat_id == "42"
    assert c.min_ev_pct == 4.5


def test_format_includes_ev_friendly_book_name_and_odd():
    msg = format_value_bet(_bet(ev_pct=5.17))
    assert "+5.17% EV" in msg
    # Book name is the human-friendly label, not the enum value.
    assert "Unibet" in msg
    assert "unibet_be" not in msg
    # Teams come out title-cased from the normalized event-key fragments.
    assert "Boise vs Sarasota" in msg
    assert "@ 1.86" in msg
    assert "fair 1.77" in msg
    # No more raw event_key dump in the body.
    assert "boise__vs__sarasota" not in msg


def test_format_includes_kickoff_date():
    msg = format_value_bet(_bet(ev_pct=5.17))
    # The event_key encodes 2026-06-01 00:00 UTC -> 02:00 Brussels time in summer
    # so the local kickoff shows the converted hour, with the date prefix.
    assert "📅" in msg
    # Should mention a time HH:MM
    import re
    assert re.search(r"\d{2}:\d{2}", msg)


def test_format_includes_euro_stake_on_default_bankroll():
    # kelly_stake_pct=1.50 on a 1000€ bankroll -> 15.00€.
    msg = format_value_bet(_bet())
    assert "1.50%" in msg
    assert "15.00€" in msg
    assert "sur 1000€" in msg


def test_format_includes_line_when_present():
    bet = _bet(label="over", line=2.5)
    bet.market = MarketType.TOTALS
    msg = format_value_bet(bet)
    assert "over 2.5" in msg


def test_format_falls_back_when_event_key_is_unparseable():
    bet = _bet()
    bet.event_key = "garbage"
    msg = format_value_bet(bet)
    # No crash + the raw key is still surfaced so we can debug.
    assert "garbage" in msg
    assert "+5.00% EV" in msg


def test_format_value_bet_prepends_sport_emoji_when_known():
    msg = format_value_bet(_bet(), sport="soccer")
    assert "⚽" in msg
    msg = format_value_bet(_bet(), sport="tennis")
    assert "🎾" in msg
    msg = format_value_bet(_bet(), sport="basketball")
    assert "🏀" in msg
    msg = format_value_bet(_bet(), sport="hockey")
    assert "🏒" in msg


def test_format_value_bet_no_emoji_when_sport_is_none_or_unknown():
    msg = format_value_bet(_bet(), sport=None)
    assert not any(e in msg for e in ("⚽", "🎾", "🏀", "🏒"))
    msg = format_value_bet(_bet(), sport="curling")
    assert not any(e in msg for e in ("⚽", "🎾", "🏀", "🏒"))


def test_format_surebet_prepends_sport_emoji():
    msg = format_surebet(_surebet(margin=0.02), sport="tennis")
    assert "🎾" in msg
    msg = format_surebet(_surebet(margin=0.02), sport="hockey")
    assert "🏒" in msg


def test_alerter_sends_message_above_threshold():
    client = MagicMock(spec=httpx.Client)
    client.post.return_value.status_code = 200
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_ev_pct=3.0)
    with TelegramAlerter(cfg, client=client) as a:
        assert a.send_value_bet(_bet(ev_pct=5.0)) is True
    client.post.assert_called_once()
    args, kwargs = client.post.call_args
    assert "/bott/sendMessage" in args[0]
    assert kwargs["json"]["chat_id"] == "c"
    assert kwargs["json"]["parse_mode"] == "HTML"


def test_alerter_skips_low_ev_silently():
    client = MagicMock(spec=httpx.Client)
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_ev_pct=3.0)
    with TelegramAlerter(cfg, client=client) as a:
        assert a.send_value_bet(_bet(ev_pct=2.0)) is False
    client.post.assert_not_called()


def test_alerter_reports_non_200_through_print_fn():
    client = MagicMock(spec=httpx.Client)
    resp = MagicMock()
    resp.status_code = 403
    resp.text = "forbidden"
    client.post.return_value = resp
    printed = []
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_ev_pct=3.0)
    with TelegramAlerter(cfg, client=client, print_fn=printed.append) as a:
        assert a.send_value_bet(_bet(ev_pct=5.0)) is False
    assert printed and "403" in printed[0]


def test_alerter_swallows_network_failure():
    client = MagicMock(spec=httpx.Client)
    client.post.side_effect = httpx.ConnectError("boom")
    printed = []
    cfg = TelegramConfig(bot_token="t", chat_id="c")
    with TelegramAlerter(cfg, client=client, print_fn=printed.append) as a:
        # EV inside the main band [8, 15) so a send is actually attempted.
        assert a.send_value_bet(_bet(ev_pct=10.0)) is False
    assert printed and "boom" in printed[0]


def _surebet(margin: float = 0.025, suspicious: bool = False,
             event_key: str = "202606010000::boise__vs__sarasota",
             line: float | None = None) -> Surebet:
    return Surebet(
        event_key=event_key,
        market=MarketType.H2H,
        line=line,
        legs={
            "home": (1.95, Book.UNIBET_BE),
            "draw": (3.85, Book.BETFIRST),
            "away": (4.20, Book.LADBROKES_BE),
        },
        margin=margin,
        suspicious=suspicious,
    )


def test_format_surebet_lists_every_leg_with_book_name():
    msg = format_surebet(_surebet(margin=0.0234))
    assert "SUREBET" in msg and "+2.34%" in msg
    assert "💰" in msg            # normal surebet emoji
    assert "⚠️" not in msg       # non-suspicious, no warning
    assert "Boise vs Sarasota" in msg
    # Each leg appears with its book name (friendly form, not the enum value).
    assert "Unibet" in msg
    assert "BetFirst" in msg
    assert "Ladbrokes" in msg
    assert "1.95" in msg and "3.85" in msg and "4.20" in msg
    assert "📅" in msg


def test_format_surebet_marks_suspicious_in_header_and_footer():
    sb = _surebet(margin=0.61, suspicious=True)
    msg = format_surebet(sb)
    # Different header emoji + "SUSPECT" word so it stands out in chat preview.
    assert "⚠️" in msg
    assert "SUSPECT" in msg
    # Footer reminder to verify before acting on a suspect signal.
    assert "Marge inhabituelle" in msg


def test_format_surebet_shows_line_for_totals():
    sb = _surebet(line=2.5)
    sb.market = MarketType.TOTALS
    msg = format_surebet(sb)
    assert "totals 2.5" in msg


def test_alerter_send_surebet_above_threshold():
    client = MagicMock(spec=httpx.Client)
    client.post.return_value.status_code = 200
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_surebet_margin_pct=1.0)
    with TelegramAlerter(cfg, client=client) as a:
        assert a.send_surebet(_surebet(margin=0.025)) is True
    client.post.assert_called_once()


def test_alerter_skips_low_margin_surebet():
    client = MagicMock(spec=httpx.Client)
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_surebet_margin_pct=1.0)
    with TelegramAlerter(cfg, client=client) as a:
        assert a.send_surebet(_surebet(margin=0.005)) is False
    client.post.assert_not_called()


def test_alerter_skips_suspicious_surebet():
    client = MagicMock(spec=httpx.Client)
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_surebet_margin_pct=1.0)
    with TelegramAlerter(cfg, client=client) as a:
        assert a.send_surebet(_surebet(margin=0.20, suspicious=True)) is False
    client.post.assert_not_called()


def test_send_surebet_alerts_counts_above_threshold(monkeypatch):
    sent_count = {"n": 0}

    class FakeClient:
        def post(self, url, json):
            sent_count["n"] += 1
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    monkeypatch.setattr("src.alerter.httpx.Client", lambda **_: FakeClient())
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_surebet_margin_pct=1.0,
                         min_send_interval_s=0.0)
    surebets = [
        _surebet(margin=0.005),                 # below threshold
        _surebet(margin=0.025),                 # above threshold
        _surebet(margin=0.20, suspicious=True), # suspicious -> skipped
        _surebet(margin=0.015),                 # above threshold
    ]
    assert len(send_surebet_alerts(surebets, cfg)) == 2
    assert sent_count["n"] == 2


def test_send_surebet_alerts_returns_zero_when_no_config():
    assert send_surebet_alerts([_surebet()], config=None) == []


def test_send_alerts_returns_zero_when_no_config():
    assert send_alerts([_bet()], config=None) == []


def test_send_alerts_counts_sent_only_above_threshold(monkeypatch):
    sent_count = {"n": 0}

    class FakeClient:
        def post(self, url, json):
            sent_count["n"] += 1
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    monkeypatch.setattr("src.alerter.httpx.Client", lambda **_: FakeClient())
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_ev_pct=3.0,
                         min_send_interval_s=0.0)
    bets = [_bet(ev_pct=2.0), _bet(ev_pct=5.0), _bet(ev_pct=4.1)]
    assert len(send_alerts(bets, cfg)) == 2
    assert sent_count["n"] == 2


# ---------------------------------------------------------------------------
# Critical channel tests
# ---------------------------------------------------------------------------

def test_critical_channel_not_called_without_config():
    """No critical_chat_id → only one POST even for very high EV bets."""
    client = MagicMock(spec=httpx.Client)
    client.post.return_value.status_code = 200
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_ev_pct=3.0,
                         main_max_ev_pct=100.0, min_send_interval_s=0.0)
    with TelegramAlerter(cfg, client=client) as a:
        assert a.send_value_bet(_bet(ev_pct=40.0)) is True
    client.post.assert_called_once()


def test_critical_channel_called_for_high_ev_bet():
    """With critical_chat_id set, a bet above min_critical_ev_pct triggers two POSTs."""
    calls = []

    class FakeClient:
        def post(self, url, json):
            calls.append(json["chat_id"])
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    cfg = TelegramConfig(
        bot_token="t", chat_id="c", min_ev_pct=3.0, main_max_ev_pct=100.0,
        critical_chat_id="crit", min_critical_ev_pct=35.0,
        min_send_interval_s=0.0,
    )
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        assert a.send_value_bet(_bet(ev_pct=40.0)) is True
    assert "c" in calls and "crit" in calls
    assert len(calls) == 2


def test_critical_channel_not_called_for_normal_ev_bet():
    """A bet below the critical threshold should NOT go to the critical channel."""
    calls = []

    class FakeClient:
        def post(self, url, json):
            calls.append(json["chat_id"])
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    cfg = TelegramConfig(
        bot_token="t", chat_id="c", min_ev_pct=3.0,
        critical_chat_id="crit", min_critical_ev_pct=35.0,
        min_send_interval_s=0.0,
    )
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        assert a.send_value_bet(_bet(ev_pct=10.0)) is True
    assert calls == ["c"]


def test_critical_channel_called_for_high_margin_surebet():
    """Surebet with margin > min_critical_surebet_pct → also posted to critical channel."""
    calls = []

    class FakeClient:
        def post(self, url, json):
            calls.append(json["chat_id"])
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    cfg = TelegramConfig(
        bot_token="t", chat_id="c",
        surebet_chat_id="sb",
        critical_chat_id="crit",
        min_surebet_margin_pct=1.0,
        min_critical_surebet_pct=10.0,
        min_send_interval_s=0.0,
    )
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        assert a.send_surebet(_surebet(margin=0.12)) is True
    assert "sb" in calls and "crit" in calls
    assert len(calls) == 2


def test_main_channel_ev_band_confines_value_bets():
    """Main chat only takes EV in [min_ev_pct, main_max_ev_pct); below stays
    silent, above is confined to premium (here: no premium, so it goes nowhere)."""
    calls = []

    class FakeClient:
        def post(self, url, json):
            calls.append(json["chat_id"])
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    cfg = TelegramConfig(
        bot_token="t", chat_id="c",
        min_ev_pct=8.0, main_max_ev_pct=15.0,
        min_send_interval_s=0.0,
    )
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        assert a.send_value_bet(_bet(ev_pct=5.0)) is False    # below band
        assert a.send_value_bet(_bet(ev_pct=10.0)) is True    # inside band
        assert a.send_value_bet(_bet(ev_pct=22.0)) is False   # above band, no premium set
    assert calls == ["c"]  # only the 10% one reached the main chat


def test_big_ev_routes_to_premium_not_main():
    """A 22% EV bet (odds in band) goes to premium, NOT the main chat."""
    calls = []

    class FakeClient:
        def post(self, url, json):
            calls.append(json["chat_id"])
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    cfg = TelegramConfig(
        bot_token="t", chat_id="c",
        min_ev_pct=8.0, main_max_ev_pct=15.0,
        premium_chat_id="prem", min_premium_ev_pct=15.0,
        premium_min_odd=1.5, premium_max_odd=4.0,
        min_send_interval_s=0.0,
    )
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        assert a.send_value_bet(_bet(ev_pct=22.0, odd=2.40)) is True
    assert calls == ["prem"]  # confined to premium, main untouched


def test_premium_skips_live_value_bet():
    """Premium is prematch-only: a premium-eligible bet whose kickoff has
    already passed must NOT reach premium. With EV above the main band and no
    other eligible channel, nothing is sent."""
    calls = []

    class FakeClient:
        def post(self, url, json):
            calls.append(json["chat_id"])
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    cfg = TelegramConfig(
        bot_token="t", chat_id="c",
        min_ev_pct=8.0, main_max_ev_pct=15.0,
        premium_chat_id="prem", min_premium_ev_pct=15.0,
        premium_min_odd=1.5, premium_max_odd=4.0,
        min_send_interval_s=0.0,
    )
    # Kickoff in the past -> the event is live.
    live = _bet(ev_pct=22.0, odd=2.40, event_key="200001010000::boise__vs__sarasota")
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        assert a.send_value_bet(live) is False
    assert calls == []  # premium skipped (live), nothing else qualifies


def test_premium_channel_called_for_big_value_bet_in_odds_band():
    """Value bet >= min_premium_ev_pct with odds in [1.5, 4.0] → also to premium."""
    calls = []

    class FakeClient:
        def post(self, url, json):
            calls.append(json["chat_id"])
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    cfg = TelegramConfig(
        bot_token="t", chat_id="c", min_ev_pct=3.0, main_max_ev_pct=100.0,
        premium_chat_id="prem", min_premium_ev_pct=20.0,
        premium_min_odd=1.5, premium_max_odd=4.0,
        min_send_interval_s=0.0,
    )
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        assert a.send_value_bet(_bet(ev_pct=25.0, odd=2.40)) is True
    assert "c" in calls and "prem" in calls
    assert len(calls) == 2


def test_premium_channel_skips_value_bet_out_of_odds_band():
    """A big EV bet outside the odds band goes to main only, not premium."""
    calls = []

    class FakeClient:
        def post(self, url, json):
            calls.append(json["chat_id"])
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    cfg = TelegramConfig(
        bot_token="t", chat_id="c", min_ev_pct=3.0, main_max_ev_pct=100.0,
        premium_chat_id="prem", min_premium_ev_pct=20.0,
        premium_min_odd=1.5, premium_max_odd=4.0,
        min_send_interval_s=0.0,
    )
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        assert a.send_value_bet(_bet(ev_pct=25.0, odd=6.50)) is True  # odd > 4.0
    assert calls == ["c"]


def test_premium_channel_called_for_prematch_surebet():
    """Prematch surebet >= min_premium_surebet_pct → also posted to premium."""
    calls = []

    class FakeClient:
        def post(self, url, json):
            calls.append(json["chat_id"])
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    cfg = TelegramConfig(
        bot_token="t", chat_id="c",
        surebet_chat_id="sb",
        premium_chat_id="prem",
        min_surebet_margin_pct=1.0, min_premium_surebet_pct=5.0,
        min_send_interval_s=0.0,
    )
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        assert a.send_surebet(_surebet(margin=0.07), is_live=False) is True
    assert "sb" in calls and "prem" in calls
    assert len(calls) == 2


def test_premium_channel_skips_live_surebet():
    """Live surebets must NOT go to the premium channel (prematch only)."""
    calls = []

    class FakeClient:
        def post(self, url, json):
            calls.append(json["chat_id"])
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    cfg = TelegramConfig(
        bot_token="t", chat_id="c",
        surebet_chat_id="sb", live_surebet_chat_id="live",
        premium_chat_id="prem",
        min_surebet_margin_pct=1.0, min_premium_surebet_pct=5.0,
        min_send_interval_s=0.0,
    )
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        assert a.send_surebet(_surebet(margin=0.07), is_live=True) is True
    assert "live" in calls and "prem" not in calls


def test_high_clv_now_lands_in_normal_clv_channel():
    """High CLV no longer has its own channel — it goes to the CLV chat with a 🔥 header."""
    calls = []

    class FakeClient:
        def post(self, url, json):
            calls.append((json["chat_id"], json["text"]))
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    cfg = TelegramConfig(
        bot_token="t", chat_id="c", clv_chat_id="clv",
        min_clv_pct=5.0, min_high_clv_pct=15.0,
        min_send_interval_s=0.0,
    )
    row = {
        "id": 0, "event_key": "202607010000::a__vs__b",
        "book": Book.UNIBET_BE.value, "market": MarketType.H2H.value,
        "outcome_label": "home", "line": None, "odd_taken": 1.86, "kelly_pct": 1.5,
    }
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        assert a.send_clv_alert(row, 18.0, 1.58, 8) is True
    assert len(calls) == 1
    chat_id, text = calls[0]
    assert chat_id == "clv"
    assert "🔥" in text  # high-CLV header retained even though routing merged


def test_premium_channel_not_called_without_config():
    """No premium_chat_id → big value bets only hit the main channel."""
    client = MagicMock(spec=httpx.Client)
    client.post.return_value.status_code = 200
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_ev_pct=3.0,
                         main_max_ev_pct=100.0, min_send_interval_s=0.0)
    with TelegramAlerter(cfg, client=client) as a:
        assert a.send_value_bet(_bet(ev_pct=25.0, odd=2.40)) is True
    client.post.assert_called_once()


def test_critical_channel_message_contains_critical_header():
    """The critical channel message must start with the 🚨 marker."""
    critical_texts = []

    class FakeClient:
        def post(self, url, json):
            if json["chat_id"] == "crit":
                critical_texts.append(json["text"])
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    cfg = TelegramConfig(
        bot_token="t", chat_id="c", min_ev_pct=3.0,
        critical_chat_id="crit", min_critical_ev_pct=35.0,
        min_send_interval_s=0.0,
    )
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        a.send_value_bet(_bet(ev_pct=40.0))
    assert critical_texts and "🚨" in critical_texts[0]
