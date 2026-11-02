"""P1 data layer: source, contacts, stage history, notes, meetings, tasks, timeline."""

from datetime import date, datetime, timedelta, timezone

from salesdesk import crm
from salesdesk.bot.engine import Router
from salesdesk.db import Database
from salesdesk.domain import CONTACT_OUTCOMES, utcnow


def _env(tmp_path):
    db = Database(tmp_path / "t.db")
    sara = db.upsert_user("bale", "1", "1", "سارا", "employee", active=True)
    return db, sara


def _stages(db, lead_id):
    with db.connect() as c:
        return [(r["from_stage"], r["to_stage"], r["user_id"]) for r in c.execute("SELECT * FROM stage_changes WHERE lead_id = ? ORDER BY id", (lead_id,))]


def _raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc:
        return
    raise AssertionError(f"expected {exc.__name__}")


# --- acquisition source ---------------------------------------------------------------
def test_source_is_stored_validated_and_editable(tmp_path):
    db, sara = _env(tmp_path)
    lead = db.add_lead("رضایی", "", "", sara.id, source="exhibition")
    assert db.get_lead(lead.id).source == "exhibition"
    assert db.add_lead("بی‌منبع", "", "", sara.id).source == ""  # optional
    db.update_lead(lead.id, source="website")
    assert db.get_lead(lead.id).source == "website"
    _raises(ValueError, db.update_lead, lead.id, source="telepathy")
    _raises(ValueError, db.add_lead, "x", "", "", sara.id, "telepathy")


# --- contacts --------------------------------------------------------------------------
def test_contact_records_all_fields_and_history_is_never_overwritten(tmp_path):
    db, sara = _env(tmp_path)
    ali = db.upsert_user("bale", "2", "2", "علی", "employee", active=True)
    lead = db.add_lead("رضایی", "", "", sara.id)
    follow = date(2026, 11, 20)
    first = db.add_contact(lead.id, sara.id, "answered", "اولین تماس", duration_sec=300, next_followup=follow)
    second = db.add_contact(lead.id, ali.id, "no_answer")
    history = db.lead_contacts(lead.id)
    assert [c.id for c in history] == [second, first]  # newest first, both still there
    old = history[1]
    assert (old.lead_id, old.owner_id, old.owner_name, old.outcome, old.note, old.duration_sec, old.next_followup) == (lead.id, sara.id, "سارا", "answered", "اولین تماس", 300, follow)
    assert old.created_at <= utcnow() and history[0].duration_sec is None and history[0].next_followup is None
    assert history[0].owner_name == "علی"


def test_every_outcome_is_accepted_and_only_reaching_the_customer_advances_the_stage(tmp_path):
    db, sara = _env(tmp_path)
    for outcome in CONTACT_OUTCOMES:
        lead = db.add_lead(outcome, "", "", sara.id)
        db.add_contact(lead.id, sara.id, outcome)
        expected = "new" if outcome in ("no_answer", "wrong_number") else "contacted"
        assert db.get_lead(lead.id).status == expected, outcome
    _raises(ValueError, db.add_contact, lead.id, sara.id, "teleported")
    _raises(ValueError, db.add_contact, lead.id, sara.id, "answered", duration_sec=-5)


def test_followup_date_rules(tmp_path):
    db, sara = _env(tmp_path)
    lead = db.add_lead("رضایی", "", "", sara.id)
    db.add_contact(lead.id, sara.id, "callback", next_followup=date(2026, 12, 1))
    assert db.get_lead(lead.id).next_followup == date(2026, 12, 1)
    db.add_contact(lead.id, sara.id, "no_answer")  # a missed attempt keeps the promise alive
    assert db.get_lead(lead.id).next_followup == date(2026, 12, 1)
    db.add_contact(lead.id, sara.id, "answered", next_followup=date(2026, 12, 9))
    assert db.get_lead(lead.id).next_followup == date(2026, 12, 9)  # replaced
    db.add_contact(lead.id, sara.id, "not_interested")  # conversation ended with no new date: cleared
    assert db.get_lead(lead.id).next_followup is None


# --- stage transitions -----------------------------------------------------------------
def test_every_stage_transition_records_previous_new_user_and_time(tmp_path):
    db, sara = _env(tmp_path)
    ali = db.upsert_user("bale", "2", "2", "علی", "employee", active=True)
    lead = db.add_lead("رضایی", "", "", sara.id)
    before = utcnow()
    db.add_contact(lead.id, sara.id, "answered")  # automatic new -> contacted
    db.add_meeting(lead.id, utcnow() + timedelta(days=1), sara.id)  # automatic contacted -> meeting
    assert db.set_stage(lead.id, "proposal", ali.id) is True
    assert db.set_stage(lead.id, "proposal", ali.id) is False  # unchanged: no duplicate record
    db.set_stage(lead.id, "won", sara.id)
    assert _stages(db, lead.id) == [(None, "new", sara.id), ("new", "contacted", sara.id), ("contacted", "meeting", sara.id),
                                    ("meeting", "proposal", ali.id), ("proposal", "won", sara.id)]
    with db.connect() as c:
        stamps = [r[0] for r in c.execute("SELECT created_at FROM stage_changes WHERE lead_id = ? ORDER BY id", (lead.id,))]
    assert stamps == sorted(stamps) and stamps[-1] >= before.isoformat()[:19]
    _raises(ValueError, db.set_stage, lead.id, "nonsense")


