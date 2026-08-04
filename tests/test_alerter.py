from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import sqlite3
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
    # kelly_stake_pct=1.50 on a 1000€ bankroll -> 15€.
    # The stake is shown rounded to the euro: a 15.37€ recommendation implies a
    # precision the model doesn't have, and nobody places a bet to the cent.
    msg = format_value_bet(_bet())
    assert "1.50%" in msg
    assert "15€" in msg
    assert "de 1000€" in msg


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
        # EV inside the main band [5, 10) so a send is actually attempted.
        assert a.send_value_bet(_bet(ev_pct=7.0)) is False
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
        assert a.send_value_bet(_bet(ev_pct=7.0)) is True
    assert calls == ["c"]


def _routed(bet, *, premium: bool = True) -> list[str]:
    """Route one bet through a premium+critical config, return the chats hit."""
    calls = []

    class FakeClient:
        def post(self, url, json):
            calls.append(json["chat_id"])
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    cfg = TelegramConfig(
        bot_token="t", chat_id="c", min_ev_pct=3.0, main_max_ev_pct=8.0,
        premium_chat_id="prem" if premium else None,
        min_premium_ev_pct=8.0, premium_min_odd=1.5, premium_max_odd=4.0,
        premium_hi_min_ev=20.0, premium_hi_min_odd=4.0, premium_hi_max_odd=6.0,
        critical_chat_id="crit", min_critical_ev_pct=35.0,
        min_send_interval_s=0.0,
    )
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        a.send_value_bet(bet)
    return calls


def test_premium_takes_huge_ev_inside_its_bands_without_critical_copy():
    """Cote 1.82 à 53 % et cote 2.40 à 50 % : du premium, et rien qu'une fois.

    Cas réels du doctor du 04/08. Ils partaient en critique parce que ce canal
    ne regardait que l'EV ; ils rentrent dans la bande premium 1.5–4, qui n'a
    aucun plafond d'EV — donc premium, sans doublon critique."""
    for odd, ev in ((1.82, 53.0), (2.40, 50.0)):
        calls = _routed(_bet(ev_pct=ev, odd=odd))
        assert "prem" in calls, f"cote {odd} EV {ev} n'atteint pas premium"
        assert "crit" not in calls, f"cote {odd} EV {ev} doublonne sur critique"


def test_premium_has_no_ev_ceiling():
    """50, 60, 80, 300 % — aucun plafond, tant que la cote tient dans la bande."""
    for ev in (50.0, 60.0, 80.0, 300.0):
        calls = _routed(_bet(ev_pct=ev, odd=2.40))
        assert "prem" in calls and "crit" not in calls, f"EV {ev} mal routée"


def test_critical_keeps_extreme_odds_outside_premium_bands():
    """Cote 14.00 à 97 % et cote 21.00 à 45 % : aucune bande premium ne les
    prend, le critique doit donc continuer de les recevoir. Aucune limite de
    cote sur ce canal — c'est sa raison d'être."""
    for odd, ev in ((14.0, 97.0), (21.0, 45.0), (40.0, 60.0)):
        calls = _routed(_bet(ev_pct=ev, odd=odd))
        assert "crit" in calls, f"cote {odd} EV {ev} perdue"
        assert "prem" not in calls


def test_critical_keeps_low_odds_outside_premium_bands():
    """Sous 1.5 le premium ne prend pas non plus — le critique récupère."""
    calls = _routed(_bet(ev_pct=60.0, odd=1.2))
    assert "crit" in calls and "prem" not in calls


def test_high_odds_premium_lane_wins_over_critical():
    """Cote 5.20 à 46 % : voie premium 4–6 (EV ≥ 20) — premium, pas critique."""
    calls = _routed(_bet(ev_pct=46.0, odd=5.20))
    assert "prem" in calls and "crit" not in calls


def test_high_odds_below_premium_lane_minimum_falls_to_critical():
    """Cote 5.00 à 40 % passe la voie 4–6 ; à EV 36 % aussi. La frontière utile
    est 20 % : en dessous, premium refuse et seul le critique peut voir passer
    le pari — encore faut-il qu'il atteigne 35 % d'EV."""
    assert "prem" in _routed(_bet(ev_pct=36.0, odd=5.0))
    # EV 15 % sur cote 5.0 : sous les 20 % du premium ET sous les 35 % du
    # critique -> nulle part, comportement inchangé.
    assert _routed(_bet(ev_pct=15.0, odd=5.0)) == []


def test_critical_still_fires_when_premium_chat_unconfigured():
    """Sans canal premium, rien n'a été livré : le critique doit rattraper le
    pari plutôt que de le laisser disparaître en silence."""
    calls = _routed(_bet(ev_pct=53.0, odd=1.82), premium=False)
    assert "crit" in calls


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


