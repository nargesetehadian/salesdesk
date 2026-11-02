"""SQLite persistence: versioned migrations and every query the application needs.

Timestamps are UTC ISO-8601 strings; leads are soft-deleted so history stays consistent.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .domain import (
    CONTACT_OUTCOMES, EFFECTIVE_OUTCOMES, PRIORITIES, SOURCES, STAGES, Contact, Deal, Lead, Meeting, Note, Task, TimelineItem, User, iso, parse_dt, utcnow,
)  # fmt: skip

log = logging.getLogger(__name__)

MIGRATIONS = ["""
CREATE TABLE users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    platform   TEXT NOT NULL,
    ext_id     TEXT NOT NULL,
    chat_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'employee',
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    UNIQUE (platform, ext_id)
);
CREATE TABLE leads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    company         TEXT NOT NULL DEFAULT '',
    phone           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'new',
    owner_id        INTEGER REFERENCES users(id),
    created_at      TEXT NOT NULL,
    last_contact_at TEXT NOT NULL,
    deleted_at      TEXT
);
CREATE TABLE calls (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL REFERENCES leads(id),
    owner_id   INTEGER REFERENCES users(id),
    note       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE meetings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id          INTEGER NOT NULL REFERENCES leads(id),
    owner_id         INTEGER REFERENCES users(id),
    scheduled_at     TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'scheduled',
    reminder_sent_at TEXT,
    created_at       TEXT NOT NULL
);
CREATE TABLE tasks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id   INTEGER NOT NULL REFERENCES users(id),
    title      TEXT NOT NULL,
    due_date   TEXT NOT NULL,
    done_at    TEXT,
    created_at TEXT NOT NULL
);
-- Append-only activity log: every dashboard metric is derived from it.
CREATE TABLE events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    owner_id   INTEGER,
    lead_id    INTEGER,
    created_at TEXT NOT NULL
);
CREATE TABLE sessions (
    platform   TEXT NOT NULL,
    chat_id    TEXT NOT NULL,
    state      TEXT NOT NULL DEFAULT '',
    data       TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (platform, chat_id)
);
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE INDEX idx_leads_active  ON leads(deleted_at, status, last_contact_at);
CREATE INDEX idx_meetings_time ON meetings(status, scheduled_at);
CREATE INDEX idx_tasks_owner   ON tasks(owner_id, due_date);
CREATE INDEX idx_events_time   ON events(created_at, kind);
"""]

MIGRATIONS.append("""
-- v2: CRM depth. Existing tables are extended in place; nothing is dropped or rewritten.
ALTER TABLE leads ADD COLUMN source TEXT NOT NULL DEFAULT '';
ALTER TABLE leads ADD COLUMN next_followup TEXT;
ALTER TABLE calls ADD COLUMN outcome TEXT NOT NULL DEFAULT 'answered';
ALTER TABLE calls ADD COLUMN duration_sec INTEGER;
ALTER TABLE calls ADD COLUMN next_followup TEXT;
ALTER TABLE meetings ADD COLUMN notes TEXT NOT NULL DEFAULT '';
ALTER TABLE meetings ADD COLUMN status_at TEXT;
ALTER TABLE meetings ADD COLUMN rescheduled_from INTEGER REFERENCES meetings(id);
ALTER TABLE meetings ADD COLUMN next_followup TEXT;
ALTER TABLE tasks ADD COLUMN priority TEXT NOT NULL DEFAULT 'medium';
ALTER TABLE tasks ADD COLUMN lead_id INTEGER REFERENCES leads(id);
ALTER TABLE tasks ADD COLUMN deleted_at TEXT;
CREATE TABLE notes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL REFERENCES leads(id),
    author_id  INTEGER REFERENCES users(id),
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    deleted_at TEXT
);
CREATE TABLE stage_changes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id    INTEGER NOT NULL REFERENCES leads(id),
    from_stage TEXT,
    to_stage   TEXT NOT NULL,
    user_id    INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE deals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         INTEGER NOT NULL REFERENCES leads(id),
    owner_id        INTEGER REFERENCES users(id),
    title           TEXT NOT NULL,
    proposed_amount INTEGER NOT NULL DEFAULT 0,
    final_amount    INTEGER,
    status          TEXT NOT NULL DEFAULT 'open',
    notes           TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    closed_at       TEXT
);
-- Delivery ledger: one row per notification that was successfully delivered (dedupe + retry).
CREATE TABLE notification_log (
    key        TEXT PRIMARY KEY,
    user_id    INTEGER,
    created_at TEXT NOT NULL
);
-- Baseline history so pre-existing customers have a starting point for stage analytics.
INSERT INTO stage_changes(lead_id, from_stage, to_stage, user_id, created_at)
    SELECT id, NULL, status, owner_id, created_at FROM leads;
