from datetime import datetime, timedelta

from harness import FakeMessenger, make_settings

from salesdesk.config import ConfigError, Settings
from salesdesk.db import Database
from salesdesk.domain import utcnow
from salesdesk.scheduler import Scheduler


def _setup(tmp_path, **overrides):
    cfg, messenger = make_settings(tmp_path, **overrides), FakeMessenger()
    db = Database(cfg.db_path)
    user = db.upsert_user("bale", "1", "1", "سارا", "employee", active=True)
    sched = Scheduler(db, cfg, {"bale": messenger}, stop=None)  # type: ignore[arg-type]
    return db, cfg, messenger, user, sched


def test_reminder_is_sent_once_and_rearmed_after_reschedule(tmp_path):
    db, _, m, user, sched = _setup(tmp_path, digest_hour=23, backup_keep=0)
    lead = db.add_lead("رضایی", "نوین", "", user.id)
    now = utcnow()
    meeting_id = db.add_meeting(lead.id, now + timedelta(minutes=30), user.id)
    db.add_meeting(lead.id, now + timedelta(hours=5), user.id)  # outside the window
    sched.tick(now)
    reminders = [e for e in m.events if "یادآوری" in e[2]]
    assert len(reminders) == 1 and "رضایی" in reminders[0][2] and reminders[0][1] == "1"
    sched.tick(now + timedelta(seconds=30))
    assert len([e for e in m.events if "یادآوری" in e[2]]) == 1  # no duplicates
    db.reschedule_meeting(meeting_id, now + timedelta(minutes=50), user.id)
    sched.tick(now + timedelta(minutes=1))
    assert len([e for e in m.events if "یادآوری" in e[2]]) == 2


def test_failed_reminder_is_retried_and_cancelled_meetings_are_skipped(tmp_path):
    db, _, m, user, sched = _setup(tmp_path, digest_hour=23, backup_keep=0)
    lead = db.add_lead("رضایی", "", "", user.id)
    now = utcnow()
    db.add_meeting(lead.id, now + timedelta(minutes=20), user.id)
    cancelled = db.add_meeting(lead.id, now + timedelta(minutes=25), user.id)
    db.cancel_meeting(cancelled, user.id)
    m.fail = True
    sched.tick(now)  # must not raise
    assert m.events == []
    m.fail = False
    sched.tick(now + timedelta(seconds=30))
    assert m.events == []  # still inside the retry backoff
    sched.tick(now + timedelta(minutes=2))
    assert len(m.events) == 1


def test_digest_is_sent_once_per_day_after_the_configured_hour(tmp_path):
    db, cfg, m, user, sched = _setup(tmp_path, digest_hour=8, backup_keep=0)
    db.upsert_user("bale", "9", "9", "مدیر", "ceo", active=True)
    tz = cfg.timezone
    sched.tick(datetime(2026, 10, 31, 7, 59, tzinfo=tz))
    assert m.events == []
    sched.tick(datetime(2026, 10, 31, 8, 1, tzinfo=tz))
    assert {e[1] for e in m.events} == {"1", "9"}
    sched.tick(datetime(2026, 10, 31, 12, 0, tzinfo=tz))
    assert len(m.events) == 2
    sched.tick(datetime(2026, 11, 1, 8, 5, tzinfo=tz))
    assert len(m.events) == 4


def test_backups_are_created_daily_and_pruned(tmp_path):
    db, cfg, _, _, sched = _setup(tmp_path, digest_hour=23, backup_keep=2)
    tz = cfg.timezone
    for day in (1, 2, 3):
        sched.tick(datetime(2026, 11, day, 12, 0, tzinfo=tz))
    names = sorted(p.name for p in cfg.backup_dir.glob("*.db"))
    assert names == ["salesdesk-2026-11-02.db", "salesdesk-2026-11-03.db"]
    assert Database(cfg.backup_dir / names[-1]).health()


def test_production_config_is_validated():
    try:
        Settings(env="production", bale_token="t").validate()
    except ConfigError as exc:
        message = str(exc)
        assert "DASHBOARD_KEY" in message and "TEAM_CODE" in message and "CEO_IDS" in message
    else:
        raise AssertionError("production config without secrets must be rejected")
    Settings(env="production", bale_token="t", dashboard_key="x" * 16, team_code="c", ceo_ids=frozenset({("*", "1")})).validate()
    assert Settings(ceo_ids=frozenset({("*", "7")})).is_ceo("bale", "7")
    assert not Settings(ceo_ids=frozenset({("bale", "7")})).is_ceo("telegram", "7")