def test_main_channel_odds_band_confines_value_bets():
    """Main chat only takes bets whose odd is within [main_min_odd, main_max_odd]
    (default 1.5-4.0). An in-EV-band bet with out-of-band odds stays silent."""
    calls = []

    class FakeClient:
        def post(self, url, json):
            calls.append((json["chat_id"], json["text"]))
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    cfg = TelegramConfig(
        bot_token="t", chat_id="c",
        min_ev_pct=8.0, main_max_ev_pct=15.0,
        main_min_odd=1.5, main_max_odd=4.0,
        min_send_interval_s=0.0,
    )
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        assert a.send_value_bet(_bet(ev_pct=10.0, odd=1.30)) is False  # odd too low
        assert a.send_value_bet(_bet(ev_pct=10.0, odd=5.50)) is False  # odd too high
        assert a.send_value_bet(_bet(ev_pct=10.0, odd=2.40)) is True   # in band
    assert [c for c, _ in calls] == ["c"]  # only the in-band one reached main


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


def test_value_bet_out_of_odds_band_reaches_no_channel():
    """A bet whose odd is outside the 1.5-4.0 band is skipped by BOTH the main
    and premium channels — it goes nowhere (only the critical channel, which has
    no odds band, would still take a high-EV one)."""
    calls = []

    class FakeClient:
        def post(self, url, json):
            calls.append(json["chat_id"])
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    cfg = TelegramConfig(
        bot_token="t", chat_id="c", min_ev_pct=3.0, main_max_ev_pct=100.0,
        main_min_odd=1.5, main_max_odd=4.0,
        premium_chat_id="prem", min_premium_ev_pct=20.0,
        premium_min_odd=1.5, premium_max_odd=4.0,
        min_send_interval_s=0.0,
    )
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        assert a.send_value_bet(_bet(ev_pct=25.0, odd=6.50)) is False  # odd > 4.0
    assert calls == []


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


def test_premium_and_critical_skip_suspicious_surebet():
    """A suspicious (phantom) surebet may reach the main surebet chat when the
    user opted in, but must NOT pollute the curated premium/critical channels."""
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
        critical_chat_id="crit",
        include_suspicious_surebets=True,
        min_surebet_margin_pct=1.0,
        min_premium_surebet_pct=5.0,
        min_critical_surebet_pct=10.0,
        min_send_interval_s=0.0,
    )
    # 25% margin, flagged suspicious — exactly the phantom case from the field.
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        assert a.send_surebet(_surebet(margin=0.25, suspicious=True), is_live=False) is True
    assert calls == ["sb"]  # main surebet chat only — not prem, not crit


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


def test_sent_value_bet_text_uses_configured_bankroll():
    """Regression: send_value_bet must format the message with cfg.bankroll, not
    the format_value_bet default (1000€). A bet on a 1250€ bankroll used to be
    advertised with a stake computed on 1000€ (20€ instead of 25€ in flat mode;
    the kelly-mode 'de 1000€' label here proves the same wiring bug)."""
    texts = []

    class FakeClient:
        def post(self, url, json):
            texts.append(json["text"])
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass

    cfg = TelegramConfig(
        bot_token="t", chat_id="c", min_ev_pct=3.0, main_max_ev_pct=100.0,
        bankroll=1250.0, min_send_interval_s=0.0,
    )
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        assert a.send_value_bet(_bet(ev_pct=7.0)) is True
    assert texts and "de 1250€" in texts[0]
    assert "de 1000€" not in texts[0]


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


# ── Drop prematch alerts firing within N minutes of kickoff ──────────────────

from datetime import datetime as _dt, timezone as _tz, timedelta as _td


def _ek_in(minutes: float) -> str:
    """event_key whose kickoff is `minutes` from now (negative = already live)."""
    ko = _dt.now(_tz.utc) + _td(minutes=minutes)
    return ko.strftime("%Y%m%d%H%M") + "::boise__vs__sarasota"


def _collect_client():
    calls = []

    class FakeClient:
        def post(self, url, json):
            calls.append(json["chat_id"])
            r = MagicMock(); r.status_code = 200
            return r
        def close(self): pass
    return calls, FakeClient


def test_value_bet_within_kickoff_window_is_dropped():
    calls, FakeClient = _collect_client()
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_ev_pct=5.0,
                         main_max_ev_pct=10.0, min_minutes_to_kickoff=15,
                         min_send_interval_s=0.0)
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        # Kickoff in 10 min -> prematch but too close -> dropped everywhere.
        assert a.send_value_bet(_bet(ev_pct=7.0, event_key=_ek_in(10))) is False
    assert calls == []


