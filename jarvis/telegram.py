"""Invio diretto di messaggi nel topic Jarvis, per gli eventi del runner (non passano da n8n)."""

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("jarvis")
LIMIT = 4000  # Telegram accetta 4096 caratteri per messaggio


def chunks(text: str, limit: int = LIMIT) -> list[str]:
    """Spezza il testo ai fine riga (così i tag HTML, sempre su una riga sola, restano bilanciati)."""
    parts, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # riga lunghissima: taglio duro
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if current and len(current) + 1 + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    return [*parts, current] if current or not parts else parts


class Telegram:
    def __init__(self, token: str, chat_id: int, thread_id: int, timeout: int = 15):
        self.token, self.chat_id, self.thread_id, self.timeout = token, chat_id, thread_id, timeout

    def _call(self, method: str, body: dict) -> dict | None:
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:  # il token non deve finire nei log
            detail = e.read().decode(errors="replace")[:120]
            if "not modified" not in detail:
                log.error("Telegram %s fallito: HTTP %s %s", method, e.code, detail)
            return None
        except (urllib.error.URLError, TimeoutError) as e:
            log.error("Telegram %s fallito: %s", method, type(e).__name__)
            return None

    def send(self, text: str, thread_id: int | None = -1, reply_to: int | None = None) -> int | bool:
        """thread_id -1: topic Jarvis; None: General. Spezza i testi lunghi. Restituisce il message_id
        dell'ultimo messaggio (truthy) o False."""
        sent: int | bool = False
        for n, part in enumerate(chunks(text)):
            body = {"chat_id": self.chat_id, "text": part, "parse_mode": "HTML", "disable_web_page_preview": True}
            if thread_id is not None:
                body["message_thread_id"] = self.thread_id if thread_id == -1 else thread_id
            if reply_to and n == 0:
                body["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            data = self._call("sendMessage", body)
            if not data or not data.get("ok"):
                return False
            sent = data["result"]["message_id"]
        return sent

    def edit(self, message_id: int, text: str) -> bool:
        """Aggiorna un messaggio già inviato (testo oltre il limite: tagliato)."""
        body = {"chat_id": self.chat_id, "message_id": message_id, "text": text[:LIMIT], "parse_mode": "HTML",
                "disable_web_page_preview": True}
        data = self._call("editMessageText", body)
        return bool(data and data.get("ok"))
