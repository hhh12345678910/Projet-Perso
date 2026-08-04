"""La commande /scan : quels paris elle retient, et comment elle les rend.

La sélection est la seule partie qui porte de la logique, et elle est pure —
elle reçoit des lignes, un ensemble de marchés joués et une horloge. Le reste
(getUpdates, sendMessage) n'est que de la plomberie.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from bot_listener import _channel_marker, format_scan, select_playable
from src.alerter import TelegramConfig


NOW = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)


def _cfg(**kw) -> TelegramConfig:
    base = dict(bot_token="t", chat_id="c", premium_chat_id="prem",
                critical_chat_id="crit", min_minutes_to_kickoff=15)
    base.update(kw)
    return TelegramConfig(**base)


def _row(*, ev=12.0, odd=2.40, book="unibet_be", market="h2h", outcome="home",
         line=None, hours=3.0, home="Anderlecht", away="Club Brugge",
         sport="soccer"):
    """Une ligne value_bets telle que la requête la renvoie (sqlite3.Row-like)."""
    start = NOW + timedelta(hours=hours)
    return {
        "event_key": f"{start.strftime('%Y%m%d%H%M')}::"
                     f"{home.lower().replace(' ', '')}__vs__{away.lower().replace(' ', '')}",
        "book": book, "market": market, "outcome_label": outcome, "line": line,
        "odd_taken": odd, "ev_pct": ev, "home": home, "away": away, "sport": sport,
    }


def _sel(rows, played=frozenset(), cfg=None):
    return select_playable(rows, set(played), cfg or _cfg(), NOW)


# --------------------------------------------------------------- filtres ----

def test_played_market_is_excluded_including_its_other_outcomes():
    """Le cœur de la demande : ce qui a été joué ne réapparaît pas.

    Et pas seulement la sélection jouée — tout le marché, comme pour les
    alertes : avoir pris le 1 d'un 1X2 rend le X et le 2 sans objet."""
    home, draw = _row(outcome="home"), _row(outcome="draw")
    assert len(_sel([home, draw])) == 2          # rien de joué : les deux sortent
    played = {f"{home['event_key']}|h2h|None"}
    assert _sel([home, draw], played) == []      # un clic sur le 1 fait taire le marché


def test_other_markets_of_the_same_match_survive():
    """Jouer le 1X2 ne doit pas faire disparaître les totaux du même match."""
    h2h = _row(market="h2h", outcome="home")
    tot = _row(market="totals", outcome="over", line=2.5)
    played = {f"{h2h['event_key']}|h2h|None"}
    kept = _sel([h2h, tot], played)
    assert [b["market"] for b in kept] == ["totals"]


def test_bets_too_close_to_kickoff_are_excluded():
    """Même règle que les alertes : sous 15 min, ce sont surtout des lignes
    périmées."""
    assert _sel([_row(hours=0.1)]) == []
    assert len(_sel([_row(hours=0.5)])) == 1


def test_started_matches_are_excluded():
    assert _sel([_row(hours=-2)]) == []


def test_bets_reaching_no_channel_are_excluded():
    """Un /scan qui montre des paris qui n'alertent jamais serait trompeur."""
    assert _sel([_row(ev=2.0, odd=2.40)]) == []      # sous le seuil principal
    assert _sel([_row(ev=25.0, odd=12.0)]) == []     # EV forte mais hors bandes
    assert _sel([_row(ev=15.0, odd=5.0)]) == []      # cote 4-6 sous les 20 % requis


def test_huge_ev_outside_premium_bands_is_kept_as_critical():
    kept = _sel([_row(ev=60.0, odd=14.0)])
    assert len(kept) == 1 and kept[0]["marker"] == "🚨"


# ------------------------------------------------------- déduplication ------

def test_same_selection_on_several_books_keeps_the_best_price():
    """Une sélection détectée sur trois books est UNE opportunité (§9), et on
    n'en joue qu'une — donc une seule ligne, au meilleur prix."""
    rows = [
        _row(book="unibet_be", odd=2.30, ev=10.0),
        _row(book="starcasino_sport", odd=2.45, ev=13.0),
        _row(book="circus_be", odd=2.38, ev=11.5),
    ]
    kept = _sel(rows)
    assert len(kept) == 1
    assert kept[0]["book"] == "starcasino_sport" and kept[0]["odd"] == 2.45


