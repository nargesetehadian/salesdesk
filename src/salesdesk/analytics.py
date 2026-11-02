"""Read-only aggregation that powers the dashboard API."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any

from .config import Settings
from .db import Database
from .domain import iso, local_midnight, utcnow

FUNNEL = ("new", "contacted", "meeting", "proposal", "won")
RANGES = {"today": 1, "7d": 7, "30d": 30, "90d": 90}
METRICS = {"lead": "leads", "call": "calls", "meeting": "meetings", "stage_won": "won", "task_done": "tasks_done"}


def build_summary(
    db: Database, settings: Settings, now: datetime | None = None, range_key: str = "30d"
) -> dict[str, Any]:
    now = now or utcnow()
    today = local_midnight(now, settings.timezone)
    counts = db.status_counts()

    # A lead that reached "proposal" has also passed every earlier stage.
    funnel = [
        {"stage": stage, "count": sum(counts.get(s, 0) for s in FUNNEL[i:])}
        for i, stage in enumerate(FUNNEL)
    ]

    created = Counter(
        d.astimezone(settings.timezone).date() for d in db.leads_created_since(today - timedelta(days=6))
    )
    trend = [
        {"date": (today.date() - timedelta(days=i)).isoformat(),
         "count": created.get(today.date() - timedelta(days=i), 0)}
        for i in range(6, -1, -1)
    ]  # fmt: skip

    meetings = db.meetings_between(today, today + timedelta(days=7))
    stale = db.stale_leads(settings.stale_days, now)
    open_total = sum(n for s, n in counts.items() if s not in ("won", "lost"))
    tomorrow = today + timedelta(days=1)

    return {
        "generated_at": iso(now),
        "timezone": str(settings.timezone),
        "stale_days": settings.stale_days,
        "kpis": {
            "open_leads": open_total,
            "meetings_today": sum(today <= m.scheduled_at < tomorrow for m in meetings),
            "meetings_week": len(meetings),
            "stale": len(stale),
            "won": counts.get("won", 0),
        },
        "funnel": funnel,
        "trend": trend,
        "performance": build_performance(db, settings, range_key, now),
        "meetings": [
            {"id": m.id, "name": m.lead_name, "company": m.company, "at": iso(m.scheduled_at)}
            for m in meetings
        ],
        "stale": [
            {"id": lead.id, "name": lead.name, "company": lead.company, "phone": lead.phone,
             "days": (now - lead.last_contact_at).days}
            for lead in stale
        ],
    }  # fmt: skip


def build_performance(
    db: Database, settings: Settings, range_key: str, now: datetime
) -> dict[str, Any]:
    """Team activity for a time range, compared with the previous range of equal length."""
    days = RANGES.get(range_key, 30)
    range_key = range_key if range_key in RANGES else "30d"
    tz = settings.timezone
    today = local_midnight(now, settings.timezone)
    start = today - timedelta(days=days - 1)
    step = 1 if days <= 31 else 7  # weekly buckets keep the 90-day chart readable
    buckets = -(-days // step)

    totals = {name: {"value": 0, "prev": 0} for name in METRICS.values()}
    per_owner: dict[int | None, Counter] = defaultdict(Counter)
    series = [Counter() for _ in range(buckets)]

    for kind, owner, at in db.events_between(start - timedelta(days=days), today + timedelta(days=1)):
        name = METRICS.get(kind)
        if name is None:
            continue
        if at < start:
            totals[name]["prev"] += 1
            continue
        totals[name]["value"] += 1
        per_owner[owner][name] += 1
        series[(at.astimezone(tz).date() - start.date()).days // step][name] += 1

    load = db.task_load(today.date())
    team = []
    for user in db.users():
        if user.is_ceo:
            continue
        open_n, late_n = load.get(user.id, (0, 0))
        team.append({"name": user.name, **{m: per_owner[user.id][m] for m in METRICS.values()},
                     "tasks_open": open_n, "tasks_overdue": late_n})  # fmt: skip
    team.sort(key=lambda t: (t["won"], t["meetings"], t["calls"]), reverse=True)

    return {
        "range": range_key,
        "days": days,
        "totals": totals,
        "series": [
            {"date": (start + timedelta(days=i * step)).date().isoformat(), **{m: c[m] for m in METRICS.values()}}
            for i, c in enumerate(series)
        ],
        "team": team,
    }