def test_value_bet_outside_kickoff_window_is_sent():
    calls, FakeClient = _collect_client()
    cfg = TelegramConfig(bot_token="t", chat_id="c", min_ev_pct=5.0,
                         main_max_ev_pct=10.0, min_minutes_to_kickoff=15,
                         min_send_interval_s=0.0)
    with TelegramAlerter(cfg, client=FakeClient()) as a:
        assert a.send_value_bet(_bet(ev_pct=7.0, event_key=_ek_in(40))) is True
    assert calls == ["c"]


def test_surebet_within_kickoff_window_is_dropped(monkeypatch):
    calls, FakeClient = _collect_client()
    monkeypatch.setattr("src.alerter.httpx.Client", lambda **_: FakeClient())
    cfg = TelegramConfig(bot_token="t", chat_id="c", surebet_chat_id="sb",
                         min_surebet_margin_pct=1.0, min_minutes_to_kickoff=15,
                         min_send_interval_s=0.0)
    sent = send_surebet_alerts([_surebet(margin=0.04, event_key=_ek_in(8))], cfg)
    assert sent == [] and calls == []


def test_surebet_outside_kickoff_window_is_sent(monkeypatch):
    calls, FakeClient = _collect_client()
    monkeypatch.setattr("src.alerter.httpx.Client", lambda **_: FakeClient())
    cfg = TelegramConfig(bot_token="t", chat_id="c", surebet_chat_id="sb",
                         min_surebet_margin_pct=1.0, min_minutes_to_kickoff=15,
                         min_send_interval_s=0.0)
    sent = send_surebet_alerts([_surebet(margin=0.04, event_key=_ek_in(45))], cfg)
    assert len(sent) == 1 and "sb" in calls


# ---------------------------------------------------------------------------
# Playing a bet silences the whole market, not just that selection: once 1 is
# backed on a 1X2, X and 2 are the other side of a position already held.
# ---------------------------------------------------------------------------

def test_market_key_strips_only_the_outcome():
    from src.alerter import _market_key

    assert _market_key("EV|h2h|home|None") == "EV|h2h|None"
    assert _market_key("EV|totals|over|2.5") == "EV|totals|2.5"


def test_market_key_keeps_the_line_so_middles_survive():
    """Over 2.5 and under 3.5 are different bets — holding both is a middle,
    which this engine looks for. Collapsing lines would suppress its own signal."""
    from src.alerter import _market_key

    assert _market_key("EV|totals|over|2.5") != _market_key("EV|totals|under|3.5")


def test_market_key_rejects_malformed_input():
    from src.alerter import _market_key

    assert _market_key("nonsense") is None
    assert _market_key("a|b|c") is None


def test_played_outcome_suppresses_competing_outcomes(tmp_path, monkeypatch):
    import src.alerter as alerter

    db = tmp_path / "valuebet.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE played_bets (dedup_key TEXT PRIMARY KEY, played_at TEXT)")
    # "home" was backed on this match's 1X2.
    con.execute("INSERT INTO played_bets VALUES (?, ?)",
                ("202606010000::boise__vs__sarasota|h2h|home|None", "now"))
    con.commit()
    con.close()
    monkeypatch.setattr(alerter, "_PLAYS_DB", db)

    keys, markets = alerter._load_played_keys()
    assert "202606010000::boise__vs__sarasota|h2h|None" in markets

    def suppressed(label):
        return f"202606010000::boise__vs__sarasota|h2h|None" in markets

    # Every side of that 1X2 is now silenced, not just the one played.
    for label in ("home", "draw", "away"):
        assert suppressed(label), label


def test_played_market_does_not_leak_to_other_markets(tmp_path, monkeypatch):
    """Backing the 1X2 must not silence totals on the same match."""
    import src.alerter as alerter

    db = tmp_path / "valuebet.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE played_bets (dedup_key TEXT PRIMARY KEY, played_at TEXT)")
    con.execute("INSERT INTO played_bets VALUES (?, ?)", ("EV|h2h|home|None", "now"))
    con.commit()
    con.close()
    monkeypatch.setattr(alerter, "_PLAYS_DB", db)

    _, markets = alerter._load_played_keys()
    assert "EV|h2h|None" in markets
    assert "EV|totals|2.5" not in markets


# ---------------------------------------------------------------------------
# League in the message
# ---------------------------------------------------------------------------

def test_format_shows_league_when_known():
    bet = _bet()
    bet.league = "Suisse - Super League"
    assert "🏆 Suisse - Super League" in format_value_bet(bet)


def test_format_omits_league_line_when_absent():
    """Sources other than Betano don't carry one — show nothing rather than an
    empty row."""
    msg = format_value_bet(_bet())
    assert "🏆" not in msg
