"""Conversation engine: routing, persistent per-chat state machine, access gate and polling loop."""

from __future__ import annotations

import hmac
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from typing import Any

from ..config import Settings
from ..db import Database
from ..domain import CEO, EMPLOYEE, User, utcnow
from ..platforms import ApiError, Messenger, Update, parse_update
from . import texts

log = logging.getLogger(__name__)
Handler = Callable[["Ctx", str], None]


class Router:
    """Registry mapping commands, callback prefixes and FSM states to handler functions."""

    def __init__(self) -> None:
        self.commands: dict[str, Handler] = {}
        self.callbacks: dict[str, Handler] = {}
        self.states: dict[str, Handler] = {}

    def _register(self, table: dict[str, Handler], *keys: str) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            for key in keys:
                if key in table:
                    raise ValueError(f"handler for {key!r} is already registered ({table[key].__module__}.{table[key].__name__})")
                table[key] = fn
            return fn

        return deco

    def command(self, *names: str):  # noqa: ANN201
        return self._register(self.commands, *names)

    def callback(self, prefix: str):  # noqa: ANN201
        return self._register(self.callbacks, prefix)

    def state(self, name: str):  # noqa: ANN201
        return self._register(self.states, name)


class Ctx:
    """Everything a handler needs: who is talking, where to reply, and the persisted FSM state."""

    def __init__(self, engine: Engine, messenger: Messenger, update: Update, user: User, state: str, data: dict[str, Any]) -> None:
        self.engine, self.messenger, self.update, self.user = engine, messenger, update, user
        self.state, self.data = state, data
        self._answered = False

    @property
    def db(self) -> Database:
        return self.engine.db

    @property
    def cfg(self) -> Settings:
        return self.engine.cfg

    def now(self) -> datetime:
        return utcnow()

    def today(self) -> date:
        return self.now().astimezone(self.cfg.timezone).date()

    def reply(self, text: str, markup: dict | None = None) -> None:
        self.messenger.send_message(self.update.chat_id, text, markup)

    def edit(self, text: str, markup: dict | None = None) -> None:
        """Edit the message that carried the pressed button; fall back to a new message."""
        if self.update.message_id is None or not self.update.callback_data:
            return self.reply(text, markup)
        try:
            self.messenger.edit_message(self.update.chat_id, self.update.message_id, text, markup)
        except ApiError:
            self.reply(text, markup)

    def answer(self, text: str = "") -> None:
        if self.update.callback_id and not self._answered:
            self._answered = True
            try:
                self.messenger.answer_callback(self.update.callback_id, text)
            except ApiError:
                log.debug("answerCallbackQuery failed", exc_info=True)

    def goto(self, state: str, **data: Any) -> None:
        self.state = state
        self.data.update(data)

    def finish(self) -> None:
        self.state = ""
        self.data = {}

    def can_manage(self, owner_id: int | None) -> bool:
        return self.user.is_ceo or owner_id == self.user.id


class Engine:
    def __init__(self, db: Database, settings: Settings, router: Router) -> None:
        self.db, self.cfg, self.router = db, settings, router

    def handle(self, messenger: Messenger, update: Update) -> None:
        """Process one update. Never raises: a bad update must not stop the polling loop."""
        try:
            self._dispatch(messenger, update)
        except Exception:
            log.exception("Unhandled error for %s chat %s", update.platform, update.chat_id)
            self.db.save_session(update.platform, update.chat_id, "", {})
            try:
                messenger.send_message(update.chat_id, texts.ERROR)
            except ApiError:
                pass

    def _dispatch(self, messenger: Messenger, u: Update) -> None:
        if u.text.strip().lower().split("@")[0] == "/id":  # bootstrap helper: needs no account
            messenger.send_message(u.chat_id, f"شناسه‌ی تو: {u.platform}:{u.user_id}")
            return
        ceo = self.cfg.is_ceo(u.platform, u.user_id)
        user = self.db.upsert_user(u.platform, u.user_id, u.chat_id, u.name, CEO if ceo else EMPLOYEE,
                                   active=ceo or not self.cfg.team_code)  # fmt: skip
        if ceo and not user.active:
            self.db.set_active(user.id, True)
            user = replace(user, active=True)
        state, data = self.db.get_session(u.platform, u.chat_id)
        ctx = Ctx(self, messenger, u, user, state, data)

        if not user.active:
            self._gate(ctx)
        elif u.callback_data:
            prefix, _, arg = u.callback_data.partition(":")
            if handler := self.router.callbacks.get(prefix):
                handler(ctx, arg)
            ctx.answer()
        else:
            self._route_text(ctx, u.text.strip())
        self.db.save_session(u.platform, u.chat_id, ctx.state, ctx.data)

    def _gate(self, ctx: Ctx) -> None:
        text = ctx.update.text.strip()
        candidate = text.split(maxsplit=1)[1] if text.startswith("/start ") else text
        if self.cfg.team_code and hmac.compare_digest(candidate.encode(), self.cfg.team_code.encode()):
            self.db.set_active(ctx.user.id, True)
            ctx.user = replace(ctx.user, active=True)
            self.router.commands["start"](ctx, "")
        else:
            ctx.reply(texts.BAD_CODE if text and not text.startswith("/") else texts.ASK_CODE)

    def _route_text(self, ctx: Ctx, text: str) -> None:
        command: tuple[str, str] | None = None
        if text.startswith("/"):
            head, _, arg = text[1:].partition(" ")
            command = (head.split("@")[0].lower(), arg.strip())
        elif text in texts.MENU:
            command = (texts.MENU[text], "")

        if command:
            handler = self.router.commands.get(command[0])
            if handler is None:
                return ctx.reply(texts.HINT)
            ctx.finish()  # any command abandons the current flow
            handler(ctx, command[1])
        elif ctx.state and (handler := self.router.states.get(ctx.state)):
            handler(ctx, text)
        else:
            ctx.finish()
            ctx.reply(texts.HINT)


class Poller(threading.Thread):
    """Long-polls one messenger and feeds updates to the engine; retries with exponential backoff."""

    def __init__(self, messenger: Messenger, engine: Engine, stop: threading.Event) -> None:
        super().__init__(name=f"poller-{messenger.name}", daemon=True)
        self.messenger, self.engine, self.stop_event = messenger, engine, stop

    def run(self) -> None:
        offset: int | None = None
        delay = 1.0
        log.info("Polling %s", self.messenger.name)
        while not self.stop_event.is_set():
            try:
                for raw in self.messenger.get_updates(offset, 25):
                    if "update_id" not in raw:
                        continue
                    offset = raw["update_id"] + 1  # advance first: a poison update is skipped, never replayed
                    try:
                        update = parse_update(self.messenger.name, raw)
                    except (KeyError, TypeError, ValueError, AttributeError):
                        log.warning("Skipping malformed update %s on %s", raw["update_id"], self.messenger.name, exc_info=True)
                        continue
                    if update:
                        self.engine.handle(self.messenger, update)
                delay = 1.0
            except ApiError as exc:
                log.warning("%s polling error: %s (retry in %.0fs)", self.messenger.name, exc, delay)
                self.stop_event.wait(delay)
                delay = min(delay * 2, 60.0)
            except Exception:
                log.exception("Unexpected polling failure on %s", self.messenger.name)
                time.sleep(5)
