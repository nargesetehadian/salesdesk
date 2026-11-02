"""Plain domain types shared by every layer. No I/O happens here."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

STAGES = ("new", "contacted", "meeting", "proposal", "won", "lost")
CLOSED = ("won", "lost")
EMPLOYEE, CEO = "employee", "ceo"
CONTACT_OUTCOMES = ("answered", "no_answer", "wrong_number", "callback", "interested", "not_interested")
# The customer was actually reached; "no_answer" and "wrong_number" are attempts, not conversations.
EFFECTIVE_OUTCOMES = ("answered", "callback", "interested", "not_interested")
MEETING_STATUSES = ("scheduled", "done", "cancelled", "no_show", "rescheduled")
PRIORITIES = ("low", "medium", "high")
SOURCES = ("referral", "website", "inbound_call", "exhibition", "social", "other")
DEAL_STATUSES = ("open", "won", "lost")


_clock_lock = threading.Lock()
_last_now = datetime.min.replace(tzinfo=timezone.utc)


def utcnow() -> datetime:
    """Current UTC time, strictly increasing within the process.

    Two actions in a row (a call and the stage change it triggers) therefore never share a timestamp,
    which keeps the activity timeline in a deterministic, causal order.
    """
    global _last_now
    with _clock_lock:
        now = datetime.now(timezone.utc)
        if now <= _last_now:
            now = _last_now + timedelta(microseconds=1)
        _last_now = now
        return now


def local_midnight(now: datetime, tz: ZoneInfo) -> datetime:
    return now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)


def iso(dt: datetime) -> str:
    # Microseconds keep same-second events ordered. Strings still sort chronologically against older
    # second/millisecond-precision rows, so existing data needs no conversion.
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass(frozen=True, slots=True)
class User:
    id: int
    platform: str
    ext_id: str
    chat_id: str
    name: str
    role: str
    active: bool

    @property
    def is_ceo(self) -> bool:
        return self.role == CEO


@dataclass(frozen=True, slots=True)
class Lead:
    id: int
    name: str
    company: str
    phone: str
    status: str
    owner_id: int | None
    owner_name: str
    created_at: datetime
    last_contact_at: datetime
    source: str = ""
    next_followup: date | None = None

    @property
    def label(self) -> str:
        return f"{self.name} · {self.company}" if self.company else self.name


@dataclass(frozen=True, slots=True)
class Meeting:
    id: int
    lead_id: int
    lead_name: str
    company: str
    owner_id: int | None
    scheduled_at: datetime
    status: str
    notes: str = ""
    status_at: datetime | None = None
    rescheduled_from: int | None = None
    next_followup: date | None = None


@dataclass(frozen=True, slots=True)
class Task:
    id: int
    owner_id: int
    title: str
    due_date: date
    done: bool
    priority: str = "medium"
    lead_id: int | None = None
    lead_name: str = ""


@dataclass(frozen=True, slots=True)
class Contact:
    id: int
    lead_id: int
    owner_id: int | None
    owner_name: str
    outcome: str
    note: str
    duration_sec: int | None
    next_followup: date | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Note:
    id: int
    lead_id: int
    author_id: int | None
    author_name: str
    body: str
    created_at: datetime
    updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class Deal:
    id: int
    lead_id: int
    lead_name: str
    owner_id: int | None
    title: str
    proposed_amount: int
    final_amount: int | None
    status: str
    notes: str
    created_at: datetime
    closed_at: datetime | None


@dataclass(frozen=True, slots=True)
class TimelineItem:
    kind: str  # contact | meeting | note | stage | deal | task
    at: datetime
    a: str
    b: str
    user_name: str
    c: str = ""
