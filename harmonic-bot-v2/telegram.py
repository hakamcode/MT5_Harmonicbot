"""Minimal Telegram Bot API sender (photo + caption, long text split into a follow-up message)."""
from __future__ import annotations

import logging
import requests

log = logging.getLogger("telegram")


class Telegram:
    def __init__(self, token: str, chat_id: str, dry_run: bool = False):
        token = str(token or "").strip().strip('"').strip()
        if token.lower().startswith("bot"):          # people often paste "bot123:ABC..."
            token = token[3:]
        self.url = f"https://api.telegram.org/bot{token}"
        self.chat_id = str(chat_id).strip()
        self.dry_run = dry_run or not token

    def check(self) -> str | None:
        """Return a plain-language problem with the token/chat id, or None if Telegram accepts them."""
        if self.dry_run:
            return None
        token = self.url.rsplit("/bot", 1)[1]
        if "PASTE" in token:
            return "config.json still has the example bot_token. Replace PASTE_TOKEN_FROM_BOTFATHER with your real token."
        if ":" not in token or not token.split(":", 1)[0].isdigit():
            return ("The bot_token in config.json doesn't look like a Telegram token. It must be numbers, a colon, "
                    "then letters (123456789:AAH...). Check you didn't paste the chat_id or a username there.")
        r = requests.get(f"{self.url}/getMe", timeout=30)
        if r.status_code in (401, 404):
            return ("Telegram rejected the bot_token in config.json. Copy it again from @BotFather "
                    "(it looks like 123456789:AAH...; no spaces, no quotes inside, no chat id).")
        if self.chat_id == token.split(":", 1)[0]:
            return ("chat_id in config.json is your bot's own number (the part before the colon in the token). "
                    "Put YOUR number there instead: message @userinfobot to get it, and press Start on your bot.")
        r = requests.post(f"{self.url}/getChat", data={"chat_id": self.chat_id}, timeout=30)
        if not r.ok:
            return ("Telegram doesn't know the chat_id in config.json. Open your bot in Telegram, press Start, "
                    "and check chat_id is the number @userinfobot shows you.")
        return None

    def _post(self, method: str, data: dict, files: dict | None = None):
        if self.dry_run:
            log.info("[dry-run] %s %s", method, (data.get("caption") or data.get("text") or "")[:80])
            return
        r = requests.post(f"{self.url}/{method}", data=data, files=files, timeout=30)
        if not r.ok:
            log.error("telegram %s failed: %s", method, r.text[:300])

    def text(self, msg: str):
        for i in range(0, len(msg), 4000):
            self._post("sendMessage", {"chat_id": self.chat_id, "text": msg[i:i + 4000], "parse_mode": "HTML"})

    def photo(self, path: str, caption: str):
        cap, rest = (caption, "") if len(caption) <= 1024 else ("", caption)
        with open(path, "rb") as f:
            self._post("sendPhoto", {"chat_id": self.chat_id, "caption": cap, "parse_mode": "HTML"},
                       files={"photo": f})
        if rest:
            self.text(rest)