def test_results_are_sorted_by_ev_descending():
    rows = [_row(ev=8.0, outcome="home"), _row(ev=22.0, outcome="draw"),
            _row(ev=14.0, outcome="away")]
    assert [b["ev"] for b in _sel(rows)] == [22.0, 14.0, 8.0]


def test_unparseable_event_key_is_skipped_not_crashed():
    bad = _row()
    bad["event_key"] = "garbage"
    assert _sel([bad, _row(outcome="draw")]) != []


# ------------------------------------------------------------- marqueurs ----

@pytest.mark.parametrize("ev,odd,expected", [
    (12.0, 2.40, "💎"),    # bande premium 1.5-4
    (25.0, 5.00, "💎"),    # bande premium haute 4-6
    (60.0, 14.0, "🚨"),    # hors bandes -> critique
    (6.0, 2.40, "📊"),     # canal principal
    (6.0, 12.0, None),     # nulle part
])
def test_channel_marker_mirrors_the_alert_routing(ev, odd, expected):
    assert _channel_marker(_cfg(), ev, odd) == expected


# --------------------------------------------------------------- rendu ------

def test_format_reports_emptiness_explicitly():
    out = format_scan([], now=NOW)
    assert len(out) == 1 and "aucune value jouable" in out[0]


def test_format_lists_teams_price_book_and_delay():
    out = format_scan(_sel([_row(ev=12.4, odd=2.35, hours=3)]), now=NOW)
    assert len(out) == 1
    msg = out[0]
    assert "Anderlecht" in msg and "Club Brugge" in msg
    assert "+12.4%" in msg and "@ 2.35" in msg
    assert "dans 3 h" in msg


def test_format_shows_the_line_for_totals():
    rows = [_row(market="totals", outcome="over", line=2.5)]
    assert "over 2.5" in format_scan(_sel(rows), now=NOW)[0]


def test_format_splits_long_scans_across_messages():
    """Telegram refuse au-delà de 4096 caractères — un scan chargé doit sortir
    en plusieurs messages plutôt que d'échouer en silence."""
    rows = [_row(outcome=f"o{i}", ev=10.0 + i * 0.01) for i in range(60)]
    out = format_scan(_sel(rows), now=NOW)
    assert len(out) > 1
    assert all(len(m) <= 4096 for m in out)


def test_format_escapes_html_in_team_names():
    """Un nom d'équipe contenant < ou & casserait le parse_mode HTML."""
    row = _row(home="A & <b>B</b>")
    msg = format_scan(_sel([row]), now=NOW)[0]
    assert "&amp;" in msg and "&lt;b&gt;" in msg


# ------------------------------------------------------- fraîcheur réelle ---
# Une opportunité n'a qu'UNE ligne en base, écrite à la première détection.
# detected_at ne bouge jamais : c'est last_seen_at que le daemon rafraîchit à
# chaque cycle où il revoit le pari. Ces tests verrouillent cette distinction —
# s'en tromper cache exactement les paris qu'on veut voir.

def test_reseen_bet_refreshes_last_seen_without_touching_detection(tmp_path):
    from src.models import Book, MarketType, Outcome, ValueBet
    from src.storage import Storage

    st = Storage(tmp_path / "t.db")
    t0 = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)

    def vb(at, odd, ev):
        return ValueBet(
            event_key="209906010000::a__vs__b", book=Book.UNIBET_BE,
            market=MarketType.H2H, outcome=Outcome(label="home"),
            odd_taken=odd, fair_prob=0.55, fair_odd=1.80, ev_pct=ev,
            kelly_stake_pct=1.5, detected_at=at,
        )

    first = st.insert_value_bet(vb(t0, 2.40, 12.0))
    again = st.insert_value_bet(vb(t0 + timedelta(hours=3), 2.20, 6.0))
    assert again == first, "une ré-détection ne doit pas créer une seconde ligne"

    import sqlite3
    con = sqlite3.connect(str(tmp_path / "t.db"))
    con.row_factory = sqlite3.Row
    r = con.execute("SELECT * FROM value_bets WHERE id=?", (first,)).fetchone()
    con.close()
    # La détection est intacte : tout le CLV compare la clôture à l'EV de départ.
    assert r["detected_at"] == t0.isoformat()
    assert r["odd_taken"] == 2.40 and r["ev_pct"] == 12.0
    # La fraîcheur et le prix courant ont suivi.
    assert r["last_seen_at"] == (t0 + timedelta(hours=3)).isoformat()
    assert r["last_odd"] == 2.20 and r["last_ev"] == 6.0