CREATE INDEX idx_stage_changes ON stage_changes(lead_id, created_at);
CREATE INDEX idx_calls_lead    ON calls(lead_id, created_at);
CREATE INDEX idx_meetings_lead ON meetings(lead_id);
CREATE INDEX idx_deals_status  ON deals(status, closed_at);
CREATE INDEX idx_notes_lead    ON notes(lead_id);
""")

TASK_SELECT = (
    "SELECT t.*, l.name AS lead_name FROM tasks t "
    "LEFT JOIN leads l ON l.id = t.lead_id AND l.deleted_at IS NULL WHERE t.deleted_at IS NULL"
)
LEAD_SELECT = "SELECT l.*, u.name AS owner_name FROM leads l LEFT JOIN users u ON u.id = l.owner_id"
MEETING_SELECT = (
    "SELECT m.*, l.name AS lead_name, l.company FROM meetings m "
    "JOIN leads l ON l.id = m.lead_id AND l.deleted_at IS NULL"
)
EDITABLE_LEAD_FIELDS = ("name", "company", "phone", "source")


def _user(r: sqlite3.Row) -> User:
    return User(r["id"], r["platform"], r["ext_id"], r["chat_id"], r["name"], r["role"], bool(r["active"]))


def _day(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def _lead(r: sqlite3.Row) -> Lead:
    return Lead(
        r["id"], r["name"], r["company"], r["phone"], r["status"], r["owner_id"], r["owner_name"] or "",
        parse_dt(r["created_at"]), parse_dt(r["last_contact_at"]), r["source"], _day(r["next_followup"]),
    )  # fmt: skip


def _meeting(r: sqlite3.Row) -> Meeting:
    return Meeting(
        r["id"], r["lead_id"], r["lead_name"], r["company"], r["owner_id"], parse_dt(r["scheduled_at"]), r["status"],
        r["notes"], parse_dt(r["status_at"]) if r["status_at"] else None, r["rescheduled_from"], _day(r["next_followup"]),
    )  # fmt: skip


def _task(r: sqlite3.Row) -> Task:
    return Task(
        r["id"], r["owner_id"], r["title"], date.fromisoformat(r["due_date"]), r["done_at"] is not None,
        r["priority"], r["lead_id"], r["lead_name"] or "",
    )  # fmt: skip


def _note(r: sqlite3.Row) -> Note:
    return Note(
        r["id"], r["lead_id"], r["author_id"], r["author_name"] or "", r["body"], parse_dt(r["created_at"]),
        parse_dt(r["updated_at"]) if r["updated_at"] else None,
    )  # fmt: skip


def _deal(r: sqlite3.Row) -> Deal:
    return Deal(
        r["id"], r["lead_id"], r["lead_name"] or "", r["owner_id"], r["title"], r["proposed_amount"], r["final_amount"],
        r["status"], r["notes"], parse_dt(r["created_at"]), parse_dt(r["closed_at"]) if r["closed_at"] else None,
    )  # fmt: skip


class RestoreError(RuntimeError):
    pass


def verify_backup(path: Path) -> int:
    """Validate a snapshot and return its schema version; raises RestoreError if unusable."""
    if not path.is_file():
        raise RestoreError(f"backup file not found: {path}")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RestoreError("backup failed the integrity check; it is corrupt")
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        raise RestoreError(f"not a valid SalesDesk database: {exc}") from None
    if "leads" not in tables or "users" not in tables:
        raise RestoreError("not a SalesDesk database (customer tables are missing)")
    if version > len(MIGRATIONS):
        raise RestoreError(f"backup schema v{version} is newer than this program (v{len(MIGRATIONS)})")
    return version


def restore_database(source: Path, target: Path) -> Path | None:
    """Replace `target` with the snapshot `source` after validating it. Stop the bot first.

    The current database is saved next to it as `*.before-restore-<time>`; that path is returned.
    """
    verify_backup(source)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    saved: Path | None = None
    try:
        if target.exists():
            saved = target.with_name(f"{target.name}.before-restore-{utcnow():%Y%m%d%H%M%S}")
            current, keep = sqlite3.connect(target), sqlite3.connect(saved)
            try:
                current.backup(keep)
                keep.execute("PRAGMA journal_mode = DELETE")
            finally:
                keep.close()
                current.close()
        for suffix in ("-wal", "-shm"):
            Path(f"{target}{suffix}").unlink(missing_ok=True)
        dest = sqlite3.connect(target)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    Database(target)  # runs pending migrations, so restoring an older backup is immediately usable
    return saved


def _like(term: str) -> str:
    return "%" + re.sub(r"([%_\\])", r"\\\1", term) + "%"


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            legacy = version == 0 and self._is_legacy(conn)
            if (version > 0 or legacy) and version < len(MIGRATIONS):
                snapshot = self.path.with_name(f"{self.path.name}.pre-migration-v{version}.bak")
                dest = sqlite3.connect(snapshot)
                try:
                    conn.backup(dest)  # a safety copy before any schema change
                finally:
                    dest.close()
                log.info("Migrating database v%d -> v%d (snapshot: %s)", version, len(MIGRATIONS), snapshot.name)
            if legacy:
                self._upgrade_legacy(conn)
                version = 1
            for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
                # One transaction per migration: a failure rolls back and leaves user_version untouched.
                conn.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {number};\nCOMMIT;")

    @staticmethod
    def _is_legacy(conn: sqlite3.Connection) -> bool:
        return "tg_id" in {r["name"] for r in conn.execute("PRAGMA table_info(users)")}

    @staticmethod
    def _upgrade_legacy(conn: sqlite3.Connection) -> None:
        """Convert a 0.x database (Telegram ids as owners, python-telegram-bot era) to the v1 layout."""

        def cols(table: str) -> set[str]:
            return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

        present = [t for t in ("users", "leads", "calls", "meetings", "tasks", "events") if cols(t)]
        now = iso(utcnow())
        mapped = "(SELECT u.id FROM users u WHERE u.ext_id = CAST(x.owner_id AS TEXT))"
        old_cols = {t: cols(t) for t in present}
        owner = lambda t: mapped if "owner_id" in old_cols.get(t, ()) else "NULL"  # noqa: E731  (table may not exist in 0.1)
        role = "role" if "role" in old_cols["users"] else "'employee'"
        script = ["BEGIN;"] + [f"ALTER TABLE {t} RENAME TO {t}_old;" for t in present]
        script += [f"DROP INDEX IF EXISTS {i};" for i in ("idx_leads_status", "idx_meetings_time", "idx_tasks_owner", "idx_events_time")]
        script.append(MIGRATIONS[0])
        script.append(
            "INSERT INTO users(platform, ext_id, chat_id, name, role, active, created_at) SELECT 'telegram', "
            f"CAST(tg_id AS TEXT), CAST(tg_id AS TEXT), name, {role}, 1, '{now}' FROM users_old;"
        )
        copies = {
            "leads": ("id, name, company, phone, status, owner_id, created_at, last_contact_at",
                      f"x.id, x.name, x.company, x.phone, x.status, {owner('leads')}, x.created_at, x.last_contact_at", ""),
            "calls": ("id, lead_id, owner_id, note, created_at", f"x.id, x.lead_id, {owner('calls')}, x.note, x.created_at", ""),
            "meetings": ("id, lead_id, owner_id, scheduled_at, status, created_at",
                         f"x.id, x.lead_id, {owner('meetings')}, x.scheduled_at, x.status, x.scheduled_at", ""),
            "tasks": ("id, owner_id, title, due_date, done_at, created_at",
                      f"x.id, {mapped}, x.title, x.due_date, x.done_at, x.created_at", f" WHERE {mapped} IS NOT NULL"),
            "events": ("kind, owner_id, lead_id, created_at", f"x.kind, {owner('events')}, x.lead_id, x.created_at", ""),
        }
        for table, (target_cols, select, where) in copies.items():
            if table in present:
                script.append(f"INSERT INTO {table}({target_cols}) SELECT {select} FROM {table}_old x{where};")
        script += [f"DROP TABLE {t}_old;" for t in present] + ["PRAGMA user_version = 1;", "COMMIT;"]
        conn.executescript("\n".join(script))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        """One short-lived connection per operation: safe across poller, scheduler and web threads."""
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _log(conn: sqlite3.Connection, kind: str, actor: int | None, lead_id: int | None = None) -> None:
        conn.execute(
            "INSERT INTO events(kind, owner_id, lead_id, created_at) VALUES(?,?,?,?)",
            (kind, actor, lead_id, iso(utcnow())),
        )

    @staticmethod
    def _change_stage(conn: sqlite3.Connection, lead_id: int, to_stage: str, user_id: int | None, *, only_from: tuple[str, ...] | None = None) -> bool:
        """Move a lead to `to_stage` and record previous stage, new stage, user and time. No-op when unchanged."""
        row = conn.execute("SELECT status FROM leads WHERE id = ?", (lead_id,)).fetchone()
        if row is None or row["status"] == to_stage or (only_from is not None and row["status"] not in only_from):
            return False
        conn.execute("UPDATE leads SET status = ? WHERE id = ?", (to_stage, lead_id))
        conn.execute(
            "INSERT INTO stage_changes(lead_id, from_stage, to_stage, user_id, created_at) VALUES(?,?,?,?,?)",
            (lead_id, row["status"], to_stage, user_id, iso(utcnow())),
        )
        Database._log(conn, f"stage_{to_stage}", user_id, lead_id)
        return True

    def health(self) -> bool:
        with self.connect() as c:
            return c.execute("SELECT 1").fetchone()[0] == 1

    # users -----------------------------------------------------------------
    def upsert_user(self, platform: str, ext_id: str, chat_id: str, name: str, role: str, *, active: bool) -> User:
        with self.connect() as c:
            c.execute(
                "INSERT INTO users(platform, ext_id, chat_id, name, role, active, created_at) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(platform, ext_id) DO UPDATE SET "
                "chat_id = excluded.chat_id, name = excluded.name, role = excluded.role",
                (platform, ext_id, chat_id, name, role, int(active), iso(utcnow())),
            )
            row = c.execute("SELECT * FROM users WHERE platform = ? AND ext_id = ?", (platform, ext_id)).fetchone()
        return _user(row)

    def set_active(self, user_id: int, active: bool) -> None:
        with self.connect() as c:
            c.execute("UPDATE users SET active = ? WHERE id = ?", (int(active), user_id))

    def get_user(self, user_id: int) -> User | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _user(row) if row else None

    def users(self, *, active_only: bool = False) -> list[User]:
        with self.connect() as c:
            rows = c.execute(f"SELECT * FROM users {'WHERE active = 1' if active_only else ''} ORDER BY name")
            return [_user(r) for r in rows]

    # leads -----------------------------------------------------------------
    def add_lead(self, name: str, company: str, phone: str, owner_id: int | None, source: str = "") -> Lead:
        if source and source not in SOURCES:
            raise ValueError(f"unknown source: {source}")
        now = iso(utcnow())
        with self.connect() as c:
            lead_id = c.execute(
                "INSERT INTO leads(name, company, phone, owner_id, source, created_at, last_contact_at) VALUES(?,?,?,?,?,?,?)",
                (name, company, phone, owner_id, source, now, now),
            ).lastrowid
            c.execute("INSERT INTO stage_changes(lead_id, from_stage, to_stage, user_id, created_at) VALUES(?, NULL, 'new', ?, ?)", (lead_id, owner_id, now))
            self._log(c, "lead", owner_id, lead_id)
        return self.get_lead(lead_id)  # type: ignore[return-value]

    def get_lead(self, lead_id: int) -> Lead | None:
        with self.connect() as c:
            row = c.execute(f"{LEAD_SELECT} WHERE l.id = ? AND l.deleted_at IS NULL", (lead_id,)).fetchone()
        return _lead(row) if row else None

    def update_lead(self, lead_id: int, **fields: str) -> None:
        if not fields or not set(fields) <= set(EDITABLE_LEAD_FIELDS):
            raise ValueError(f"cannot edit fields: {sorted(fields)}")
        if fields.get("source") and fields["source"] not in SOURCES:
            raise ValueError(f"unknown source: {fields['source']}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as c:
            c.execute(f"UPDATE leads SET {sets} WHERE id = ? AND deleted_at IS NULL", (*fields.values(), lead_id))  # noqa: S608

    def set_followup(self, lead_id: int, day: date | None) -> None:
        with self.connect() as c:
            c.execute("UPDATE leads SET next_followup = ? WHERE id = ?", (day.isoformat() if day else None, lead_id))

    def delete_lead(self, lead_id: int, actor_id: int | None) -> None:
        """Soft delete: hides the lead everywhere and cancels its upcoming meetings."""
        with self.connect() as c:
            c.execute("UPDATE leads SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL", (iso(utcnow()), lead_id))
            c.execute("UPDATE meetings SET status = 'cancelled', status_at = ? WHERE lead_id = ? AND status = 'scheduled'", (iso(utcnow()), lead_id))
            self._log(c, "lead_deleted", actor_id, lead_id)

    def search_leads(
        self, *, query: str = "", status: str | None = None, owner_id: int | None = None,
        open_only: bool = False, limit: int = 6, offset: int = 0,
    ) -> tuple[list[Lead], int]:  # fmt: skip
        """Filter by stage / owner / free text (name, company or phone); returns (page, total)."""
        where, args = ["l.deleted_at IS NULL"], {}  # type: list[str], dict[str, Any]
        if status:
            where.append("l.status = :status")
            args["status"] = status
        if open_only:
            where.append("l.status NOT IN ('won', 'lost')")
        if owner_id is not None:
            where.append("l.owner_id = :owner")
            args["owner"] = owner_id
        if query:
            where.append("(l.name LIKE :q ESCAPE '\\' OR l.company LIKE :q ESCAPE '\\' OR l.phone LIKE :q ESCAPE '\\')")
            args["q"] = _like(query)
        clause = " AND ".join(where)
        with self.connect() as c:
            total = c.execute(f"SELECT COUNT(*) FROM leads l WHERE {clause}", args).fetchone()[0]  # noqa: S608
            rows = c.execute(
                f"{LEAD_SELECT} WHERE {clause} ORDER BY l.last_contact_at DESC, l.id DESC "  # noqa: S608
                "LIMIT :limit OFFSET :offset",
                {**args, "limit": limit, "offset": offset},
            ).fetchall()
        return [_lead(r) for r in rows], total

    def stale_leads(self, days: int, now: datetime | None = None) -> list[Lead]:
        cutoff = iso((now or utcnow()) - timedelta(days=days))
        with self.connect() as c:
            rows = c.execute(
                f"{LEAD_SELECT} WHERE l.deleted_at IS NULL AND l.status NOT IN ('won','lost') "  # noqa: S608
                "AND l.last_contact_at < ? ORDER BY l.last_contact_at",
                (cutoff,),
            ).fetchall()
        return [_lead(r) for r in rows]

    def status_counts(self) -> dict[str, int]:
        with self.connect() as c:
            rows = c.execute("SELECT status, COUNT(*) AS n FROM leads WHERE deleted_at IS NULL GROUP BY status")
            return {r["status"]: r["n"] for r in rows}

    def leads_created_since(self, since: datetime) -> list[datetime]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT created_at FROM leads WHERE deleted_at IS NULL AND created_at >= ?", (iso(since),)
            )
            return [parse_dt(r["created_at"]) for r in rows]

    def set_stage(self, lead_id: int, status: str, actor_id: int | None = None) -> bool:
        """Change the sales stage (recorded in stage_changes). Returns False if it was already in that stage."""
        if status not in STAGES:
            raise ValueError(f"unknown stage: {status}")
        with self.connect() as c:
            changed = self._change_stage(c, lead_id, status, actor_id)
            if changed:
                c.execute("UPDATE leads SET last_contact_at = ? WHERE id = ?", (iso(utcnow()), lead_id))
        return changed

    # contacts (calls): append-only, previous records are never overwritten --------
    def add_contact(
        self, lead_id: int, owner_id: int | None, outcome: str, note: str = "",
        duration_sec: int | None = None, next_followup: date | None = None,
    ) -> int:  # fmt: skip
        """Record one contact attempt. Reaching the customer moves 'new' leads to 'contacted'.

        `next_followup` replaces the lead's follow-up date; a conversation that ends without one clears it,
        while a missed attempt (no answer / wrong number) leaves the existing follow-up untouched.
        """
        if outcome not in CONTACT_OUTCOMES:
            raise ValueError(f"unknown contact outcome: {outcome}")
        if duration_sec is not None and duration_sec < 0:
            raise ValueError("duration cannot be negative")
        now, follow = iso(utcnow()), (next_followup.isoformat() if next_followup else None)
        with self.connect() as c:
            contact_id = c.execute(
                "INSERT INTO calls(lead_id, owner_id, note, created_at, outcome, duration_sec, next_followup) VALUES(?,?,?,?,?,?,?)",
                (lead_id, owner_id, note, now, outcome, duration_sec, follow),
            ).lastrowid
            if follow is not None or outcome in EFFECTIVE_OUTCOMES:
                c.execute("UPDATE leads SET last_contact_at = ?, next_followup = ? WHERE id = ?", (now, follow, lead_id))
            else:
                c.execute("UPDATE leads SET last_contact_at = ? WHERE id = ?", (now, lead_id))
            if outcome in EFFECTIVE_OUTCOMES:
                self._change_stage(c, lead_id, "contacted", owner_id, only_from=("new",))
            self._log(c, "call", owner_id, lead_id)
        return contact_id  # type: ignore[return-value]

    def add_call(self, lead_id: int, note: str, actor_id: int | None) -> int:
        """Backwards-compatible shortcut: a plain answered call."""
        return self.add_contact(lead_id, actor_id, "answered", note)

    def lead_contacts(self, lead_id: int, limit: int = 20) -> list[Contact]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT k.*, u.name AS owner_name FROM calls k LEFT JOIN users u ON u.id = k.owner_id "
                "WHERE k.lead_id = ? ORDER BY k.created_at DESC, k.id DESC LIMIT ?",
                (lead_id, limit),
            ).fetchall()
        return [
            Contact(r["id"], r["lead_id"], r["owner_id"], r["owner_name"] or "", r["outcome"], r["note"], r["duration_sec"], _day(r["next_followup"]), parse_dt(r["created_at"]))
            for r in rows
        ]

    # meetings --------------------------------------------------------------
    def add_meeting(self, lead_id: int, when: datetime, owner_id: int | None) -> int:
        with self.connect() as c:
            meeting_id = c.execute(
                "INSERT INTO meetings(lead_id, owner_id, scheduled_at, created_at) VALUES(?,?,?,?)",
                (lead_id, owner_id, iso(when), iso(utcnow())),
            ).lastrowid
            c.execute("UPDATE leads SET last_contact_at = ? WHERE id = ?", (iso(utcnow()), lead_id))
            self._change_stage(c, lead_id, "meeting", owner_id, only_from=("new", "contacted"))
            self._log(c, "meeting", owner_id, lead_id)
        return meeting_id  # type: ignore[return-value]

    def get_meeting(self, meeting_id: int) -> Meeting | None:
        with self.connect() as c:
            row = c.execute(f"{MEETING_SELECT} WHERE m.id = ?", (meeting_id,)).fetchone()  # noqa: S608
        return _meeting(row) if row else None

    def lead_meetings(self, lead_id: int, now: datetime | None = None) -> list[Meeting]:
        with self.connect() as c:
            rows = c.execute(
                f"{MEETING_SELECT} WHERE m.lead_id = ? AND m.status = 'scheduled' "  # noqa: S608
                "AND m.scheduled_at >= ? ORDER BY m.scheduled_at",
                (lead_id, iso(now or utcnow())),
            ).fetchall()
        return [_meeting(r) for r in rows]

    def meetings_between(self, start: datetime, end: datetime, owner_id: int | None = None) -> list[Meeting]:
        with self.connect() as c:
            rows = c.execute(
                f"{MEETING_SELECT} WHERE m.status = 'scheduled' AND m.scheduled_at >= ? "  # noqa: S608
                "AND m.scheduled_at < ? AND (? IS NULL OR m.owner_id = ?) ORDER BY m.scheduled_at",
                (iso(start), iso(end), owner_id, owner_id),
            ).fetchall()
        return [_meeting(r) for r in rows]

    def cancel_meeting(self, meeting_id: int, actor_id: int | None) -> bool:
        with self.connect() as c:
            changed = c.execute(
                "UPDATE meetings SET status = 'cancelled', status_at = ? WHERE id = ? AND status = 'scheduled'",
                (iso(utcnow()), meeting_id),
            ).rowcount
            if changed:
                self._log(c, "meeting_cancelled", actor_id)
        return bool(changed)

    def complete_meeting(self, meeting_id: int, status: str, notes: str = "", actor_id: int | None = None, next_followup: date | None = None) -> bool:
        """Record the outcome of a scheduled meeting: 'done' (held) or 'no_show' (did not take place)."""
        if status not in ("done", "no_show"):
            raise ValueError(f"unsupported outcome: {status}")
        now = iso(utcnow())
        with self.connect() as c:
            row = c.execute("SELECT lead_id FROM meetings WHERE id = ? AND status = 'scheduled'", (meeting_id,)).fetchone()
            if row is None:
                return False
            c.execute(
                "UPDATE meetings SET status = ?, notes = ?, status_at = ?, next_followup = ? WHERE id = ?",
                (status, notes, now, next_followup.isoformat() if next_followup else None, meeting_id),
            )
            if status == "done":  # a meeting that really happened counts as contact with the customer
                c.execute("UPDATE leads SET last_contact_at = ? WHERE id = ?", (now, row["lead_id"]))
            if next_followup is not None:
                c.execute("UPDATE leads SET next_followup = ? WHERE id = ?", (next_followup.isoformat(), row["lead_id"]))
            self._log(c, f"meeting_{status}", actor_id, row["lead_id"])
        return True

    def meetings_pending_outcome(self, now: datetime, owner_id: int | None = None, limit: int = 10) -> list[Meeting]:
        """Meetings whose time has passed but whose outcome nobody recorded yet."""
        with self.connect() as c:
            rows = c.execute(
                f"{MEETING_SELECT} WHERE m.status = 'scheduled' AND m.scheduled_at < ? "  # noqa: S608
                "AND (? IS NULL OR m.owner_id = ?) ORDER BY m.scheduled_at DESC LIMIT ?",
                (iso(now), owner_id, owner_id, limit),
            ).fetchall()
        return [_meeting(r) for r in rows]

    def lead_meeting_history(self, lead_id: int, limit: int = 20) -> list[Meeting]:
        with self.connect() as c:
            rows = c.execute(
                f"{MEETING_SELECT} WHERE m.lead_id = ? ORDER BY m.scheduled_at DESC LIMIT ?", (lead_id, limit)  # noqa: S608
            ).fetchall()
        return [_meeting(r) for r in rows]

    def reschedule_meeting(self, meeting_id: int, when: datetime, actor_id: int | None) -> None:
        with self.connect() as c:
            c.execute(
                "UPDATE meetings SET scheduled_at = ?, reminder_sent_at = NULL WHERE id = ? AND status = 'scheduled'",
                (iso(when), meeting_id),
            )
            self._log(c, "meeting_rescheduled", actor_id)

    def due_reminders(self, now: datetime, ahead: timedelta) -> list[Meeting]:
        with self.connect() as c:
            rows = c.execute(
                f"{MEETING_SELECT} WHERE m.status = 'scheduled' AND m.reminder_sent_at IS NULL "  # noqa: S608
                "AND m.scheduled_at > ? AND m.scheduled_at <= ? AND m.owner_id IS NOT NULL",
                (iso(now), iso(now + ahead)),
            ).fetchall()
        return [_meeting(r) for r in rows]

    def claim_reminder(self, meeting_id: int, now: datetime | None = None) -> bool:
        """Atomically reserve a reminder; False means someone else already sent it (or the meeting is no longer scheduled)."""
        with self.connect() as c:
            return bool(
                c.execute(
                    "UPDATE meetings SET reminder_sent_at = ? WHERE id = ? AND reminder_sent_at IS NULL AND status = 'scheduled'",
                    (iso(now or utcnow()), meeting_id),
                ).rowcount
            )

    def release_reminder(self, meeting_id: int) -> None:
        """Undo a claim after a failed delivery so the next tick retries it."""
        with self.connect() as c:
            c.execute("UPDATE meetings SET reminder_sent_at = NULL WHERE id = ?", (meeting_id,))

    # notification ledger -----------------------------------------------------
    def claim_notification(self, key: str, user_id: int | None = None) -> bool:
        """Reserve a notification key. True = caller must deliver it; False = already delivered/claimed."""
        with self.connect() as c:
            return bool(
                c.execute(
                    "INSERT OR IGNORE INTO notification_log(key, user_id, created_at) VALUES(?,?,?)",
                    (key, user_id, iso(utcnow())),
                ).rowcount
            )

    def release_notification(self, key: str) -> None:
        with self.connect() as c:
            c.execute("DELETE FROM notification_log WHERE key = ?", (key,))

    def prune_notifications(self, keep_days: int = 60) -> int:
        with self.connect() as c:
            return c.execute(
                "DELETE FROM notification_log WHERE created_at < ?", (iso(utcnow() - timedelta(days=keep_days)),)
            ).rowcount

    # tasks -----------------------------------------------------------------
    def add_task(self, owner_id: int, title: str, due: date, priority: str = "medium", lead_id: int | None = None) -> Task:
        if priority not in PRIORITIES:
            raise ValueError(f"unknown priority: {priority}")
        with self.connect() as c:
            task_id = c.execute(
                "INSERT INTO tasks(owner_id, title, due_date, priority, lead_id, created_at) VALUES(?,?,?,?,?,?)",
                (owner_id, title, due.isoformat(), priority, lead_id, iso(utcnow())),
            ).lastrowid
            row = c.execute(f"{TASK_SELECT} AND t.id = ?", (task_id,)).fetchone()  # noqa: S608
        return _task(row)

    def get_task(self, task_id: int) -> Task | None:
        with self.connect() as c:
            row = c.execute(f"{TASK_SELECT} AND t.id = ?", (task_id,)).fetchone()  # noqa: S608
        return _task(row) if row else None

    def update_task(self, task_id: int, owner_id: int, **fields: object) -> bool:
        """Edit title, due_date (a date) or priority of one of the owner's tasks."""
        if not fields or not set(fields) <= {"title", "due_date", "priority"}:
            raise ValueError(f"cannot edit fields: {sorted(fields)}")
        if "priority" in fields and fields["priority"] not in PRIORITIES:
            raise ValueError(f"unknown priority: {fields['priority']}")
        values = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in fields.items()}
        sets = ", ".join(f"{k} = ?" for k in values)
        with self.connect() as c:
            return bool(c.execute(f"UPDATE tasks SET {sets} WHERE id = ? AND owner_id = ? AND deleted_at IS NULL", (*values.values(), task_id, owner_id)).rowcount)  # noqa: S608

    def delete_task(self, task_id: int, owner_id: int) -> bool:
        with self.connect() as c:
            return bool(c.execute("UPDATE tasks SET deleted_at = ? WHERE id = ? AND owner_id = ? AND deleted_at IS NULL", (iso(utcnow()), task_id, owner_id)).rowcount)

    def reopen_task(self, task_id: int, owner_id: int) -> bool:
        with self.connect() as c:
            return bool(c.execute("UPDATE tasks SET done_at = NULL WHERE id = ? AND owner_id = ? AND done_at IS NOT NULL AND deleted_at IS NULL", (task_id, owner_id)).rowcount)

    def list_tasks(self, owner_id: int, view: str, today: date, limit: int = 15) -> list[Task]:
        """view: 'pending' (not done), 'overdue' (not done, past due) or 'done' (latest first)."""
        order = "CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END"
        where, args, sort = {
            "pending": ("t.done_at IS NULL", (), f"t.due_date, {order}, t.id"),
            "overdue": ("t.done_at IS NULL AND t.due_date < ?", (today.isoformat(),), f"t.due_date, {order}, t.id"),
            "done": ("t.done_at IS NOT NULL", (), "t.done_at DESC"),
        }[view]
        with self.connect() as c:
            rows = c.execute(f"{TASK_SELECT} AND t.owner_id = ? AND {where} ORDER BY {sort} LIMIT ?", (owner_id, *args, limit)).fetchall()  # noqa: S608
        return [_task(r) for r in rows]

    def open_tasks(self, owner_id: int, until: date) -> list[Task]:
        """Unfinished tasks due on or before `until`, overdue ones included."""
        with self.connect() as c:
            rows = c.execute(
                f"{TASK_SELECT} AND t.owner_id = ? AND t.done_at IS NULL AND t.due_date <= ? "
                "ORDER BY t.due_date, CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, t.id",
                (owner_id, until.isoformat()),
            ).fetchall()
        return [_task(r) for r in rows]

    def count_upcoming(self, owner_id: int, after: date) -> int:
        with self.connect() as c:
            return c.execute(
                "SELECT COUNT(*) FROM tasks WHERE deleted_at IS NULL AND owner_id = ? AND done_at IS NULL AND due_date > ?",
                (owner_id, after.isoformat()),
            ).fetchone()[0]

    def complete_task(self, task_id: int, owner_id: int) -> bool:
        with self.connect() as c:
            changed = c.execute(
                "UPDATE tasks SET done_at = ? WHERE id = ? AND owner_id = ? AND done_at IS NULL AND deleted_at IS NULL",
                (iso(utcnow()), task_id, owner_id),
            ).rowcount
            if changed:
                self._log(c, "task_done", owner_id)
        return bool(changed)

    def task_load(self, today: date) -> dict[int, tuple[int, int]]:
        """owner_id -> (open tasks, overdue tasks)."""
        with self.connect() as c:
            rows = c.execute(
                "SELECT owner_id, COUNT(*) AS open_n, SUM(due_date < ?) AS late_n "
                "FROM tasks WHERE done_at IS NULL AND deleted_at IS NULL GROUP BY owner_id",
                (today.isoformat(),),
            )
            return {r["owner_id"]: (r["open_n"], r["late_n"] or 0) for r in rows}

    # notes -------------------------------------------------------------------
    def add_note(self, lead_id: int, author_id: int | None, body: str) -> int:
        with self.connect() as c:
            note_id = c.execute("INSERT INTO notes(lead_id, author_id, body, created_at) VALUES(?,?,?,?)", (lead_id, author_id, body, iso(utcnow()))).lastrowid
            self._log(c, "note", author_id, lead_id)
        return note_id  # type: ignore[return-value]

    def get_note(self, note_id: int) -> Note | None:
        with self.connect() as c:
            row = c.execute("SELECT n.*, u.name AS author_name FROM notes n LEFT JOIN users u ON u.id = n.author_id WHERE n.id = ? AND n.deleted_at IS NULL", (note_id,)).fetchone()
        return _note(row) if row else None

    def edit_note(self, note_id: int, body: str) -> bool:
        with self.connect() as c:
            return bool(c.execute("UPDATE notes SET body = ?, updated_at = ? WHERE id = ? AND deleted_at IS NULL", (body, iso(utcnow()), note_id)).rowcount)

    def delete_note(self, note_id: int) -> bool:
        """Soft delete: the note disappears from the profile and timeline but stays in the database."""
        with self.connect() as c:
            return bool(c.execute("UPDATE notes SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL", (iso(utcnow()), note_id)).rowcount)

    def lead_notes(self, lead_id: int, limit: int = 20) -> list[Note]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT n.*, u.name AS author_name FROM notes n LEFT JOIN users u ON u.id = n.author_id "
                "WHERE n.lead_id = ? AND n.deleted_at IS NULL ORDER BY n.created_at DESC, n.id DESC LIMIT ?",
                (lead_id, limit),
            ).fetchall()
        return [_note(r) for r in rows]

    # profile data --------------------------------------------------------------
    def lead_timeline(self, lead_id: int, limit: int = 40) -> list[TimelineItem]:
        """Contacts, meetings, notes and stage changes of one customer, newest first."""
        sql = (
            "SELECT 'contact' AS kind, k.created_at AS at, k.outcome AS a, k.note AS b, COALESCE(k.next_followup, '') AS c, k.owner_id AS uid FROM calls k WHERE k.lead_id = :id "
            "UNION ALL SELECT 'meeting', m.created_at, m.status, m.notes, m.scheduled_at, m.owner_id FROM meetings m WHERE m.lead_id = :id "
            "UNION ALL SELECT 'note', n.created_at, '', n.body, '', n.author_id FROM notes n WHERE n.lead_id = :id AND n.deleted_at IS NULL "
            "UNION ALL SELECT 'stage', s.created_at, COALESCE(s.from_stage, ''), s.to_stage, '', s.user_id FROM stage_changes s WHERE s.lead_id = :id "
            "ORDER BY at DESC LIMIT :limit"
        )
        with self.connect() as c:
            rows = c.execute(sql, {"id": lead_id, "limit": limit}).fetchall()
        names = {u.id: u.name for u in self.users()}
        return [TimelineItem(r["kind"], parse_dt(r["at"]), r["a"], r["b"], names.get(r["uid"], ""), r["c"]) for r in rows]

    def lead_counts(self, lead_id: int) -> dict[str, int]:
        with self.connect() as c:
            row = c.execute(
                "SELECT (SELECT COUNT(*) FROM calls WHERE lead_id = :id) AS contacts, "
                "(SELECT COUNT(*) FROM meetings WHERE lead_id = :id) AS meetings, "
                "(SELECT COUNT(*) FROM notes WHERE lead_id = :id AND deleted_at IS NULL) AS notes",
                {"id": lead_id},
            ).fetchone()
        return dict(row)

    # events, exports -------------------------------------------------------
    def events_between(self, start: datetime, end: datetime) -> list[tuple[str, int | None, datetime]]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT e.kind, e.owner_id, e.created_at FROM events e "
                "LEFT JOIN leads l ON l.id = e.lead_id "
                "WHERE e.created_at >= ? AND e.created_at < ? AND (l.id IS NULL OR l.deleted_at IS NULL)",
                (iso(start), iso(end)),
            ).fetchall()
        return [(r["kind"], r["owner_id"], parse_dt(r["created_at"])) for r in rows]

    def export_leads(self, owner_id: int | None = None) -> list[sqlite3.Row]:
        with self.connect() as c:
            return c.execute(
                "SELECT l.id, l.name, l.company, l.phone, l.status, u.name AS owner_name, l.created_at, "
                "l.last_contact_at, (SELECT COUNT(*) FROM calls k WHERE k.lead_id = l.id) AS calls, "
                "(SELECT MIN(m.scheduled_at) FROM meetings m WHERE m.lead_id = l.id "
                " AND m.status = 'scheduled' AND m.scheduled_at >= ?) AS next_meeting "
                "FROM leads l LEFT JOIN users u ON u.id = l.owner_id "
                "WHERE l.deleted_at IS NULL AND (? IS NULL OR l.owner_id = ?) ORDER BY l.id",
                (iso(utcnow()), owner_id, owner_id),
            ).fetchall()

    def export_meetings(self, owner_id: int | None = None) -> list[sqlite3.Row]:
        with self.connect() as c:
            return c.execute(
                "SELECT m.id, l.name AS lead_name, l.company, u.name AS owner_name, m.scheduled_at, m.status "
                "FROM meetings m JOIN leads l ON l.id = m.lead_id AND l.deleted_at IS NULL "
                "LEFT JOIN users u ON u.id = m.owner_id "
                "WHERE (? IS NULL OR m.owner_id = ?) ORDER BY m.scheduled_at DESC",
                (owner_id, owner_id),
            ).fetchall()

    # conversation sessions and key-value -----------------------------------
    def get_session(self, platform: str, chat_id: str) -> tuple[str, dict[str, Any]]:
        with self.connect() as c:
            row = c.execute("SELECT state, data FROM sessions WHERE platform = ? AND chat_id = ?", (platform, chat_id)).fetchone()
        return (row["state"], json.loads(row["data"])) if row else ("", {})

    def save_session(self, platform: str, chat_id: str, state: str, data: dict[str, Any]) -> None:
        with self.connect() as c:
            c.execute(
                "INSERT INTO sessions(platform, chat_id, state, data, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(platform, chat_id) DO UPDATE SET state = excluded.state, "
                "data = excluded.data, updated_at = excluded.updated_at",
                (platform, chat_id, state, json.dumps(data, ensure_ascii=False), iso(utcnow())),
            )

    def kv_get(self, key: str) -> str | None:
        with self.connect() as c:
            row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def kv_set(self, key: str, value: str) -> None:
        with self.connect() as c:
            c.execute("INSERT INTO kv(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))

    def backup_to(self, target: Path) -> None:
        """Consistent online snapshot (SQLite backup API, safe while the bot runs).

        Written to a temporary file, verified, then renamed, so `target` is never a partial file.
        """
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.unlink(missing_ok=True)
        try:
            with self.connect() as src:
                dest = sqlite3.connect(tmp)
                try:
                    src.backup(dest)
                    dest.execute("PRAGMA journal_mode = DELETE")  # a snapshot must be one self-contained file
                finally:
                    dest.close()
            verify_backup(tmp)
            tmp.replace(target)
        finally:
            for suffix in ("", "-wal", "-shm", "-journal"):
                Path(f"{tmp}{suffix}").unlink(missing_ok=True)
