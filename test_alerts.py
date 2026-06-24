#!/usr/bin/env python3
"""Envoie des alertes de TEST sur tous les canaux en passant par le vrai circuit
de routage (donc le bouton Jouer apparait sur les copies premium).

Couvre :
  - value 6%  cote 2.0  -> canal principal
  - value 12% cote 2.0  -> premium (avec bouton Jouer)
  - value 40% cote 2.0  -> premium (bouton) + critique
  - value 40% cote 6.0  -> critique seul (hors bande premium)
  - surebet 10%         -> surebet + premium
  - surebet 30%         -> surebet + critique
  - CLV 12%             -> canal CLV (jamais critique)

A lancer depuis ~/Projet-Perso :
    source .venv/bin/activate
    python test_alerts.py
"""
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent

# 1. charge le .env dans l'environnement (le daemon le fait via systemd, pas nous)
env = ROOT / ".env"
if env.exists():
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

from src.alerter import TelegramAlerter, TelegramConfig  # noqa: E402
from src.matcher import parse_event_key  # noqa: E402
from src.models import Book  # noqa: E402

cfg = TelegramConfig.from_env()
if cfg is None:
    raise SystemExit("Config Telegram absente : verifie TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID dans .env")

# 2. un match reel et FUTUR (>1h) pour que les paris soient prematch et non filtres
now = datetime.now(timezone.utc)
con = sqlite3.connect(str(ROOT / "data" / "valuebet.db"))
row = con.execute(
    "SELECT event_key FROM events WHERE start_time > ? ORDER BY start_time ASC LIMIT 1",
    ((now + timedelta(hours=1)).isoformat(),),
).fetchone()
con.close()
if not row:
    raise SystemExit("Aucun match futur (>1h) en base — relance quand le daemon a scanne des matchs a venir.")
event_key = row[0]
print("Match de test :", event_key, "->", parse_event_key(event_key))


def vbet(ev, odd):
    return SimpleNamespace(
        ev_pct=ev, odd_taken=odd, event_key=event_key,
        outcome=SimpleNamespace(label="EQUIPE TEST", line=None),
        book=Book.UNIBET_BE, also_books=[],
        kelly_stake_pct=2.5, fair_odd=round(odd / (1 + ev / 100), 3),
    )


def sbet(margin):
    return SimpleNamespace(
        event_key=event_key, market=SimpleNamespace(value="h2h"), line=None,
        legs={"EQUIPE A": (2.10, Book.UNIBET_BE), "EQUIPE B": (2.10, Book.BETFIRST)},
        margin=margin, roi=margin, suspicious=False,
    )


with TelegramAlerter(cfg) as a:
    print("value 6%  cote 2.0  (principal)              ->", a.send_value_bet(vbet(6, 2.0), sport="soccer"))
    print("value 12% cote 2.0  (premium + BOUTON)       ->", a.send_value_bet(vbet(12, 2.0), sport="soccer"))
    print("value 40% cote 2.0  (premium+BOUTON+critique)->", a.send_value_bet(vbet(40, 2.0), sport="soccer"))
    print("value 40% cote 6.0  (critique seul)          ->", a.send_value_bet(vbet(40, 6.0), sport="soccer"))
    print("surebet 10%         (surebet + premium)      ->", a.send_surebet(sbet(0.10), sport="soccer", is_live=False))
    print("surebet 30%         (surebet + critique)     ->", a.send_surebet(sbet(0.30), sport="soccer", is_live=False))
    clv_bet = {
        "event_key": event_key, "book": Book.UNIBET_BE.value, "line": None,
        "outcome_label": "EQUIPE TEST", "odd_taken": 2.0, "kelly_pct": 2.5,
    }
    print("CLV 12%            (canal CLV, PAS critique)  ->", a.send_clv_alert(clv_bet, 12.0, 1.8, 20, sport="soccer"))

print("\nTermine. Regarde tes canaux : le bouton Jouer doit apparaitre sous les value premium.")
print("(Si tu cliques le bouton d'un test, une ligne de test s'ajoutera a paris.xlsx — tu peux la supprimer.)")
