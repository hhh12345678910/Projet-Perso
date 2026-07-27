#!/usr/bin/env python3
"""Ecoute les clics sur le bouton "Jouer" des alertes Telegram et enregistre
le pari dans un fichier Excel (data/paris.xlsx) + marque placed=1 en base.

Architecture :
  - L'alerte est envoyee par le daemon avec un bouton inline dont le
    callback_data vaut "play:<value_bet_id>".
  - Ce service fait du long-polling getUpdates (un SEUL process doit le faire).
  - Au clic : bouton -> "Joue", placed=1, ligne ajoutee dans l'Excel.

Dependances :  pip install requests openpyxl
Token : lu depuis la variable d'env TELEGRAM_BOT_TOKEN (ou le .env du projet).

Lancement manuel :
    source .venv/bin/activate
    python bot_listener.py
"""
from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from openpyxl import Workbook, load_workbook
from openpyxl.worksheet.datavalidation import DataValidation

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "data" / "valuebet.db"
XLSX_PATH = ROOT / "data" / "paris.xlsx"
TRACK_PATH = ROOT / "data" / "paris_track.csv"
OFFSET_PATH = ROOT / "data" / ".telegram_offset"
BANKROLL = 1000.0  # capital de depart pour la colonne "Capital"
TRACK_STAKE_EUR = 25.0  # mise fictive constante, pour comparer des signaux

# --- colonnes de l'Excel (ordre = ordre d'ecriture) ---
HEADERS = [
    "Date", "Sport", "Match", "Book", "Selection",
    "Cote", "Mise EUR", "Resultat", "P&L EUR", "Capital", "EV %", "id",
]
COL = {name: chr(ord("A") + i) for i, name in enumerate(HEADERS)}  # A..L


def load_token() -> str:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if tok:
        return tok
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise SystemExit("TELEGRAM_BOT_TOKEN introuvable (env ou .env)")


API = ""  # rempli dans main()


def tg(method: str, **params):
    r = requests.post(f"{API}/{method}", json=params, timeout=65)
    return r.json()


# ---------------------------------------------------------------- Excel -----
def ensure_workbook():
    if XLSX_PATH.exists():
        return
    XLSX_PATH.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Paris"
    ws.append(HEADERS)
    wb.save(XLSX_PATH)


def already_logged(vb_id: int) -> bool:
    """Evite les doublons si on reclique sur le meme pari."""
    if not XLSX_PATH.exists():
        return False
    wb = load_workbook(XLSX_PATH)
    ws = wb.active
    idx = HEADERS.index("id")
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row and str(row[idx]) == str(vb_id):
            return True
    return False


def append_bet(bet: dict) -> None:
    ensure_workbook()
    wb = load_workbook(XLSX_PATH)
    ws = wb.active
    r = ws.max_row + 1  # ligne en cours d'ecriture

    pnl = COL["P&L EUR"] + str(r)
    res = COL["Resultat"] + str(r)
    cote = COL["Cote"] + str(r)
    mise = COL["Mise EUR"] + str(r)
    pnl_col = COL["P&L EUR"]

    # P&L auto selon le resultat saisi a la main (Gagne / Perdu / Annule)
    pnl_formula = (
        f'=IF({res}="Gagne",{mise}*({cote}-1),'
        f'IF({res}="Perdu",-{mise},'
        f'IF({res}="Annule",0,"")))'
    )
    # Capital = bankroll + somme des P&L jusqu'a cette ligne
    capital_formula = f"={BANKROLL}+SUM({pnl_col}$2:{pnl_col}{r})"

    ws.append([
        bet["date"], bet["sport"], bet["match"], bet["book"], bet["selection"],
        bet["cote"], bet["mise"], "", pnl_formula, capital_formula,
        bet["ev"], bet["id"],
    ])

    # menu deroulant Gagne/Perdu/Annule sur la colonne Resultat
    dv = DataValidation(type="list", formula1='"Gagne,Perdu,Annule"', allow_blank=True)
    ws.add_data_validation(dv)
    dv.add(ws[res])

    wb.save(XLSX_PATH)


# -------------------------------------------------------------- Database ----
def fetch_bet(token: str) -> dict | None:
    """Lit la ligne pending_plays ecrite par l'alerter au moment de l'alerte."""
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=5000")
    row = con.execute(
        "SELECT * FROM pending_plays WHERE token = ?", (token,)
    ).fetchone()
    con.close()
    if not row:
        return None
    return {
        "id": token,
        "date": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "sport": row["sport"] or "",
        "match": row["match"],
        "book": row["book"],
        "selection": row["selection"],
        "cote": round(row["cote"], 3),
        "mise": round(row["mise"] or 0.0, 2),
        "ev": round(row["ev"], 1),
        "dedup_key": (row["dedup_key"] if "dedup_key" in row.keys() else None),
    }


