from datetime import date, timedelta

from harness import make_settings

from salesdesk.analytics import build_summary
from salesdesk.db import Database
from salesdesk.domain import iso, utcnow
from salesdesk.fa import normalize_phone, normalize_text, parse_jalali, to_gregorian, to_jalali


def _user(db, ext="1", name="سارا", role="employee"):
    return db.upsert_user("bale", ext, ext, name, role, active=True)


def test_migrations_are_versioned_and_idempotent(tmp_path):
    path = tmp_path / "m.db"
    Database(path)
    db = Database(path)  # reopening must not re-run migrations
    with db.connect() as c:
        from salesdesk.db import MIGRATIONS
        assert c.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_soft_delete_hides_lead_cancels_meetings_and_drops_it_from_metrics(tmp_path):
    db, cfg = Database(tmp_path / "t.db"), make_settings(tmp_path)
    user = _user(db)
    lead = db.add_lead("رضایی", "", "", user.id)
    meeting_id = db.add_meeting(lead.id, utcnow() + timedelta(hours=2), user.id)
    before = build_summary(db, cfg, range_key="7d")["performance"]["totals"]["leads"]["value"]
    db.delete_lead(lead.id, user.id)
    assert db.get_lead(lead.id) is None
    assert db.get_meeting(meeting_id) is None  # hidden together with its lead
    assert before == 1 and build_summary(db, cfg, range_key="7d")["performance"]["totals"]["leads"]["value"] == 0


def test_search_escapes_wildcards_and_filters(tmp_path):
    db = Database(tmp_path / "t.db")
    user = _user(db)
    db.add_lead("100% تخفیف", "", "", user.id)
    db.add_lead("احمدی", "پارس", "09351112233", user.id)
    assert db.search_leads(query="%")[1] == 1  # a literal percent, not a wildcard
    assert db.search_leads(query="_")[1] == 0
    assert db.search_leads(query="پارس")[1] == 1 and db.search_leads(query="0935")[1] == 1
    assert db.search_leads(owner_id=user.id + 99)[1] == 0
    page, total = db.search_leads(limit=1, offset=1)
    assert total == 2 and len(page) == 1


def test_stale_ignores_closed_deleted_and_recent(tmp_path):
    db = Database(tmp_path / "t.db")
    user = _user(db)
    old, won, gone, fresh = (db.add_lead(n, "", "", user.id) for n in "abcd")
    with db.connect() as c:
        c.execute("UPDATE leads SET last_contact_at = ? WHERE id != ?", (iso(utcnow() - timedelta(days=20)), fresh.id))
    db.set_stage(won.id, "won", user.id)
    with db.connect() as c:  # set_stage touches last_contact_at, so age it again
        c.execute("UPDATE leads SET last_contact_at = ? WHERE id = ?", (iso(utcnow() - timedelta(days=20)), won.id))
    db.delete_lead(gone.id, user.id)
    assert [lead.id for lead in db.stale_leads(10)] == [old.id]


def test_reminder_window_and_reschedule_rearms(tmp_path):
    db = Database(tmp_path / "t.db")
    user = _user(db)
    lead = db.add_lead("x", "", "", user.id)
    now = utcnow()
    soon = db.add_meeting(lead.id, now + timedelta(minutes=30), user.id)
    db.add_meeting(lead.id, now + timedelta(hours=5), user.id)
    assert [m.id for m in db.due_reminders(now, timedelta(minutes=60))] == [soon]
    assert db.claim_reminder(soon) is True and db.claim_reminder(soon) is False  # atomic: second claim loses
    assert db.due_reminders(now, timedelta(minutes=60)) == []
    db.reschedule_meeting(soon, now + timedelta(minutes=45), user.id)
    assert [m.id for m in db.due_reminders(now, timedelta(minutes=60))] == [soon]


def test_tasks_overdue_and_ownership(tmp_path):
    db = Database(tmp_path / "t.db")
    a, b = _user(db, "1"), _user(db, "2", "علی")
    today = date(2026, 10, 29)
    late = db.add_task(a.id, "قدیمی", today - timedelta(days=2))
    db.add_task(a.id, "امروز", today)
    db.add_task(a.id, "آینده", today + timedelta(days=3))
    assert [t.title for t in db.open_tasks(a.id, today)] == ["قدیمی", "امروز"]
    assert db.task_load(today)[a.id] == (3, 1)
    assert db.complete_task(late.id, b.id) is False and db.complete_task(late.id, a.id) is True
    assert db.complete_task(late.id, a.id) is False


def test_session_and_kv_roundtrip(tmp_path):
    db = Database(tmp_path / "t.db")
    assert db.get_session("bale", "1") == ("", {})
    db.save_session("bale", "1", "new:phone", {"name": "رضایی"})
    assert db.get_session("bale", "1") == ("new:phone", {"name": "رضایی"})
    db.kv_set("k", "v")
    assert db.kv_get("k") == "v" and db.kv_get("missing") is None


def test_performance_compares_with_previous_period(tmp_path):
    db, cfg = Database(tmp_path / "t.db"), make_settings(tmp_path)
    emp, ceo = _user(db), _user(db, "9", "مدیر", "ceo")
    lead = db.add_lead("x", "", "", emp.id)
    db.add_call(lead.id, "n", emp.id)
    db.set_stage(lead.id, "won", emp.id)
    with db.connect() as c:
        c.execute("INSERT INTO events(kind, owner_id, created_at) VALUES('call', ?, ?)", (emp.id, iso(utcnow() - timedelta(days=9))))
    perf = build_summary(db, cfg, range_key="7d")["performance"]
    assert perf["totals"]["calls"] == {"value": 1, "prev": 1} and perf["totals"]["won"]["value"] == 1
    assert [t["name"] for t in perf["team"]] == ["سارا"] and ceo.is_ceo  # managers are not ranked
    assert len(perf["series"]) == 7


def test_persian_helpers():
    assert to_jalali(2026, 10, 29) == (1405, 8, 7) and to_gregorian(1405, 1, 1) == (2026, 3, 21)
    assert parse_jalali("۱۴۰۵/۰۸/۱۰") == date(2026, 11, 1)
    assert parse_jalali("1405/13/01") is None and parse_jalali("hello") is None
    assert normalize_phone("۰۹۱۲ ۱۲۳-۴۵۶۷") == "09121234567" and normalize_phone("abc") == ""
    assert normalize_text("علي كريمي ۱۲") == "علی کریمی 12"
