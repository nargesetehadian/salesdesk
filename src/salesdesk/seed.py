"""Fill the database with realistic fake data for demos: `python -m salesdesk.seed`."""

from __future__ import annotations

import argparse
import random
import sys
from datetime import timedelta
from pathlib import Path

from .config import Settings
from .db import Database
from .domain import iso, utcnow

SURNAMES = ["رضایی", "احمدی", "کریمی", "موسوی", "حسینی", "صادقی", "نجفی", "رحیمی", "قاسمی", "جعفری",
            "مرادی", "فتحی", "سلیمانی", "اکبری", "یوسفی", "زارعی", "شریفی", "عباسی", "کاظمی", "طاهری"]  # fmt: skip
COMPANIES = ["نوین‌تک", "آسمان صنعت", "پارس ارتباط", "دیجی‌پیشرو", "البرز گستر", "آرمان داده", "سپهر نرم"]
TEAM = [("1001", "مدیرعامل", "ceo"), ("1002", "سارا محمدی", "employee"), ("1003", "علی کریمی", "employee")]
TASKS = ["ارسال پیشنهاد قیمت", "تماس پیگیری", "هماهنگی جلسه معرفی", "ارسال قرارداد", "پیگیری پرداخت"]
FUNNEL_PATH = ["new", "contacted", "meeting", "proposal", "won"]


def seed(db: Database, settings: Settings, seed_value: int = 7) -> None:
    rng, now = random.Random(seed_value), utcnow()
    users = [db.upsert_user("bale", ext, ext, name, role, active=True) for ext, name, role in TEAM]
    staff = [u.id for u in users if not u.is_ceo]
    with db.connect() as c:
        for table in ("notification_log", "stage_changes", "deals", "notes", "events", "tasks", "meetings", "calls", "leads"):
            c.execute(f"DELETE FROM {table}")  # noqa: S608 (fixed table names)

        def log(kind: str, owner: int, lead: int, at) -> None:  # noqa: ANN001
            c.execute("INSERT INTO events(kind, owner_id, lead_id, created_at) VALUES(?,?,?,?)",
                      (kind, owner, lead, iso(at)))  # fmt: skip

        for n in range(70):
            owner = rng.choice(staff)
            created = now - timedelta(days=rng.randint(0, 85), hours=rng.randint(0, 20))
            depth = min(rng.choices(range(5), weights=[3, 4, 3, 2, 1.4])[0], 4)
            if rng.random() < 0.1:
                depth = -1  # lost
            status = "lost" if depth < 0 else FUNNEL_PATH[depth]
            last = min(created + timedelta(days=rng.randint(0, 20)), now - timedelta(hours=2))
            lead = c.execute(
                "INSERT INTO leads(name, company, phone, status, owner_id, created_at, last_contact_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (SURNAMES[n % len(SURNAMES)], rng.choice(COMPANIES), f"0912{rng.randint(1000000, 9999999)}",
                 status, owner, iso(created), iso(last)),
            ).lastrowid  # fmt: skip
            def within(t=created):  # a random moment between creation and now
                return t + (now - t) * rng.random()

            c.execute("INSERT INTO stage_changes(lead_id, from_stage, to_stage, user_id, created_at) VALUES(?, NULL, 'new', ?, ?)", (lead, owner, iso(created)))
            if status != "new":
                c.execute("INSERT INTO stage_changes(lead_id, from_stage, to_stage, user_id, created_at) VALUES(?, 'new', ?, ?, ?)", (lead, status, owner, iso(last)))
            log("lead", owner, lead, created)
            if depth >= 1:
                for _ in range(rng.randint(1, 3)):
                    log("call", owner, lead, within())
            if depth >= 2:
                when = created + timedelta(days=rng.randint(1, 15))
                if status == "meeting":
                    when = now + timedelta(days=rng.randint(0, 5), hours=rng.randint(-2, 3))
                c.execute("INSERT INTO meetings(lead_id, scheduled_at, owner_id, status, created_at) VALUES(?,?,?,?,?)",
                          (lead, iso(when), owner, "done" if when < now else "scheduled", iso(created)))  # fmt: skip
                log("meeting", owner, lead, within())
            if status == "won":
                log("stage_won", owner, lead, last)

        for owner in staff:
            for i in range(12):
                due = (now + timedelta(days=rng.randint(-3, 6))).date()
                done = rng.random() < 0.55 and due <= now.date()
                c.execute("INSERT INTO tasks(owner_id, title, due_date, done_at, created_at) VALUES(?,?,?,?,?)",
                          (owner, rng.choice(TASKS), due.isoformat(), iso(now) if done else None, iso(now)))  # fmt: skip
                if done:
                    log("task_done", owner, 0, now - timedelta(days=rng.randint(0, 40)))


def main(argv: list[str] | None = None) -> int:
    """Create a demo database. Refuses to touch a database that holds real data."""
    parser = argparse.ArgumentParser(prog="python -m salesdesk.seed", description=__doc__)
    parser.add_argument("--db", type=Path, help="target database (default: DEMO_DB_PATH, i.e. data/demo.db)")
    parser.add_argument("--force", action="store_true", help="overwrite even if the database is not a demo one")
    args = parser.parse_args(argv)
    cfg = Settings.from_env(need_platform=False)
    target = args.db or cfg.demo_db_path
    if not args.force and target.exists() and target.resolve() == cfg.db_path.resolve():
        print(f"Refusing to seed the production database {target}. Use the default demo path or --force.", file=sys.stderr)
        return 2
    db = Database(target)
    if not args.force and db.status_counts() and db.kv_get("demo") != "1":
        print(f"{target} already contains customers and is not a demo database. Use --force to overwrite.", file=sys.stderr)
        return 2
    seed(db, cfg)
    db.kv_set("demo", "1")
    print(f"Seeded demo data into {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
