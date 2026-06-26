#!/usr/bin/env python3
"""Supprime les alertes value sur une selection deja jouee (clic Jouer).

Au clic, le listener enregistre la cle 'event|marche|issue|ligne' dans la table
played_bets. L'alerter, avant d'envoyer une value, saute si la selection est
deja jouee (tous books/cotes confondus). Une AUTRE selection du meme match
reste alertee.

Touche src/alerter.py et bot_listener.py. Par fichier : ignore si deja patche,
verifie chaque ancre unique, sauvegarde, applique, recompile (rollback sinon).

Usage : python3 apply_played_suppress.py   (depuis ~/Projet-Perso)
"""
from __future__ import annotations

import py_compile
import shutil
import time
from pathlib import Path

DEDUP = 'f"{bet.event_key}|{bet.market.value}|{bet.outcome.label}|{bet.outcome.line}"'

LOAD_FN = (
    "def _load_played_keys() -> set:\n"
    '    """Selections deja jouees (clic Jouer) a ne plus alerter, tous books/cotes."""\n'
    "    try:\n"
    "        con = sqlite3.connect(str(_PLAYS_DB))\n"
    '        con.execute("PRAGMA busy_timeout=3000")\n'
    "        con.execute(\n"
    '            "CREATE TABLE IF NOT EXISTS played_bets (dedup_key TEXT PRIMARY KEY, played_at TEXT)"\n'
    "        )\n"
    '        rows = con.execute("SELECT dedup_key FROM played_bets").fetchall()\n'
    "        con.close()\n"
    "        return {r[0] for r in rows}\n"
    "    except Exception:\n"
    "        return set()\n"
    "\n\n"
)

RECORD_FN = (
    "def _record_played(dedup_key) -> None:\n"
    '    """Marque la selection comme jouee -> plus d\'alerte dessus (tous books/cotes)."""\n'
    "    if not dedup_key:\n"
    "        return\n"
    "    con = sqlite3.connect(str(DB_PATH))\n"
    '    con.execute("PRAGMA busy_timeout=5000")\n'
    "    con.execute(\n"
    '        "CREATE TABLE IF NOT EXISTS played_bets (dedup_key TEXT PRIMARY KEY, played_at TEXT)"\n'
    "    )\n"
    "    con.execute(\n"
    '        "INSERT OR IGNORE INTO played_bets(dedup_key, played_at) VALUES (?, ?)",\n'
    "        (dedup_key, datetime.now(timezone.utc).isoformat()),\n"
    "    )\n"
    "    con.commit()\n"
    "    con.close()\n"
    "\n\n"
)