def test_scan_selects_on_last_seen_not_detected_at(tmp_path, monkeypatch):
    """Le test qui aurait attrapé le bug, et il passe par fetch_playable — pas
    par une requête recopiée dans le test, qui n'aurait rien prouvé.

    Un pari détecté il y a 3 h mais revu il y a 10 s est jouable ; un pari
    détecté il y a 5 min et mort depuis ne l'est pas. Filtrer sur detected_at
    intervertit exactement les deux."""
    import sqlite3
    import bot_listener
    from src.storage import Storage

    db = tmp_path / "t.db"
    Storage(db)                            # schéma + migrations
    monkeypatch.setattr(bot_listener, "DB_PATH", db)
    monkeypatch.setattr("src.alerter._PLAYS_DB", db)

    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    iso = lambda m: (now - timedelta(minutes=m)).isoformat()  # noqa: E731
    kickoff = (now + timedelta(hours=3)).strftime("%Y%m%d%H%M")
    con = sqlite3.connect(str(db))
    con.executemany(
        "INSERT INTO value_bets(event_key, book, market, outcome_label, line, "
        "odd_taken, fair_prob, fair_odd, ev_pct, kelly_pct, detected_at, "
        "last_seen_at, last_odd, last_ev) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            # vieille détection, toujours vivante -> doit sortir, au prix ACTUEL
            (f"{kickoff}::vivant__vs__x", "unibet_be", "h2h", "home", None,
             2.40, 0.55, 1.8, 12.0, 1.5, iso(180), iso(0.2), 2.35, 11.0),
            # détection récente, morte depuis -> ne doit pas sortir
            (f"{kickoff}::mort__vs__y", "unibet_be", "h2h", "home", None,
             2.40, 0.55, 1.8, 12.0, 1.5, iso(5), iso(40), 2.40, 12.0),
        ],
    )
    con.commit()
    con.close()

    bets = bot_listener.fetch_playable(_cfg(), now=now)
    assert [b["home"] for b in bets] == ["Vivant"]
    # Et c'est le dernier prix vu qui est proposé, pas celui d'il y a 3 h :
    # miser à 2.40 quand le book affiche 2.35 revient à courir après une cote
    # qui n'existe plus.
    assert bets[0]["odd"] == 2.35 and bets[0]["ev"] == 11.0


def test_scan_query_still_sees_rows_written_before_the_migration(tmp_path):
    """Les lignes déjà en base n'ont pas de last_seen_at — le COALESCE doit les
    rattraper plutôt que de les faire disparaître au redémarrage."""
    import sqlite3
    from src.storage import Storage

    Storage(tmp_path / "t.db")
    con = sqlite3.connect(str(tmp_path / "t.db"))
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    con.execute(
        "INSERT INTO value_bets(event_key, book, market, outcome_label, line, "
        "odd_taken, fair_prob, fair_odd, ev_pct, kelly_pct, detected_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("old", "unibet_be", "h2h", "home", None, 2.4, 0.55, 1.8, 12.0, 1.5,
         (now - timedelta(minutes=2)).isoformat()),
    )
    con.commit()
    n = con.execute(
        "SELECT COUNT(*) FROM value_bets WHERE COALESCE(last_seen_at, detected_at) >= ?",
        ((now - timedelta(minutes=10)).isoformat(),),
    ).fetchone()[0]
    con.close()
    assert n == 1


# ------------------------------------------------------------- dispatch -----
# Dans un CANAL Telegram, un message posté arrive en "channel_post" et non en
# "message". Les canaux d'alerte du projet en sont : n'écouter que "message"
# faisait que /scan ne recevait jamais rien, sans la moindre trace au journal.

def _dispatch_capture(monkeypatch, upd):
    import bot_listener
    seen = {}
    monkeypatch.setattr(bot_listener, "handle_message", lambda m: seen.setdefault("msg", m))
    monkeypatch.setattr(bot_listener, "handle_callback", lambda c: seen.setdefault("cb", c))
    bot_listener.dispatch(upd)
    return seen


def test_dispatch_routes_channel_post_like_a_message(monkeypatch):
    post = {"chat": {"id": -100123}, "text": "/scan"}
    assert _dispatch_capture(monkeypatch, {"update_id": 1, "channel_post": post}) == {"msg": post}


