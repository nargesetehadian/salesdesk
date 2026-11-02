"""Test doubles: a recording Messenger and a Chat that behaves like a person using the bot."""

from __future__ import annotations

import itertools
import os
from contextlib import contextmanager

from salesdesk.bot.engine import Engine
from salesdesk.config import Settings
from salesdesk.db import Database
from salesdesk.platforms import ApiError, Update


class FakeMessenger:
    name = "bale"

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str, dict | None]] = []  # (kind, chat_id, text, markup)
        self.documents: list[tuple[str, str, bytes]] = []
        self.answers: list[str] = []
        self.fail = False
        self.fail_chats: set[str] = set()
        self.fail_code = 500
        self.attempts: list[str] = []  # chat id of every send attempt, failed or not

    def send_message(self, chat_id, text, markup=None):  # noqa: ANN001, ANN201
        self.attempts.append(chat_id)
        if self.fail or chat_id in self.fail_chats:
            raise ApiError("sendMessage", self.fail_code, "boom")
        self.events.append(("send", chat_id, text, markup))
        return len(self.events)

    def edit_message(self, chat_id, message_id, text, markup=None):  # noqa: ANN001, ANN201
        self.events.append(("edit", chat_id, text, markup))

    def answer_callback(self, callback_id, text=""):  # noqa: ANN001, ANN201
        self.answers.append(text)

    def send_document(self, chat_id, filename, data, caption=""):  # noqa: ANN001, ANN201
        self.documents.append((chat_id, filename, data))

    def set_commands(self, commands):  # noqa: ANN001, ANN201
        pass

    def get_updates(self, offset, timeout):  # noqa: ANN001, ANN201
        return []


class Chat:
    """One simulated user. `say` types text; `click` presses an inline button by (part of) its label."""

    _ids = itertools.count(1)

    def __init__(self, engine: Engine, messenger: FakeMessenger, user_id: str, name: str = "سارا محمدی") -> None:
        self.engine, self.m, self.id, self.name = engine, messenger, user_id, name

    def say(self, text: str) -> Chat:
        self.engine.handle(self.m, Update("bale", self.id, self.id, self.name, text=text, message_id=next(self._ids)))
        return self

    def press(self, data: str) -> Chat:
        self.engine.handle(self.m, Update("bale", self.id, self.id, self.name, callback_data=data, callback_id="cb", message_id=1))
        return self

    @property
    def mine(self) -> list[tuple[str, str, str, dict | None]]:
        return [e for e in self.m.events if e[1] == self.id]

    @property
    def last(self) -> str:
        return self.mine[-1][2]

    def labels(self) -> list[str]:
        markup = self.mine[-1][3] or {}
        return [b["text"] for row in markup.get("inline_keyboard", []) for b in row]

    def click(self, part: str) -> Chat:
        markup = self.mine[-1][3] or {}
        for row in markup.get("inline_keyboard", []):
            for button in row:
                if part in button["text"]:
                    return self.press(button["callback_data"])
        raise AssertionError(f"no button containing {part!r}; have {self.labels()}")


def make_settings(tmp_path, **overrides) -> Settings:  # noqa: ANN001
    return Settings(db_path=tmp_path / "t.db", backup_dir=tmp_path / "backups", **overrides)


def make_bot(tmp_path, **overrides):  # noqa: ANN001, ANN201
    from salesdesk.bot.handlers import router

    settings = make_settings(tmp_path, **overrides)
    db = Database(settings.db_path)
    return db, settings, Engine(db, settings, router), FakeMessenger()




_APP_PREFIXES = ("APP_", "BALE_", "TELEGRAM_", "DB_", "DEMO_", "CEO_", "TEAM_", "DASHBOARD_", "TIMEZONE", "DIGEST_", "REMINDER_", "STALE_", "WEB_", "BACKUP_", "LOG_", "PUBLIC_")


@contextmanager
def env(**values):  # noqa: ANN201
    """Run with a clean SalesDesk environment plus `values`; restores everything afterwards."""
    saved, cwd = dict(os.environ), os.getcwd()
    for key in [k for k in os.environ if k.startswith(_APP_PREFIXES)]:
        del os.environ[key]
    os.environ.update(values)
    try:
        yield
    finally:
        os.chdir(cwd)
        os.environ.clear()
        os.environ.update(saved)