FILES: dict[str, dict] = {
    "src/alerter.py": {
        "marker": "_load_played_keys",
        "patches": [
            # A1 : dedup_key dans le payload
            (
                '        "cote": round(bet.odd_taken, 3),\n'
                '        "mise": round(min(bet.kelly_stake_pct, _MAX_STAKE_PCT) / 100.0 * bankroll, 2),\n'
                '        "ev": round(bet.ev_pct, 2),\n'
                "    }\n",
                '        "cote": round(bet.odd_taken, 3),\n'
                '        "mise": round(min(bet.kelly_stake_pct, _MAX_STAKE_PCT) / 100.0 * bankroll, 2),\n'
                '        "ev": round(bet.ev_pct, 2),\n'
                f'        "dedup_key": {DEDUP},\n'
                "    }\n",
            ),
            # A2 : pending_plays + colonne dedup_key (avec migration ALTER)
            (
                '        con.execute(\n'
                '            "CREATE TABLE IF NOT EXISTS pending_plays("\n'
                '            "token TEXT PRIMARY KEY, sport TEXT, match TEXT, book TEXT, "\n'
                '            "selection TEXT, cote REAL, mise REAL, ev REAL, created_at TEXT)"\n'
                '        )\n'
                '        con.execute(\n'
                '            "INSERT INTO pending_plays(token, sport, match, book, selection, "\n'
                '            "cote, mise, ev, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",\n'
                '            (token, payload["sport"], payload["match"], payload["book"],\n'
                '             payload["selection"], payload["cote"], payload["mise"], payload["ev"],\n'
                '             datetime.now(timezone.utc).isoformat()),\n'
                '        )\n',
                '        con.execute(\n'
                '            "CREATE TABLE IF NOT EXISTS pending_plays("\n'
                '            "token TEXT PRIMARY KEY, sport TEXT, match TEXT, book TEXT, "\n'
                '            "selection TEXT, cote REAL, mise REAL, ev REAL, created_at TEXT, dedup_key TEXT)"\n'
                '        )\n'
                '        try:\n'
                '            con.execute("ALTER TABLE pending_plays ADD COLUMN dedup_key TEXT")\n'
                '        except sqlite3.OperationalError:\n'
                '            pass\n'
                '        con.execute(\n'
                '            "INSERT INTO pending_plays(token, sport, match, book, selection, "\n'
                '            "cote, mise, ev, created_at, dedup_key) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",\n'
                '            (token, payload["sport"], payload["match"], payload["book"],\n'
                '             payload["selection"], payload["cote"], payload["mise"], payload["ev"],\n'
                '             datetime.now(timezone.utc).isoformat(), payload.get("dedup_key")),\n'
                '        )\n',
            ),
            # A3 : fonction _load_played_keys avant la classe
            (
                'class TelegramAlerter:\n'
                '    """Thin wrapper around the Telegram Bot API. send_value_bet is best-effort:\n',
                LOAD_FN
                + 'class TelegramAlerter:\n'
                '    """Thin wrapper around the Telegram Bot API. send_value_bet is best-effort:\n',
            ),
            # A4 : charge les cles jouees a l'init
            (
                "        self.config = config\n"
                "        self._client = client or httpx.Client(timeout=10.0)\n"
                "        self._owns_client = client is None\n"
                "        self._print = print_fn\n",
                "        self.config = config\n"
                "        self._client = client or httpx.Client(timeout=10.0)\n"
                "        self._owns_client = client is None\n"
                "        self._print = print_fn\n"
                "        self._played_keys = _load_played_keys()\n",
            ),
            # A5 : saut si selection deja jouee
            (
                "        cfg = self.config\n"
                "        ev = bet.ev_pct\n"
                "        text = format_value_bet(bet, sport=sport)\n"
                "        delivered = False\n",
                "        cfg = self.config\n"
                f"        if {DEDUP} in self._played_keys:\n"
                "            return False\n"
                "        ev = bet.ev_pct\n"
                "        text = format_value_bet(bet, sport=sport)\n"
                "        delivered = False\n",
            ),
        ],
    },
    "bot_listener.py": {
        "marker": "_record_played",
        "patches": [
            # L1 : fetch_bet renvoie dedup_key
            (
                '        "cote": round(row["cote"], 3),\n'
                '        "mise": round(row["mise"] or 0.0, 2),\n'
                '        "ev": round(row["ev"], 1),\n'
                "    }\n",
                '        "cote": round(row["cote"], 3),\n'
                '        "mise": round(row["mise"] or 0.0, 2),\n'
                '        "ev": round(row["ev"], 1),\n'
                '        "dedup_key": (row["dedup_key"] if "dedup_key" in row.keys() else None),\n'
                "    }\n",
            ),
            # L2 : fonction _record_played avant les callbacks
            (
                "# ----------------------------------------------------------- Callbacks ------\n"
                "def handle_callback(cb: dict) -> None:\n",
                RECORD_FN
                + "# ----------------------------------------------------------- Callbacks ------\n"
                "def handle_callback(cb: dict) -> None:\n",
            ),
            # L3 : enregistre la selection jouee au clic
            (
                "    append_bet(bet)\n"
                "    _mark_button_done(cb)\n",
                "    append_bet(bet)\n"
                "    _record_played(bet.get(\"dedup_key\"))\n"
                "    _mark_button_done(cb)\n",
            ),
        ],
    },
}


def main() -> int:
    any_done = False
    for rel, spec in FILES.items():
        target = Path(rel)
        if not target.exists():
            print(f"SKIP  {rel} : introuvable")
            continue
        src = target.read_text()
        if spec["marker"] in src:
            print(f"SKIP  {rel} : deja patche")
            continue
        bad = False
        for i, (old, _n) in enumerate(spec["patches"], 1):
            c = src.count(old)
            if c != 1:
                print(f"ABORT {rel} patch {i} : ancre {c}x (attendu 1) -> {old[:50]!r}")
                bad = True
        if bad:
            continue
        bak = target.with_name(f"{target.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(target, bak)
        out = src
        for old, new in spec["patches"]:
            out = out.replace(old, new, 1)
        target.write_text(out)
        try:
            py_compile.compile(str(target), doraise=True)
        except py_compile.PyCompileError as e:
            shutil.copy2(bak, target)
            print(f"ROLLBACK {rel} : compilation KO\n{e}")
            continue
        print(f"OK    {rel} patche (backup {bak.name})")
        any_done = True
    print("\nTermine. Redemarre les 2 services :" if any_done else "\nRien modifie.")
    if any_done:
        print("  sudo systemctl restart valuebet-daemon.service valuebet-listener.service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