def test_stage_is_not_dragged_backwards_by_later_activity(tmp_path):
    db, sara = _env(tmp_path)
    lead = db.add_lead("رضایی", "", "", sara.id)
    db.set_stage(lead.id, "proposal", sara.id)
    db.add_contact(lead.id, sara.id, "answered")
    db.add_meeting(lead.id, utcnow() + timedelta(days=1), sara.id)
    assert db.get_lead(lead.id).status == "proposal"


# --- notes -----------------------------------------------------------------------------
def test_notes_can_be_added_edited_and_deleted_independently(tmp_path):
    db, sara = _env(tmp_path)
    lead = db.add_lead("رضایی", "", "", sara.id)
    n1, n2 = db.add_note(lead.id, sara.id, "اولی"), db.add_note(lead.id, sara.id, "دومی")
    assert [n.body for n in db.lead_notes(lead.id)] == ["دومی", "اولی"] and db.get_note(n1).author_name == "سارا"
    assert db.get_note(n1).updated_at is None
    assert db.edit_note(n1, "اولی (ویرایش‌شده)") and db.get_note(n1).updated_at is not None
    assert db.delete_note(n2) and db.get_note(n2) is None and db.delete_note(n2) is False
    assert [n.body for n in db.lead_notes(lead.id)] == ["اولی (ویرایش‌شده)"]
    assert db.lead_counts(lead.id)["notes"] == 1
    with db.connect() as c:  # soft delete: the row is still stored
        assert c.execute("SELECT COUNT(*) FROM notes").fetchone()[0] == 2
    assert db.edit_note(n2, "x") is False


# --- meetings --------------------------------------------------------------------------
def test_meeting_outcomes_and_history(tmp_path):
    db, sara = _env(tmp_path)
    lead = db.add_lead("رضایی", "", "", sara.id)
    now = utcnow()
    held = db.add_meeting(lead.id, now - timedelta(days=2), sara.id)
    missed = db.add_meeting(lead.id, now - timedelta(days=1), sara.id)
    cancelled = db.add_meeting(lead.id, now + timedelta(days=1), sara.id)
    future = db.add_meeting(lead.id, now + timedelta(days=2), sara.id)
    assert [m.id for m in db.meetings_pending_outcome(now)] == [missed, held]  # past, still "scheduled"
    assert db.complete_meeting(held, "done", "خوب پیش رفت", sara.id, next_followup=date(2026, 12, 5)) is True
    assert db.complete_meeting(missed, "no_show", "نیامد", sara.id) is True
    assert db.cancel_meeting(cancelled, sara.id) is True
    got = {m.id: m for m in db.lead_meeting_history(lead.id)}
    assert [(got[i].status, got[i].notes) for i in (held, missed, cancelled, future)] == [("done", "خوب پیش رفت"), ("no_show", "نیامد"), ("cancelled", ""), ("scheduled", "")]
    assert got[held].status_at is not None and got[future].status_at is None and got[held].next_followup == date(2026, 12, 5)
    assert db.get_lead(lead.id).next_followup == date(2026, 12, 5)
    assert db.meetings_pending_outcome(now) == [] and [m.id for m in db.lead_meetings(lead.id, now)] == [future]
    assert not db.complete_meeting(held, "done") and not db.complete_meeting(cancelled, "done") and not db.cancel_meeting(held, sara.id)  # finished meetings are final
    _raises(ValueError, db.complete_meeting, future, "cancelled")


def test_only_a_held_meeting_counts_as_contact(tmp_path):
    db, sara = _env(tmp_path)
    lead = db.add_lead("رضایی", "", "", sara.id)
    old = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with db.connect() as c:
        c.execute("UPDATE leads SET last_contact_at = ? WHERE id = ?", ("2026-01-01T00:00:00.000+00:00", lead.id))
    a, b = db.add_meeting(lead.id, utcnow() - timedelta(hours=3), sara.id), db.add_meeting(lead.id, utcnow() - timedelta(hours=2), sara.id)
    with db.connect() as c:
        c.execute("UPDATE leads SET last_contact_at = ? WHERE id = ?", ("2026-01-01T00:00:00.000+00:00", lead.id))
    db.complete_meeting(a, "no_show", actor_id=sara.id)
    assert db.get_lead(lead.id).last_contact_at == old  # a no-show is not a contact
    db.complete_meeting(b, "done", actor_id=sara.id)
    assert db.get_lead(lead.id).last_contact_at > old


