import sqlite3

from salesdesk import db as D
from salesdesk.db import MIGRATIONS, Database

LEGACY_02 = """
CREATE TABLE users (tg_id INTEGER PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'employee');
CREATE TABLE leads (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, company TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new', owner_id INTEGER, created_at TEXT NOT NULL, last_contact_at TEXT NOT NULL);
CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL, note TEXT NOT NULL, owner_id INTEGER, created_at TEXT NOT NULL);
CREATE TABLE meetings (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL, scheduled_at TEXT NOT NULL, owner_id INTEGER, status TEXT NOT NULL DEFAULT 'scheduled');
CREATE TABLE tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER NOT NULL, title TEXT NOT NULL, due_date TEXT NOT NULL, done_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, owner_id INTEGER, lead_id INTEGER, created_at TEXT NOT NULL);
CREATE INDEX idx_leads_status ON leads(status, last_contact_at);
CREATE INDEX idx_meetings_time ON meetings(status, scheduled_at);
CREATE INDEX idx_tasks_owner ON tasks(owner_id, due_date);
CREATE INDEX idx_events_time ON events(created_at, kind);
INSERT INTO users VALUES (111, 'سارا', 'employee'), (222, 'مدیر', 'ceo');
INSERT INTO leads VALUES (1, 'رضایی', 'نوین', '0912', 'meeting', 111, '2026-01-02T08:00:00+00:00', '2026-01-05T08:00:00+00:00'),
                         (2, 'بی‌صاحب', '', '', 'new', 999, '2026-01-03T08:00:00+00:00', '2026-01-03T08:00:00+00:00');
INSERT INTO calls VALUES (1, 1, 'تماس اول', 111, '2026-01-03T08:00:00+00:00');
INSERT INTO meetings VALUES (1, 1, '2026-02-01T09:00:00+00:00', 111, 'scheduled');
INSERT INTO tasks VALUES (1, 111, 'پیگیری', '2026-02-02', NULL, '2026-01-04T08:00:00+00:00');
INSERT INTO events VALUES (1, 'lead', 111, 1, '2026-01-02T08:00:00+00:00');
"""
LEGACY_01 = """
CREATE TABLE users (tg_id INTEGER PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE leads (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, company TEXT NOT NULL DEFAULT '', phone TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new', created_at TEXT NOT NULL, last_contact_at TEXT NOT NULL);
CREATE TABLE calls (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL, note TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE meetings (id INTEGER PRIMARY KEY AUTOINCREMENT, lead_id INTEGER NOT NULL, scheduled_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'scheduled');
INSERT INTO users VALUES (111, 'سارا');
INSERT INTO leads VALUES (1, 'قدیمی', '', '', 'contacted', '2026-01-02T08:00:00+00:00', '2026-01-05T08:00:00+00:00');
INSERT INTO calls VALUES (1, 1, 'تماس', '2026-01-03T08:00:00+00:00');
INSERT INTO meetings VALUES (1, 1, '2026-02-01T09:00:00+00:00', 'scheduled');
"""


def _make(path, script):
    c = sqlite3.connect(path)
    c.executescript(script)
    c.commit()
    c.close()


