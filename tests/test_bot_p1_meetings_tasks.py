"""P1 conversations: meeting outcomes and task management."""

from datetime import timedelta

from harness import Chat, make_bot
from test_bot_p1 import _jalali, _new, _setup, _tomorrow

from salesdesk.domain import utcnow
from salesdesk.fa import fa_digits


def test_meeting_outcomes_are_recorded_with_notes_and_followup(tmp_path):
    db, cfg, sara = _setup(tmp_path)
    _new(sara)
    lead, user = db.search_leads()[0][0], db.users()[0]
    held = db.add_meeting(lead.id, utcnow() - timedelta(hours=3), user.id)
    later = db.add_meeting(lead.id, utcnow() + timedelta(days=1), user.id)

    sara.say("📅 قرارها")
    assert "منتظر ثبت نتیجه: ۱" in sara.last
    sara.click("⏳").click("برگزار شد").say("خوب پیش رفت").click("هفته‌ی بعد")
    meeting = db.get_meeting(held)
    week_ahead = utcnow().astimezone(cfg.timezone).date() + timedelta(days=7)
    assert (meeting.status, meeting.notes, meeting.next_followup) == ("done", "خوب پیش رفت", week_ahead)
    assert db.get_lead(lead.id).next_followup == week_ahead and "برگزار شد" in sara.last

    sara.say("/meetings").click("رضایی").click("انجام نشد").say("-").click("بدون پیگیری")  # the remaining upcoming meeting
    assert db.get_meeting(later).status == "no_show" and db.get_meeting(later).notes == ""

    sara.press(f"mg:{held}")  # finished meetings stay viewable, but offer no more actions
    assert sara.labels() == ["👤 مشتری"] and "خوب پیش رفت" in sara.last and "برگزار شد" in sara.last
    sara.press(f"mo:{held}:no_show")  # a finished meeting cannot be re-decided
    assert db.get_meeting(held).status == "done"
    sara.press(f"lh:{lead.id}:m")
    assert "برگزار شد" in sara.last and "انجام نشد" in sara.last


def test_only_the_owner_or_manager_can_record_an_outcome(tmp_path):
    db, cfg, engine, m = make_bot(tmp_path, ceo_ids=frozenset({("bale", "900")}))
    sara, ali, ceo = Chat(engine, m, "1"), Chat(engine, m, "2", "علی"), Chat(engine, m, "900", "مدیر")
    for chat in (sara, ali, ceo):
        chat.say("/start")
    _new(sara)
    lead, owner = db.search_leads()[0][0], db.users()[0]
    meeting = db.add_meeting(lead.id, utcnow() - timedelta(hours=1), owner.id)
    ali.press(f"mo:{meeting}:done")
    ali.say("-")
    assert db.get_meeting(meeting).status == "scheduled"  # refused; "-" was just ordinary text
    ceo.press(f"mo:{meeting}:no_show")
    ceo.say("غیبت مشتری").click("بدون پیگیری")
    assert db.get_meeting(meeting).status == "no_show" and db.get_meeting(meeting).notes == "غیبت مشتری"


def test_task_creation_with_priority_and_the_list_views(tmp_path):
    db, cfg, sara = _setup(tmp_path)
    user = db.users()[0]
    sara.say("/task").say("ارسال قرارداد").click("امروز").click("بالا")
    sara.say("➕ کار جدید").say("تماس پیگیری").click("امروز").click("متوسط")
    sara.say("/task").say("کار فردا").click("فردا").click("پایین")
    tasks = {t.title: t for t in db.list_tasks(user.id, "pending", utcnow().date() + timedelta(days=9))}
    assert [(t.priority, t.due_date) for t in (tasks["ارسال قرارداد"], tasks["تماس پیگیری"])] == [("high", tasks["ارسال قرارداد"].due_date)] + [("medium", tasks["تماس پیگیری"].due_date)]
    assert tasks["کار فردا"].priority == "low" and tasks["کار فردا"].due_date == _tomorrow(cfg)

    sara.say("📋 کارهای من")
    assert "🔴 ارسال قرارداد" in sara.last and "🟡 تماس پیگیری" in sara.last and "کار فردا" not in sara.last
    old = db.add_task(user.id, "عقب‌افتاده", _tomorrow(cfg) - timedelta(days=4), "high")
    sara.click("عقب‌افتاده")  # the overdue shortcut
    assert "(۱)" in sara.last and "عقب‌افتاده" in sara.last
    sara.click("عقب‌افتاده").click("انجام شد")  # open the card, complete from the card
    assert db.get_task(old.id).done and "انجام شد ✅" in sara.last
    sara.press("tl:done")
    assert "(۱)" in sara.last and any("عقب‌افتاده" in label for label in sara.labels())  # the task is listed as a button
    sara.click("عقب‌افتاده").click("بازگشایی")
    assert not db.get_task(old.id).done


def test_task_edit_delete_and_ownership(tmp_path):
    db, cfg, engine, m = make_bot(tmp_path)
    sara, ali = Chat(engine, m, "1"), Chat(engine, m, "2", "علی")
    sara.say("/start")
    ali.say("/start")
    owner = db.users()[1] if db.users()[0].name == "علی" else db.users()[0]
    task = db.add_task(owner.id, "اصلی", _tomorrow(cfg), "medium")

    sara.press(f"tv:{task.id}").click("ویرایش").click("عنوان").say("عنوان جدید")
    assert db.get_task(task.id).title == "عنوان جدید"
    sara.click("ویرایش").click("موعد").say("نامعتبر")
    assert "نفهمیدم" in sara.last and db.get_task(task.id).due_date == _tomorrow(cfg)  # refused, still waiting for a valid date
    target = _tomorrow(cfg) + timedelta(days=20)
    sara.say(fa_digits(_jalali(target)))
    assert db.get_task(task.id).due_date == target
    sara.click("ویرایش").click("اولویت").click("پایین")
    assert db.get_task(task.id).priority == "low" and "🟢" in sara.last

    for forged in (f"tc:{task.id}", f"tz:{task.id}", f"tp:{task.id}:high", f"tv:{task.id}"):
        ali.press(forged)  # a colleague cannot touch or even open it
    assert db.get_task(task.id).priority == "low" and not db.get_task(task.id).done
    sara.click("حذف").click("بله")
    assert db.get_task(task.id) is None and "حذف شد" in sara.last
