#!/usr/bin/env python3
"""Ajoute le bouton "Jouer" aux alertes value premium de src/alerter.py.

Sans risque : sauvegarde d'abord le fichier, n'applique les modifs que si
chaque ancre est trouvee une et une seule fois, puis verifie que le resultat
compile. En cas de doute, il s'arrete sans rien casser.

Usage :
    python3 apply_play_button.py            # patche src/alerter.py
    python3 apply_play_button.py chemin.py  # patche un autre fichier
"""
from __future__ import annotations

import py_compile
import shutil
import sys
import time
from pathlib import Path

HELPERS = '''_PLAYS_DB = Path(__file__).resolve().parent.parent / "data" / "valuebet.db"


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
            "selection TEXT, cote REAL, mise REAL, ev REAL, created_at TEXT)"
        )
        con.execute(
            "INSERT INTO pending_plays(token, sport, match, book, selection, "
            "cote, mise, ev, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (token, payload["sport"], payload["match"], payload["book"],
             payload["selection"], payload["cote"], payload["mise"], payload["ev"],
             datetime.now(timezone.utc).isoformat()),
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
        "mise": round(bet.kelly_stake_pct / 100.0 * bankroll, 2),
        "ev": round(bet.ev_pct, 2),
    }


'''

# (ancre a remplacer, texte de remplacement)
PATCHES: list[tuple[str, str]] = [
    # 1. imports : ajoute Path (uuid est importe localement dans le helper)
    (
        "import os\nimport sqlite3\n",
        "import os\nimport sqlite3\nfrom pathlib import Path\n",
    ),
    # 2. helpers module-level, juste avant la classe
    (
        'class TelegramAlerter:\n    """Thin wrapper around the Telegram Bot API.',
        HELPERS + 'class TelegramAlerter:\n    """Thin wrapper around the Telegram Bot API.',
    ),
    # 3a. signature de _send : ajoute reply_markup
    (
        "    def _send(self, text: str, chat_id: str) -> bool:\n",
        "    def _send(self, text: str, chat_id: str, reply_markup: dict | None = None) -> bool:\n",
    ),
    # 3b. injecte reply_markup dans le payload du sendMessage
    (
        '            r = self._client.post(\n'
        '                f"{self.API_BASE}/bot{self.config.bot_token}/sendMessage",\n'
        '                json={\n'
        '                    "chat_id": chat_id,\n'
        '                    "text": text,\n'
        '                    "parse_mode": self.config.parse_mode,\n'
        '                    "disable_web_page_preview": True,\n'
        '                },\n'
        '            )\n',
        '            payload = {\n'
        '                "chat_id": chat_id,\n'
        '                "text": text,\n'
        '                "parse_mode": self.config.parse_mode,\n'
        '                "disable_web_page_preview": True,\n'
        '            }\n'
        '            if reply_markup is not None:\n'
        '                payload["reply_markup"] = reply_markup\n'
        '            r = self._client.post(\n'
        '                f"{self.API_BASE}/bot{self.config.bot_token}/sendMessage",\n'
        '                json=payload,\n'
        '            )\n',
    ),
    # 4. branche premium : genere le token + attache le bouton Jouer
    (
        '            delivered |= self._send(\n'
        '                "💎 <b>VALUE PREMIUM</b>\\n" + text, chat_id=cfg.effective_premium_chat_id\n'
        '            )\n',
        '            button = None\n'
        '            try:\n'
        '                token = _record_pending_play(\n'
        '                    _value_bet_play_payload(bet, sport, cfg.bankroll)\n'
        '                )\n'
        '                button = {"inline_keyboard": [[{\n'
        '                    "text": "\\u25b6\\ufe0f Jouer", "callback_data": f"play:{token}"\n'
        '                }]]}\n'
        '            except Exception as e:  # never let bet-logging break alerting\n'
        '                self._print(f"pending_play record failed: {e}")\n'
        '            delivered |= self._send(\n'
        '                "💎 <b>VALUE PREMIUM</b>\\n" + text,\n'
        '                chat_id=cfg.effective_premium_chat_id,\n'
        '                reply_markup=button,\n'
        '            )\n',
    ),
]


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "src/alerter.py")
    if not target.exists():
        sys.exit(f"fichier introuvable : {target}")
    src = target.read_text()

    # verifie d'abord que tout est applicable (sinon on ne touche a rien)
    for i, (old, _new) in enumerate(PATCHES, 1):
        n = src.count(old)
        if n != 1:
            sys.exit(f"patch {i} : ancre trouvee {n} fois (attendu 1) — abandon, rien modifie")
    if "_record_pending_play" in src:
        sys.exit("le fichier semble deja patche (_record_pending_play present) — abandon")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    bak = target.with_name(f"{target.name}.bak-{stamp}")
    shutil.copy2(target, bak)

    out = src
    for old, new in PATCHES:
        out = out.replace(old, new, 1)
    target.write_text(out)

    try:
        py_compile.compile(str(target), doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copy2(bak, target)  # rollback
        sys.exit(f"erreur de compilation, restauration de la sauvegarde :\n{e}")

    print(f"OK : {target} patche. Sauvegarde -> {bak}")
    print("Les alertes 'VALUE PREMIUM' porteront maintenant un bouton ▶️ Jouer.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