def _tables(db):
    with db.connect() as c:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_fresh_database_is_at_the_latest_version_and_reopening_is_a_noop(tmp_path):
    path = tmp_path / "fresh.db"
    db = Database(path)
    assert {"users", "leads", "calls", "meetings", "tasks", "events", "notes", "stage_changes", "deals", "notification_log"} <= _tables(db)
    Database(path)
    assert not list(tmp_path.glob("*.bak"))  # no snapshot for a brand-new database
    with db.connect() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_v1_database_keeps_every_record_and_gets_sane_defaults(tmp_path):
    path = tmp_path / "v1.db"
    _make(path, MIGRATIONS[0] + """PRAGMA user_version = 1;
        INSERT INTO users(platform, ext_id, chat_id, name, role, active, created_at) VALUES('bale','1','1','سارا','employee',1,'2026-01-01T00:00:00+00:00');
        INSERT INTO leads(name, company, phone, status, owner_id, created_at, last_contact_at) VALUES('رضایی','نوین','0912','proposal',1,'2026-01-02T00:00:00+00:00','2026-01-03T00:00:00+00:00');
        INSERT INTO calls(lead_id, owner_id, note, created_at) VALUES(1,1,'تماس قدیمی','2026-01-03T00:00:00+00:00');
        INSERT INTO meetings(lead_id, owner_id, scheduled_at, status, created_at) VALUES(1,1,'2026-02-01T09:00:00+00:00','cancelled','2026-01-04T00:00:00+00:00');
        INSERT INTO tasks(owner_id, title, due_date, done_at, created_at) VALUES(1,'کار قدیمی','2026-02-02',NULL,'2026-01-04T00:00:00+00:00');
        INSERT INTO events(kind, owner_id, lead_id, created_at) VALUES('call',1,1,'2026-01-03T00:00:00+00:00');""")
    db = Database(path)
    lead = db.get_lead(1)
    assert (lead.name, lead.company, lead.status, lead.source, lead.next_followup) == ("رضایی", "نوین", "proposal", "", None)
    with db.connect() as c:
        call = c.execute("SELECT note, outcome, duration_sec FROM calls").fetchone()
        assert tuple(call) == ("تماس قدیمی", "answered", None)  # old calls count as answered contacts
        assert c.execute("SELECT status, notes FROM meetings").fetchone()["status"] == "cancelled"
        assert c.execute("SELECT priority, deleted_at FROM tasks").fetchone()["priority"] == "medium"
        assert c.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
        base = c.execute("SELECT from_stage, to_stage FROM stage_changes").fetchall()
        assert [(r["from_stage"], r["to_stage"]) for r in base] == [(None, "proposal")]  # baseline for analytics
    assert (tmp_path / "v1.db.pre-migration-v1.bak").exists()
    assert db.open_tasks(1, __import__("datetime").date(2026, 12, 1))[0].title == "کار قدیمی"


def test_a_failing_migration_rolls_back_completely(tmp_path):
    path = tmp_path / "t.db"
    Database(path).upsert_user("bale", "1", "1", "سارا", "employee", active=True)
    D.MIGRATIONS.append("CREATE TABLE half_done(x INTEGER); INSERT INTO half_done VALUES (1); THIS IS NOT SQL;")
    try:
        try:
            Database(path)
        except sqlite3.Error:
            pass
        else:
            raise AssertionError("a broken migration must raise")
    finally:
        D.MIGRATIONS.pop()
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)  # not advanced
    assert conn.execute("SELECT name FROM sqlite_master WHERE name = 'half_done'").fetchone() is None  # no partial table
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1  # data intact
    conn.close()
    Database(path)  # and the next start-up works normally


def test_legacy_0_2_database_is_converted_with_owners_remapped(tmp_path):
    path = tmp_path / "legacy.db"
    _make(path, LEGACY_02)
    db = Database(path)
    by_ext = {u.ext_id: u for u in db.users()}
    assert set(by_ext) == {"111", "222"} and by_ext["111"].platform == "telegram" and by_ext["222"].is_ceo
    lead = db.get_lead(1)
    assert (lead.name, lead.status, lead.owner_id) == ("رضایی", "meeting", by_ext["111"].id)  # Telegram id -> internal id
    assert db.get_lead(2).owner_id is None  # unknown owner is kept, not dropped
    assert db.lead_meetings(1, now=__import__("datetime").datetime(2026, 1, 1, tzinfo=__import__("datetime").timezone.utc))[0].owner_id == by_ext["111"].id
    with db.connect() as c:
        assert c.execute("SELECT owner_id, note FROM calls").fetchone()["owner_id"] == by_ext["111"].id
        assert c.execute("SELECT owner_id FROM tasks").fetchone()["owner_id"] == by_ext["111"].id
        assert c.execute("SELECT owner_id FROM events").fetchone()["owner_id"] == by_ext["111"].id
        assert c.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert not any(t.endswith("_old") for t in _tables(db)) and (tmp_path / "legacy.db.pre-migration-v0.bak").exists()


def test_legacy_0_1_database_without_owner_columns_is_converted(tmp_path):
    path = tmp_path / "legacy1.db"
    _make(path, LEGACY_01)
    db = Database(path)
    assert [u.ext_id for u in db.users()] == ["111"]
    lead = db.get_lead(1)
    assert (lead.name, lead.status, lead.owner_id) == ("قدیمی", "contacted", None)
    with db.connect() as c:
        assert c.execute("SELECT note FROM calls").fetchone()["note"] == "تماس"
        assert c.execute("SELECT status FROM meetings").fetchone()["status"] == "scheduled"