def _record_played(bet: dict) -> None:
    """Enregistre le pari joue : suppression des alertes sur la selection, et
    une ligne dans le fichier de suivi.

    Le dedup_key vaut exactement event_key|market|outcome|line, ce qui suffit a
    retrouver le value_bet correspondant — et donc a rattacher plus tard la
    ligne de cloture, le CLV et le resultat a ce clic."""
    dedup_key = bet.get("dedup_key")
    if not dedup_key:
        return
    now = datetime.now(timezone.utc)

    from src.storage import Storage
    from src import track

    storage = Storage(DB_PATH)
    parts = dedup_key.split("|")
    vb = None
    if len(parts) == 4:
        line = None if parts[3] in ("None", "") else float(parts[3])
        vb = storage.latest_value_bet_for(parts[0], parts[1], parts[2], line)

    already = storage.played_bet(dedup_key) is not None
    storage.record_played_bet(
        dedup_key=dedup_key, played_at=now, stake=track.STAKE_EUR,
        value_bet=vb, sport=bet.get("sport") or "", book=bet.get("book") or "",
        odd_taken=bet.get("cote"), ev_pct=bet.get("ev"),
    )
    if already:
        return  # deja suivi : ne pas dupliquer la ligne du fichier

    row = [""] * len(track.HEADERS)
    def put(col: str, value) -> None:
        row[track.HEADERS.index(col)] = value

    put("Date", now.strftime("%Y-%m-%d %H:%M"))
    put("Sport", bet.get("sport") or "")
    put("Match", bet.get("match") or "")
    put("Book", bet.get("book") or "")
    put("Sélection", bet.get("selection") or "")
    put("Cote prise", f"{float(bet['cote']):.2f}" if bet.get("cote") else "")
    put("EV %", f"{float(bet['ev']):.2f}" if bet.get("ev") is not None else "")
    put("Mise fictive", f"{track.STAKE_EUR:.2f}")
    if vb is not None:
        put("Marché", vb["market"])
        put("Cote fair (détection)", f"{float(vb['fair_odd']):.2f}")
        put("event_key", vb["event_key"])
    elif len(parts) == 4:
        put("Marché", parts[1])
        put("event_key", parts[0])
    # CLV, resultat et P&L restent vides : ils n'existent qu'apres le coup
    # d'envoi. `track-update` reconstruit le fichier complet a ce moment-la.
    track.append(TRACK_PATH, row)


# ----------------------------------------------------------- Callbacks ------
def handle_callback(cb: dict) -> None:
    data = cb.get("data", "")
    if not data.startswith("play:"):
        return
    cb_id = cb["id"]
    token = data.split(":", 1)[1]
    if not token:
        tg("answerCallbackQuery", callback_query_id=cb_id, text="Jeton invalide")
        return

    if already_logged(token):
        tg("answerCallbackQuery", callback_query_id=cb_id, text="Deja enregistre")
        _mark_button_done(cb)
        return

    bet = fetch_bet(token)
    if not bet:
        tg("answerCallbackQuery", callback_query_id=cb_id, text="Pari introuvable")
        return

    append_bet(bet)
    _record_played(bet)
    _mark_button_done(cb)
    tg("answerCallbackQuery", callback_query_id=cb_id,
       text=f"Enregistre : {bet['mise']:.2f} EUR @ {bet['cote']}")
    print(f"[{datetime.now():%H:%M:%S}] logged {token} {bet['match']} {bet['book']}")


def _mark_button_done(cb: dict) -> None:
    """Remplace le bouton par un '✅ Joue' non cliquable (effet 'vert')."""
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    msg_id = msg.get("message_id")
    if chat_id is None or msg_id is None:
        return
    tg("editMessageReplyMarkup", chat_id=chat_id, message_id=msg_id,
       reply_markup={"inline_keyboard": [[{"text": "✅ Joue", "callback_data": "noop"}]]})


# --------------------------------------------------------------- Loop -------
def read_offset() -> int:
    if OFFSET_PATH.exists():
        try:
            return int(OFFSET_PATH.read_text().strip())
        except ValueError:
            return 0
    return 0


def save_offset(off: int) -> None:
    OFFSET_PATH.write_text(str(off))


def main() -> None:
    global API
    API = f"https://api.telegram.org/bot{load_token()}"
    offset = read_offset()
    print(f"bot_listener demarre (offset={offset}, db={DB_PATH}, xlsx={XLSX_PATH})")
    while True:
        try:
            resp = tg("getUpdates", offset=offset, timeout=50,
                      allowed_updates=["callback_query"])
            for upd in resp.get("result", []):
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    handle_callback(upd["callback_query"])
                save_offset(offset)
        except requests.RequestException as e:
            print("reseau:", e)
            time.sleep(3)
        except Exception as e:  # ne jamais laisser le service tomber
            print("erreur:", e)
            time.sleep(3)


if __name__ == "__main__":
    main()