# --- tasks -----------------------------------------------------------------------------
def test_task_priority_edit_delete_reopen_and_views(tmp_path):
    db, sara = _env(tmp_path)
    ali = db.upsert_user("bale", "2", "2", "علی", "employee", active=True)
    today = date(2026, 11, 1)
    low = db.add_task(sara.id, "کم", today, "low")
    high = db.add_task(sara.id, "زیاد", today, "high")
    mid = db.add_task(sara.id, "متوسط", today)  # default priority
    late = db.add_task(sara.id, "عقب‌افتاده", today - timedelta(days=3), "low")
    future = db.add_task(sara.id, "آینده", today + timedelta(days=5), "high")
    assert [t.title for t in db.open_tasks(sara.id, today)] == ["عقب‌افتاده", "زیاد", "متوسط", "کم"]  # overdue first, then by priority
    assert [t.title for t in db.list_tasks(sara.id, "overdue", today)] == ["عقب‌افتاده"]
    assert len(db.list_tasks(sara.id, "pending", today)) == 5
    assert db.complete_task(high.id, sara.id) and [t.title for t in db.list_tasks(sara.id, "done", today)] == ["زیاد"]
    assert db.get_task(high.id).done and db.reopen_task(high.id, sara.id) and not db.get_task(high.id).done
    assert db.reopen_task(high.id, sara.id) is False  # already open
    assert db.update_task(low.id, sara.id, title="ویرایش‌شده", priority="high", due_date=today + timedelta(days=1))
    edited = db.get_task(low.id)
    assert (edited.title, edited.priority, edited.due_date) == ("ویرایش‌شده", "high", today + timedelta(days=1))
    assert not db.update_task(low.id, ali.id, title="هک") and db.get_task(low.id).title == "ویرایش‌شده"  # owner only
    assert db.delete_task(mid.id, sara.id) and db.get_task(mid.id) is None and not db.delete_task(mid.id, sara.id)
    assert mid.id not in [t.id for t in db.open_tasks(sara.id, today)] and db.task_load(today)[sara.id] == (4, 1)
    assert db.count_upcoming(sara.id, today) == 2 and late.id and future.id
    _raises(ValueError, db.add_task, sara.id, "x", today, "urgent")
    _raises(ValueError, db.update_task, low.id, sara.id, priority="urgent")
    _raises(ValueError, db.update_task, low.id, sara.id, lead_id=99)  # fields outside title/date/priority are refused


# --- timeline and profile ----------------------------------------------------------------
def test_timeline_merges_all_activity_newest_first_and_hides_deleted_notes(tmp_path):
    db, sara = _env(tmp_path)
    lead = db.add_lead("رضایی", "نوین", "0912", sara.id, "referral")
    db.add_contact(lead.id, sara.id, "interested", "علاقه دارد", next_followup=date(2026, 12, 1))
    note = db.add_note(lead.id, sara.id, "یادداشت موقت")
    meeting = db.add_meeting(lead.id, utcnow() + timedelta(days=2), sara.id)
    db.set_stage(lead.id, "proposal", sara.id)
    db.delete_note(note)
    kinds = [(i.kind, i.a, i.b) for i in db.lead_timeline(lead.id)]
    # strictly chronological, newest first: an action comes before the stage change it triggered
    assert kinds == [("stage", "meeting", "proposal"), ("stage", "contacted", "meeting"), ("meeting", "scheduled", ""),
                     ("stage", "new", "contacted"), ("contact", "interested", "علاقه دارد"), ("stage", "", "new")]
    assert all(i.user_name == "سارا" for i in db.lead_timeline(lead.id))
    assert db.lead_timeline(lead.id)[4].c == "2026-12-01" and meeting
    assert len(db.lead_timeline(lead.id, limit=2)) == 2


def test_profile_assembles_everything_and_handles_deleted_leads(tmp_path):
    db, sara = _env(tmp_path)
    lead = db.add_lead("رضایی", "نوین", "0912", sara.id, "website")
    db.add_contact(lead.id, sara.id, "answered", "x")
    db.add_note(lead.id, sara.id, "n")
    db.add_meeting(lead.id, utcnow() + timedelta(days=1), sara.id)
    profile = crm.build_profile(db, lead.id, utcnow())
    assert profile.lead.source == "website" and profile.counts == {"contacts": 1, "meetings": 1, "notes": 1}
    assert len(profile.upcoming_meetings) == 1 and profile.last_activity == profile.recent_activity[0].at
    db.delete_lead(lead.id, sara.id)
    assert crm.build_profile(db, lead.id, utcnow()) is None
    assert crm.followup_date(date(2026, 11, 1), "3") == date(2026, 11, 4) and crm.followup_date(date(2026, 11, 1), "x") is None


def test_router_refuses_duplicate_handlers():
    router = Router()
    router.command("a")(lambda ctx, arg="": None)
    _raises(ValueError, router.command("a"), lambda ctx, arg="": None)
