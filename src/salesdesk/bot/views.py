"""Read-only message builders shared by command handlers, the digest and the reminder job."""

from __future__ import annotations

from datetime import datetime, timedelta

from ..config import Settings
from ..db import Database
from ..domain import User, local_midnight
from . import texts
from .keyboards import btn, inline


def tasks_view(db: Database, cfg: Settings, user: User, now: datetime) -> tuple[str, dict | None]:
    today = local_midnight(now, cfg.timezone).date()
    open_tasks = db.open_tasks(user.id, today)
    text = texts.tasks(
        [t for t in open_tasks if t.due_date == today],
        [t for t in open_tasks if t.due_date < today],
        db.count_upcoming(user.id, today),
        today,
    )
    rows = [[btn(f"✅ {t.title[:40]}", f"td:{t.id}")] for t in open_tasks[:10]]
    rows.append([btn("⏳ همه‌ی باز", "tl:pending"), btn("⚠️ عقب‌افتاده", "tl:overdue"), btn("✔️ انجام‌شده", "tl:done")])
    return text, inline(rows)


def today_meetings_text(db: Database, cfg: Settings, user: User, now: datetime) -> str:
    start = local_midnight(now, cfg.timezone)
    owner = None if user.is_ceo else user.id  # managers see the whole team
    return texts.today(db.meetings_between(start, start + timedelta(days=1), owner), cfg.timezone)


def digest(db: Database, cfg: Settings, user: User, now: datetime) -> tuple[str, dict | None]:
    start = local_midnight(now, cfg.timezone)
    if user.is_ceo:
        meetings = len(db.meetings_between(start, start + timedelta(days=1)))
        overdue = sum(late for _, late in db.task_load(start.date()).values())
        return texts.ceo_digest(meetings, len(db.stale_leads(cfg.stale_days, now)), overdue), None
    tasks_text, markup = tasks_view(db, cfg, user, now)
    return f"☀️ صبح بخیر\n\n{tasks_text}\n\n{today_meetings_text(db, cfg, user, now)}", markup
