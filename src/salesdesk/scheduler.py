"""Background jobs: meeting reminders, the morning digest and daily database backups.

Delivery model: *claim, send, release on failure*. A claim is an atomic database write, so two
ticks (or two processes) can never both send the same notification; if the send fails the claim is
released and the notification is retried with exponential backoff. A crash between claim and send
loses at most that single notification (never duplicates it).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta

from .bot import texts, views
from .bot.keyboards import btn, inline
from .config import Settings
from .db import Database
from .domain import utcnow
from .platforms import ApiError, Messenger

log = logging.getLogger(__name__)
PERMANENT_ERRORS = {400, 401, 403, 404}  # blocked bot, unknown chat, ...: retrying cannot help


@dataclass(slots=True)
class _Attempt:
    count: int = 0
    next_at: datetime | None = None
    gave_up: bool = False


class Backoff:
    """In-memory retry schedule per delivery key: 1, 2, 4 ... minutes (capped), then give up."""

    def __init__(self, base: timedelta = timedelta(minutes=1), cap: timedelta = timedelta(minutes=30), max_attempts: int = 8) -> None:
        self.base, self.cap, self.max_attempts = base, cap, max_attempts
        self._state: dict[str, _Attempt] = {}

    def ready(self, key: str, now: datetime) -> bool:
        state = self._state.get(key)
        return state is None or (not state.gave_up and (state.next_at is None or now >= state.next_at))

    def failed(self, key: str, now: datetime, *, permanent: bool = False) -> bool:
        """Record a failure; returns True when the key will not be retried any more."""
        state = self._state.setdefault(key, _Attempt())
        state.count += 1
        state.gave_up = permanent or state.count >= self.max_attempts
        state.next_at = now + min(self.base * 2 ** (state.count - 1), self.cap)
        return state.gave_up

    def succeeded(self, key: str) -> None:
        self._state.pop(key, None)


class Scheduler(threading.Thread):
    """Ticks every `interval` seconds. Jobs are independent: one failing never stops the others."""

    def __init__(self, db: Database, cfg: Settings, messengers: Mapping[str, Messenger], stop: threading.Event, interval: float = 30.0) -> None:
        super().__init__(name="scheduler", daemon=True)
        self.db, self.cfg, self.messengers, self.stop_event, self.interval = db, cfg, messengers, stop, interval
        self.backoff = Backoff()

    def run(self) -> None:
        log.info("Scheduler started (reminders %d min ahead, digest from %02d:00 %s)", self.cfg.reminder_minutes, self.cfg.digest_hour, self.cfg.timezone)
        while not self.stop_event.wait(self.interval):
            self.tick()

    @property
    def jobs(self) -> list[tuple[str, Callable[[datetime], None]]]:
        return [("reminders", self.send_reminders), ("digest", self.send_digest), ("backup", self.backup)]

    def tick(self, now: datetime | None = None) -> dict[str, bool]:
        """Run every job; returns {job: succeeded}. Errors are logged with a traceback, never swallowed silently."""
        now = now or utcnow()
        outcome = {}
        for name, job in self.jobs:
            try:
                job(now)
                outcome[name] = True
            except Exception:
                log.exception("Scheduler job %r failed; the other jobs continue", name)
                outcome[name] = False
        return outcome

    # delivery ------------------------------------------------------------------
    def _deliver(self, key: str, now: datetime, send: Callable[[], None], undo: Callable[[], None]) -> bool:
        """Send once; on failure undo the claim and schedule a retry. Returns True if delivered."""
        try:
            send()
        except ApiError as exc:
            undo()
            gave_up = self.backoff.failed(key, now, permanent=exc.code in PERMANENT_ERRORS)
            log.warning("Delivery %s failed (%s)%s", key, exc, "; giving up" if gave_up else "; will retry")
            return False
        except Exception:
            undo()
            self.backoff.failed(key, now)
            log.exception("Delivery %s crashed while building the message", key)
            return False
        self.backoff.succeeded(key)
        return True

    def send_reminders(self, now: datetime) -> None:
        for meeting in self.db.due_reminders(now, timedelta(minutes=self.cfg.reminder_minutes)):
            key = f"reminder:{meeting.id}"
            owner = self.db.get_user(meeting.owner_id) if meeting.owner_id else None
            messenger = self.messengers.get(owner.platform) if owner and owner.active else None
            if messenger is None or not self.backoff.ready(key, now) or not self.db.claim_reminder(meeting.id, now):
                continue
            minutes = max(1, round((meeting.scheduled_at - now).total_seconds() / 60))
            text = texts.reminder(meeting, minutes, self.cfg.timezone)
            markup = inline([[btn("📅 جزئیات قرار", f"mg:{meeting.id}")]])
            self._deliver(key, now, lambda: messenger.send_message(owner.chat_id, text, markup), lambda: self.db.release_reminder(meeting.id))

    def send_digest(self, now: datetime) -> None:
        local = now.astimezone(self.cfg.timezone)
        if local.hour < self.cfg.digest_hour:
            return
        day = local.date().isoformat()
        for user in self.db.users(active_only=True):
            messenger = self.messengers.get(user.platform)
            key = f"digest:{user.id}:{day}"
            if messenger is None or not self.backoff.ready(key, now) or not self.db.claim_notification(key, user.id):
                continue

            def send(user=user, messenger=messenger) -> None:  # noqa: ANN001
                text, markup = views.digest(self.db, self.cfg, user, now)
                messenger.send_message(user.chat_id, text, markup)

            self._deliver(key, now, send, lambda key=key: self.db.release_notification(key))

    def backup(self, now: datetime) -> None:
        if self.cfg.backup_keep <= 0:
            return
        day = now.astimezone(self.cfg.timezone).date().isoformat()
        key = f"backup:{day}"
        if self.db.kv_get("backup:last") == day or not self.backoff.ready(key, now):
            return
        try:
            self.db.backup_to(self.cfg.backup_dir / f"salesdesk-{day}.db")  # verified before it is published
        except Exception:
            self.backoff.failed(key, now)
            raise
        self.backoff.succeeded(key)
        self.db.kv_set("backup:last", day)
        for old in sorted(self.cfg.backup_dir.glob("salesdesk-*.db"))[: -self.cfg.backup_keep]:
            old.unlink(missing_ok=True)
        self.db.prune_notifications()


