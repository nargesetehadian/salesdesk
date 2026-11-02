import os
import sqlite3
import threading

from harness import env

from salesdesk import admin
from salesdesk.db import MIGRATIONS, Database, RestoreError, restore_database, verify_backup


def _db(tmp_path, name="live.db"):
    db = Database(tmp_path / name)
    return db, db.upsert_user("bale", "1", "1", "سارا", "employee", active=True)


def _names(db):
    return sorted(lead.name for lead in db.search_leads(limit=500)[0])


def _raises(fn, *args):
    try:
        fn(*args)
    except RestoreError as exc:
        return str(exc)
    raise AssertionError("expected RestoreError")


def test_snapshot_is_one_self_contained_valid_file(tmp_path):
    db, user = _db(tmp_path)
    db.add_lead("رضایی", "", "", user.id)
    target = tmp_path / "b" / "snap.db"
    db.backup_to(target)
    assert verify_backup(target) == len(MIGRATIONS)
    assert sorted(p.name for p in target.parent.iterdir()) == ["snap.db"]  # no -wal / -shm / .tmp leftovers
    assert Database(target).search_leads()[1] == 1


def test_snapshot_taken_while_the_bot_is_writing_is_consistent(tmp_path):
    db, user = _db(tmp_path)
    stop = threading.Event()

    def writer():
        i = 0
        while not stop.is_set():
            db.add_lead(f"مشتری {i}", "", "", user.id)
            i += 1

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        for n in range(3):
            db.backup_to(tmp_path / f"snap{n}.db")
    finally:
        stop.set()
        thread.join()
    for n in range(3):
        verify_backup(tmp_path / f"snap{n}.db")  # integrity_check passes on every snapshot


def test_restore_roundtrip_keeps_a_safety_copy_and_clears_stale_wal(tmp_path):
    db, user = _db(tmp_path)
    db.add_lead("قبل از پشتیبان", "", "", user.id)
    snap = tmp_path / "snap.db"
    db.backup_to(snap)
    db.add_lead("بعد از پشتیبان", "", "", user.id)  # data created after the snapshot
    saved = restore_database(snap, db.path)
    restored = Database(db.path)
    assert _names(restored) == ["قبل از پشتیبان"]
    assert saved is not None and _names(Database(saved)) == ["بعد از پشتیبان", "قبل از پشتیبان"]  # nothing is lost for good


def test_restore_refuses_bad_input_and_leaves_the_database_untouched(tmp_path):
    db, user = _db(tmp_path)
    db.add_lead("مهم", "", "", user.id)
    good = tmp_path / "good.db"
    db.backup_to(good)
    garbage = tmp_path / "garbage.db"
    garbage.write_bytes(b"this is not a database" * 100)
    truncated = tmp_path / "truncated.db"
    truncated.write_bytes(good.read_bytes()[:2000])
    foreign = tmp_path / "foreign.db"
    other = sqlite3.connect(foreign)
    other.execute("CREATE TABLE unrelated(x)")
    other.commit()
    other.close()
    newer = tmp_path / "newer.db"
    db.backup_to(newer)
    sq = sqlite3.connect(newer)
    sq.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 5}")
    sq.commit()
    sq.close()
    for bad, hint in ((garbage, "valid"), (truncated, ""), (foreign, "not a SalesDesk"), (newer, "newer"), (tmp_path / "missing.db", "not found")):
        message = _raises(restore_database, bad, db.path)
        assert hint in message, (bad.name, message)
    assert _names(Database(db.path)) == ["مهم"]  # every refusal happened before any change


def test_restoring_an_old_backup_migrates_it_and_pre_migration_snapshots_are_a_rollback_path(tmp_path):
    old = tmp_path / "v1.db"
    c = sqlite3.connect(old)
    c.executescript(MIGRATIONS[0] + "PRAGMA user_version = 1;")
    c.execute("INSERT INTO users(platform, ext_id, chat_id, name, created_at) VALUES('bale','1','1','سارا','2026-01-01T00:00:00+00:00')")
    c.execute("INSERT INTO leads(name, owner_id, created_at, last_contact_at) VALUES('قدیمی',1,'2026-01-02T00:00:00+00:00','2026-01-02T00:00:00+00:00')")
    c.commit()
    c.close()
    live = tmp_path / "live.db"
    restore_database(old, live)
    db = Database(live)
    assert _names(db) == ["قدیمی"]
    with db.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)  # migrated on restore
    # Upgrading an existing v1 database left a snapshot; it can be restored to roll the upgrade back.
    legacy = tmp_path / "rollback.db"
    c = sqlite3.connect(legacy)
    c.executescript(MIGRATIONS[0] + "PRAGMA user_version = 1;")
    c.commit()
    c.close()
    Database(legacy)
    snapshot = tmp_path / "rollback.db.pre-migration-v1.bak"
    assert snapshot.exists() and verify_backup(snapshot) == 1


def test_admin_cli_backup_verify_restore(tmp_path, capsys=None):
    os.chdir(tmp_path)
    with env():
        db = Database(tmp_path / "data" / "salesdesk.db")
        user = db.upsert_user("bale", "1", "1", "سارا", "employee", active=True)
        db.add_lead("اصلی", "", "", user.id)
        snap = tmp_path / "manual.db"
        assert admin.main(["backup", "--to", str(snap)]) == 0 and snap.exists()
        assert admin.main(["verify", str(snap)]) == 0
        db.add_lead("اضافه", "", "", user.id)
        assert admin.main(["restore", str(snap)]) == 2  # no --yes: refuses
        assert _names(Database(db.path)) == ["اصلی", "اضافه"]
        assert admin.main(["restore", str(snap), "--yes"]) == 0
        assert _names(Database(db.path)) == ["اصلی"]
        assert admin.main(["restore", str(tmp_path / "nope.db"), "--yes"]) == 1  # clean error, no traceback
        assert admin.main(["verify", str(tmp_path / "nope.db")]) == 1
