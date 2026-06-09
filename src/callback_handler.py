from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import httpx

from .storage import Storage
from .excel_tracker import append_bet


class CallbackPoller(threading.Thread):
    """Background daemon thread that long-polls Telegram getUpdates for
    callback_query events. When the user taps '🎯 Jouer', looks up the
    pending alert, writes a row to the Excel tracker, marks it played in
    the DB, and removes the inline keyboard from the original message."""

    API_BASE = "https://api.telegram.org"

    def __init__(self, bot_token: str, storage: Storage, *, print_fn=print):
        super().__init__(daemon=True, name="TelegramCallbackPoller")
        self._token = bot_token
        self._storage = storage
        self._print = print_fn
        self._offset = 0
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        with httpx.Client(timeout=35.0) as client:
            while not self._stop.is_set():
                try:
                    self._poll_once(client)
                except Exception as exc:
                    self._print(f"[CallbackPoller] error: {exc}")
                    time.sleep(5)

    def _poll_once(self, client: httpx.Client) -> None:
        r = client.get(
            f"{self.API_BASE}/bot{self._token}/getUpdates",
            params={
                "offset": self._offset,
                "timeout": 25,
                "allowed_updates": ["callback_query"],
            },
        )
        if r.status_code != 200:
            return
        data = r.json()
        if not data.get("ok"):
            return
        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            cq = update.get("callback_query")
            if cq is not None:
                self._handle_callback(client, cq)

    def _handle_callback(self, client: httpx.Client, cq: dict) -> None:
        cb_data = cq.get("data", "")
        if not cb_data.startswith("play:"):
            return
        try:
            alert_id = int(cb_data.split(":", 1)[1])
        except ValueError:
            return

        alert = self._storage.get_pending_alert(alert_id)
        now = datetime.now(timezone.utc)

        if alert is None:
            answer_text = "❌ Pari introuvable."
        elif alert["played"]:
            answer_text = "⚠️ Déjà noté dans Excel."
        else:
            try:
                append_bet(alert, now)
                self._storage.mark_alert_played(alert_id, now)
                answer_text = "✅ Noté dans Excel !"
                self._print(f"[CallbackPoller] alert {alert_id} played and logged to Excel")
            except Exception as exc:
                self._print(f"[CallbackPoller] Excel write failed for alert {alert_id}: {exc}")
                answer_text = f"❌ Erreur Excel : {exc}"

        try:
            client.post(
                f"{self.API_BASE}/bot{self._token}/answerCallbackQuery",
                json={"callback_query_id": cq["id"], "text": answer_text, "show_alert": False},
            )
        except Exception as exc:
            self._print(f"[CallbackPoller] answerCallbackQuery failed: {exc}")

        # Remove the keyboard so the button can't be clicked twice
        msg = cq.get("message")
        if msg:
            try:
                client.post(
                    f"{self.API_BASE}/bot{self._token}/editMessageReplyMarkup",
                    json={
                        "chat_id": msg["chat"]["id"],
                        "message_id": msg["message_id"],
                        "reply_markup": {"inline_keyboard": []},
                    },
                )
            except Exception as exc:
                self._print(f"[CallbackPoller] editMessageReplyMarkup failed: {exc}")
