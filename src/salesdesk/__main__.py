"""Composition root: wires settings, storage, platforms, engine, scheduler and web server."""

from __future__ import annotations

import logging
import signal
import threading

from .bot import texts
from .bot.engine import Engine, Poller
from .bot.handlers import router
from .config import Settings
from .db import Database
from .platforms import ApiError, BotApiClient
from .scheduler import Scheduler
from .web import start_in_background

log = logging.getLogger("salesdesk")


def main() -> None:
    settings = Settings.from_env()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore"):  # their request logs contain the bot token
        logging.getLogger(noisy).setLevel(logging.WARNING)

    db = Database(settings.db_path)
    engine = Engine(db, settings, router)
    stop = threading.Event()
    messengers = {name: BotApiClient(name, token) for name, token in settings.tokens.items()}

    for messenger in messengers.values():
        try:
            messenger.set_commands(texts.COMMANDS)
        except ApiError as exc:  # cosmetic only: never block startup
            log.warning("Could not publish command list on %s: %s", messenger.name, exc)

    workers = [Poller(m, engine, stop) for m in messengers.values()] + [Scheduler(db, settings, messengers, stop)]
    web = start_in_background(db, settings)
    for worker in workers:
        worker.start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info("SalesDesk %s running on: %s", settings.env, ", ".join(messengers))
    stop.wait()
    log.info("Shutting down")
    web.shutdown()
