"""Messenger adapters.

Telegram and Bale expose the same Bot API shape, so a single hardened HTTP client serves both;
only the base URL and network tuning differ. The rest of the application depends on the small
`Messenger` protocol below and never on a concrete platform.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)
MAX_TEXT = 4096


class ApiError(RuntimeError):
    """Bot API failure. Messages never contain the request URL (it embeds the token)."""

    def __init__(self, method: str, code: int, description: str) -> None:
        super().__init__(f"{method} failed ({code}): {description}")
        self.code, self.description = code, description


@dataclass(frozen=True, slots=True)
class Update:
    platform: str
    chat_id: str
    user_id: str
    name: str
    text: str = ""
    callback_data: str = ""
    callback_id: str = ""
    message_id: int | None = None


class Messenger(Protocol):
    name: str

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]: ...
    def send_message(self, chat_id: str, text: str, markup: dict | None = None) -> int | None: ...
    def edit_message(self, chat_id: str, message_id: int, text: str, markup: dict | None = None) -> None: ...
    def answer_callback(self, callback_id: str, text: str = "") -> None: ...
    def send_document(self, chat_id: str, filename: str, data: bytes, caption: str = "") -> None: ...
    def set_commands(self, commands: list[tuple[str, str]]) -> None: ...


def _display_name(user: dict[str, Any]) -> str:
    full = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
    return full or user.get("username") or str(user.get("id", ""))


def parse_update(platform: str, raw: dict[str, Any]) -> Update | None:
    """Normalise a raw update; anything we do not handle (groups, media, ...) returns None."""
    if cb := raw.get("callback_query"):
        user, msg = cb.get("from") or {}, cb.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id", user.get("id"))
        return Update(platform, str(chat_id), str(user.get("id", "")), _display_name(user),
                      callback_data=cb.get("data") or "", callback_id=str(cb.get("id", "")),
                      message_id=msg.get("message_id"))  # fmt: skip
    msg = raw.get("message")
    if not msg or not msg.get("text"):
        return None
    chat, user = msg.get("chat") or {}, msg.get("from") or {}
    if chat.get("type", "private") != "private":
        return None
    return Update(platform, str(chat["id"]), str(user.get("id", chat["id"])), _display_name(user),
                  text=msg["text"], message_id=msg.get("message_id"))  # fmt: skip


# platform -> (API host, force IPv4). Bale gateways stall on IPv6 / HTTP2 from some Iranian networks.
PRESETS = {"telegram": ("https://api.telegram.org", False), "bale": ("https://tapi.bale.ai", True)}


class BotApiClient:
    def __init__(self, name: str, token: str, *, base_url: str | None = None, force_ipv4: bool | None = None) -> None:
        import httpx  # imported lazily so the pure logic stays importable without it

        host, ipv4 = PRESETS[name]
        self.name = name
        self._httpx = httpx
        self._url = f"{base_url or host}/bot{token}"
        transport = httpx.HTTPTransport(
            retries=2, local_address="0.0.0.0" if (ipv4 if force_ipv4 is None else force_ipv4) else None  # noqa: S104
        )  # binding to 0.0.0.0 makes the OS pick an IPv4 source address, so only A records are used
        self._http = httpx.Client(
            transport=transport, http2=False, headers={"User-Agent": "SalesDesk/1.0"},
            timeout=httpx.Timeout(30.0, connect=15.0),
        )  # fmt: skip

    def call(self, method: str, payload: dict | None = None, *, files: dict | None = None, timeout: float = 30) -> Any:
        try:
            if files:
                response = self._http.post(f"{self._url}/{method}", data=payload, files=files, timeout=timeout)
            else:
                response = self._http.post(f"{self._url}/{method}", json=payload or {}, timeout=timeout)
            body = response.json()
        except (self._httpx.HTTPError, ValueError) as exc:
            raise ApiError(method, 0, type(exc).__name__) from None  # drop the chained error: it may embed the URL
        if not body.get("ok"):
            raise ApiError(method, body.get("error_code", response.status_code), str(body.get("description", "")))
        return body.get("result")

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self.call("getUpdates", payload, timeout=timeout + 10) or []

    def send_message(self, chat_id: str, text: str, markup: dict | None = None) -> int | None:
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text[:MAX_TEXT]}
        if markup:
            payload["reply_markup"] = markup
        return (self.call("sendMessage", payload) or {}).get("message_id")

    def edit_message(self, chat_id: str, message_id: int, text: str, markup: dict | None = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id, "text": text[:MAX_TEXT]}
        if markup:
            payload["reply_markup"] = markup
        try:
            self.call("editMessageText", payload)
        except ApiError as exc:
            if "not modified" not in exc.description.lower():
                raise

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    def send_document(self, chat_id: str, filename: str, data: bytes, caption: str = "") -> None:
        self.call("sendDocument", {"chat_id": chat_id, "caption": caption}, files={"document": (filename, data)}, timeout=60)

    def set_commands(self, commands: list[tuple[str, str]]) -> None:
        self.call("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in commands]})
