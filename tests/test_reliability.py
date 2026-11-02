"""Scheduler reliability: retries, no duplicates, job isolation, backups, time zones."""

from datetime import datetime, timedelta, timezone

from harness import FakeMessenger, make_settings

from salesdesk.db import Database
from salesdesk.domain import local_midnight, utcnow
from salesdesk.fa import format_stamp
from salesdesk.scheduler import Backoff, Scheduler


def _setup(tmp_path, **overrides):
    overrides.setdefault("backup_keep", 0)
    cfg, m = make_settings(tmp_path, **overrides), FakeMessenger()
    db = Database(cfg.db_path)
    sara = db.upsert_user("bale", "1", "1", "سارا", "employee", active=True)
    ceo = db.upsert_user("bale", "9", "9", "مدیر", "ceo", active=True)
    return db, cfg, m, sara, ceo, Scheduler(db, cfg, {"bale": m}, stop=None)  # type: ignore[arg-type]


def _digests(m, chat):
    return [e for e in m.events if e[1] == chat and "صبح بخیر" in e[2]]


# --- digest retry and deduplication -----------------------------------------------------
def test_failed_digest_is_retried_for_that_user_only(tmp_path):
    db, cfg, m, *_, sched = _setup(tmp_path, digest_hour=8)
    at = lambda h, mi: datetime(2026, 10, 31, h, mi, tzinfo=cfg.timezone)  # noqa: E731
    m.fail_chats = {"1"}
    sched.tick(at(8, 1))
    assert len(_digests(m, "9")) == 1 and _digests(m, "1") == []  # one failure does not block the others
    m.fail_chats = set()
    sched.tick(at(8, 1) + timedelta(seconds=20))
    assert _digests(m, "1") == []  # inside the backoff window
    sched.tick(at(8, 4))
    assert len(_digests(m, "1")) == 1 and len(_digests(m, "9")) == 1  # delivered once, no duplicate for the CEO
    for minute in (10, 30):
        sched.tick(at(8, minute))
    assert len(_digests(m, "1")) == 1


def test_permanent_error_is_attempted_exactly_once(tmp_path):
    db, cfg, m, *_, sched = _setup(tmp_path, digest_hour=0)
    m.fail_chats, m.fail_code = {"1"}, 403  # the user blocked the bot
    start = datetime(2026, 10, 31, 9, 0, tzinfo=cfg.timezone)
    for i in range(10):
        sched.tick(start + timedelta(minutes=i * 40))
    assert m.attempts.count("1") == 1  # no pointless retries
    assert len(_digests(m, "9")) == 1  # everyone else unaffected


def test_transient_errors_stop_after_the_attempt_limit(tmp_path):
    db, cfg, m, *_, sched = _setup(tmp_path, digest_hour=0)
    m.fail_chats, m.fail_code = {"1"}, 500  # flaky network
    now = datetime(2026, 10, 31, 0, 30, tzinfo=cfg.timezone)
    for _ in range(40):  # far more ticks than attempts, spaced beyond the backoff cap
        now += timedelta(minutes=31)
        sched.tick(now)
        if now.date().isoformat() != "2026-10-31":
            break
    assert m.attempts.count("1") == 8  # Backoff.max_attempts, then it gives up for the day


def test_two_scheduler_instances_never_double_send(tmp_path):
    db, cfg, m, sara, _, first = _setup(tmp_path, digest_hour=8)
    second = Scheduler(db, cfg, {"bale": m}, stop=None)  # type: ignore[arg-type]
    lead = db.add_lead("رضایی", "", "", sara.id)
    now = datetime(2026, 10, 31, 8, 30, tzinfo=cfg.timezone)
    db.add_meeting(lead.id, now + timedelta(minutes=20), sara.id)
    first.tick(now)
    second.tick(now)  # a second process racing the first
    assert len(_digests(m, "1")) == 1 and len(_digests(m, "9")) == 1
    assert len([e for e in m.events if "یادآوری" in e[2]]) == 1


def test_digest_is_not_resent_after_a_restart_and_waits_for_the_hour(tmp_path):
    db, cfg, m, *_, sched = _setup(tmp_path, digest_hour=8)
    tz = cfg.timezone
    sched.tick(datetime(2026, 10, 31, 7, 59, tzinfo=tz))
    assert m.events == []
    sched.tick(datetime(2026, 10, 31, 8, 0, tzinfo=tz))
    assert len(m.events) == 2
    restarted = Scheduler(db, cfg, {"bale": m}, stop=None)  # type: ignore[arg-type]  # fresh in-memory state
    restarted.tick(datetime(2026, 10, 31, 15, 0, tzinfo=tz))
    assert len(m.events) == 2  # the ledger in the database remembers
    restarted.tick(datetime(2026, 11, 1, 8, 5, tzinfo=tz))
    assert len(m.events) == 4  # next day sends again


# --- reminders -------------------------------------------------------------------------
def test_reminders_skip_finished_meetings_and_retry_failures(tmp_path):
    db, _, m, sara, _, sched = _setup(tmp_path, digest_hour=23)
    lead = db.add_lead("رضایی", "", "", sara.id)
    now = utcnow()
    ids = {s: db.add_meeting(lead.id, now + timedelta(minutes=20 + i), sara.id) for i, s in enumerate(["scheduled", "cancelled", "done", "no_show", "rescheduled"])}
    with db.connect() as c:
        for status, meeting_id in ids.items():
            c.execute("UPDATE meetings SET status = ? WHERE id = ?", (status, meeting_id))
    sched.tick(now)
    assert len([e for e in m.events if "یادآوری" in e[2]]) == 1  # only the scheduled one
    assert db.claim_reminder(ids["done"]) is False  # not claimable either


