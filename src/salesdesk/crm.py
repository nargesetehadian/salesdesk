"""Customer business logic that knows nothing about messengers or message text."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from .db import Database
from .domain import Lead, Meeting, TimelineItem

FOLLOWUP_CHOICES = {"1": 1, "3": 3, "7": 7}  # callback value -> days from today
DURATION_CHOICES_MIN = (1, 5, 10, 30)


@dataclass(frozen=True, slots=True)
class CustomerProfile:
    """Everything the customer page needs, assembled in one place."""

    lead: Lead
    counts: dict[str, int]  # contacts, meetings, notes
    upcoming_meetings: list[Meeting]
    recent_activity: list[TimelineItem]  # newest first

    @property
    def last_activity(self) -> datetime:
        return self.recent_activity[0].at if self.recent_activity else self.lead.created_at


def build_profile(db: Database, lead_id: int, now: datetime, recent: int = 4) -> CustomerProfile | None:
    lead = db.get_lead(lead_id)
    if lead is None:
        return None
    return CustomerProfile(lead, db.lead_counts(lead_id), db.lead_meetings(lead_id, now), db.lead_timeline(lead_id, recent))


def followup_date(today: date, choice: str) -> date | None:
    """Map a follow-up button ('x' = none, '1' / '3' / '7' = days ahead) to a date."""
    days = FOLLOWUP_CHOICES.get(choice)
    return today + timedelta(days=days) if days else None