def test_dispatch_routes_plain_message(monkeypatch):
    msg = {"chat": {"id": 42}, "text": "/scan"}
    assert _dispatch_capture(monkeypatch, {"update_id": 1, "message": msg}) == {"msg": msg}


def test_dispatch_routes_callback_query(monkeypatch):
    cb = {"id": "x", "data": "play:abc"}
    assert _dispatch_capture(monkeypatch, {"update_id": 1, "callback_query": cb}) == {"cb": cb}


def test_dispatch_logs_unhandled_update_types(monkeypatch, capsys):
    _dispatch_capture(monkeypatch, {"update_id": 1, "poll_answer": {}})
    assert "poll_answer" in capsys.readouterr().out


def test_allowed_updates_covers_every_type_dispatch_handles():
    """Telegram ne livre que ce qui est demandé : un type géré par dispatch mais
    absent d'allowed_updates n'arriverait jamais — l'erreur d'origine."""
    import bot_listener
    for kind in ("callback_query", "message", "channel_post", "edited_channel_post"):
        assert kind in bot_listener.ALLOWED_UPDATES


# ------------------------------------------------------ chargement du .env --
# bot_listener est lance par systemd sans EnvironmentFile : sans chargement
# explicite, os.environ ne contient aucune config Telegram et /scan se
# desactive tout seul sur une installation qui marche. load_token() lisait deja
# .env pour son propre compte, ce qui masquait le probleme — le service
# demarrait normalement et ne repondait a rien.

def test_load_env_file_populates_os_environ(tmp_path, monkeypatch):
    from src.config import load_env_file

    env = tmp_path / ".env"
    env.write_text(
        "# un commentaire\n"
        "\n"
        "TELEGRAM_BOT_TOKEN=abc123\n"
        'TELEGRAM_PREMIUM_CHAT_ID="-1001234"\n'
        "LIGNE_SANS_EGAL\n"
    )
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_PREMIUM_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    assert load_env_file(env) == 2
    import os
    assert os.environ["TELEGRAM_BOT_TOKEN"] == "abc123"
    assert os.environ["TELEGRAM_PREMIUM_CHAT_ID"] == "-1001234"   # guillemets retires


def test_load_env_file_does_not_override_the_environment(monkeypatch, tmp_path):
    """Un override passe au service doit rester prioritaire sur le fichier."""
    from src.config import load_env_file
    import os

    env = tmp_path / ".env"
    env.write_text("TELEGRAM_CHAT_ID=du_fichier\n")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "de_l_environnement")
    load_env_file(env)
    assert os.environ["TELEGRAM_CHAT_ID"] == "de_l_environnement"


def test_load_env_file_tolerates_a_missing_file(tmp_path):
    from src.config import load_env_file
    assert load_env_file(tmp_path / "pas_la") == 0


def test_scan_accepts_every_configured_channel(monkeypatch, tmp_path):
    """La garde de chat doit connaitre TOUS les canaux du .env, pas seulement
    le principal — /scan est tape depuis le premium."""
    from src.config import load_env_file
    import bot_listener
    from src.alerter import TelegramConfig

    env = tmp_path / ".env"
    env.write_text(
        "TELEGRAM_BOT_TOKEN=t\n"
        "TELEGRAM_CHAT_ID=-100main\n"
        "TELEGRAM_PREMIUM_CHAT_ID=-100prem\n"
        "TELEGRAM_CRITICAL_CHAT_ID=-100crit\n"
        "TELEGRAM_SUREBET_CHAT_ID=-100sure\n"
        "TELEGRAM_CLV_CHAT_ID=-100clv\n"
    )
    for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "TELEGRAM_PREMIUM_CHAT_ID",
              "TELEGRAM_CRITICAL_CHAT_ID", "TELEGRAM_SUREBET_CHAT_ID",
              "TELEGRAM_CLV_CHAT_ID", "TELEGRAM_LIVE_SUREBET_CHAT_ID"):
        monkeypatch.delenv(k, raising=False)
    load_env_file(env)
    cfg = TelegramConfig.from_env()
    assert cfg is not None, "config illisible alors que .env est complet"
    assert bot_listener._allowed_chats(cfg) == {
        "-100main", "-100prem", "-100crit", "-100sure", "-100clv",
    }