def test_a_failed_reminder_releases_its_claim(tmp_path):
    db, _, m, sara, _, sched = _setup(tmp_path, digest_hour=23)
    lead = db.add_lead("رضایی", "", "", sara.id)
    now = utcnow()
    meeting_id = db.add_meeting(lead.id, now + timedelta(minutes=30), sara.id)
    m.fail = True
    sched.tick(now)
    assert db.due_reminders(now, timedelta(hours=1))[0].id == meeting_id  # claim was released
    m.fail = False
    sched.tick(now + timedelta(minutes=3))
    assert len(m.events) == 1 and db.due_reminders(now, timedelta(hours=1)) == []


# --- job isolation ---------------------------------------------------------------------
def test_one_failing_job_does_not_stop_the_others(tmp_path):
    db, cfg, m, *_, sched = _setup(tmp_path, digest_hour=8, backup_keep=3)

    def broken(now):
        raise RuntimeError("reminder job exploded")

    sched.send_reminders = broken  # type: ignore[method-assign]
    outcome = sched.tick(datetime(2026, 10, 31, 9, 0, tzinfo=cfg.timezone))
    assert outcome == {"reminders": False, "digest": True, "backup": True}
    assert len(m.events) == 2 and list(cfg.backup_dir.glob("salesdesk-*.db"))


def test_backoff_schedule():
    b, now = Backoff(max_attempts=3), datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert b.ready("k", now)
    assert not b.failed("k", now) and not b.ready("k", now + timedelta(seconds=30)) and b.ready("k", now + timedelta(minutes=1))
    assert not b.failed("k", now) and b.ready("k", now + timedelta(minutes=2))
    assert b.failed("k", now) and not b.ready("k", now + timedelta(days=1))  # third strike: gave up
    b.succeeded("k")
    assert b.ready("k", now)


# --- backups through the scheduler -----------------------------------------------------
def test_backup_failure_is_logged_retried_with_backoff_and_never_leaves_partial_files(tmp_path):
    db, cfg, m, *_, sched = _setup(tmp_path, digest_hour=23, backup_keep=3)
    real, calls = db.backup_to, []

    def flaky(target):
        calls.append(1)
        if len(calls) == 1:
            raise OSError("disk full")
        real(target)

    db.backup_to = flaky  # type: ignore[method-assign]
    now = datetime(2026, 11, 1, 12, 0, tzinfo=cfg.timezone)
    assert sched.tick(now)["backup"] is False
    assert sched.tick(now + timedelta(seconds=30))["backup"] is True and len(calls) == 1  # skipped: backoff
    assert sched.tick(now + timedelta(minutes=2))["backup"] is True and len(calls) == 2
    assert [p.name for p in cfg.backup_dir.iterdir()] == ["salesdesk-2026-11-01.db"]  # no .tmp leftovers


# --- time zones ------------------------------------------------------------------------
def test_tehran_has_a_fixed_offset_all_year(tmp_path):
    tz = make_settings(tmp_path).timezone
    for month in (1, 3, 7, 10, 12):  # Iran abolished daylight saving time in 2022
        assert datetime(2026, month, 15, 12, tzinfo=timezone.utc).astimezone(tz).utcoffset() == timedelta(hours=3, minutes=30)


def test_local_day_boundaries_and_jalali_stamp_at_midnight(tmp_path):
    tz = make_settings(tmp_path).timezone
    before = datetime(2026, 3, 20, 20, 29, tzinfo=timezone.utc)  # 23:59 Tehran, 20 Esfand 1404
    after = datetime(2026, 3, 20, 20, 30, tzinfo=timezone.utc)  # 00:00 Tehran, 1 Farvardin 1405
    assert format_stamp(before, tz).startswith("1404/12/29") and format_stamp(after, tz) == "1405/01/01 00:00"
    assert local_midnight(after, tz).astimezone(timezone.utc) == after
    assert local_midnight(before, tz).astimezone(timezone.utc) == datetime(2026, 3, 19, 20, 30, tzinfo=timezone.utc)


def test_digest_and_reminder_fire_at_the_right_utc_moment(tmp_path):
    db, cfg, m, sara, _, sched = _setup(tmp_path, digest_hour=8, reminder_minutes=60)
    lead = db.add_lead("رضایی", "نوین", "", sara.id)
    meeting_at = datetime(2026, 10, 31, 10, 0, tzinfo=cfg.timezone)  # 06:30 UTC
    db.add_meeting(lead.id, meeting_at, sara.id)
    sched.tick(datetime(2026, 10, 31, 4, 29, tzinfo=timezone.utc))  # 07:59 Tehran
    assert m.events == []
    sched.tick(datetime(2026, 10, 31, 4, 31, tzinfo=timezone.utc))  # 08:01 Tehran: digest, meeting still >60 min away
    assert len(_digests(m, "1")) == 1 and not [e for e in m.events if "یادآوری" in e[2]]
    sched.tick(datetime(2026, 10, 31, 5, 31, tzinfo=timezone.utc))  # 09:01 Tehran: 59 minutes before
    reminder = [e for e in m.events if "یادآوری" in e[2]][0][2]
    assert "۱۰:۰۰" in reminder and "۵۹" in reminder  # shown in local time, not UTC
