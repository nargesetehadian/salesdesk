from datetime import timedelta

from harness import Chat, make_bot

from salesdesk.bot.engine import Engine, Router
from salesdesk.db import Database
from salesdesk.domain import utcnow
from salesdesk.fa import fa_digits


def _create_lead(chat: Chat, name="رضایی", company="نوین‌تک", phone="۰۹۱۲۱۲۳۴۵۶۷") -> None:
    chat.say("👤 مشتری جدید").say(name).say(company).say(phone).click("رد کردن")  # skip the acquisition source


def test_employee_creates_edits_and_deletes_lead(tmp_path):
    db, _, engine, m = make_bot(tmp_path)
    sara = Chat(engine, m, "1")
    sara.say("/start")
    _create_lead(sara)
    assert "رضایی" in sara.last and "نوین‌تک" in sara.last
    lead = db.search_leads()[0][0]
    assert lead.phone == "09121234567"  # Persian digits normalised

    sara.click("ویرایش").click("شماره").say("0935 000 0000")
    assert db.get_lead(lead.id).phone == "09350000000"

    sara.click("حذف").click("بله")
    assert db.get_lead(lead.id) is None and db.search_leads()[1] == 0


def test_permissions_employee_cannot_touch_colleagues_leads_but_ceo_can(tmp_path):
    db, _, engine, m = make_bot(tmp_path, ceo_ids=frozenset({("bale", "900")}))
    sara, ali, ceo = Chat(engine, m, "1"), Chat(engine, m, "2", "علی"), Chat(engine, m, "900", "مدیر")
    _create_lead(sara)
    lead_id = db.search_leads()[0][0].id

    ali.say("/leads").click("رضایی")
    assert not any("حذف" in label for label in ali.labels())  # no manage buttons for a colleague
    ali.press(f"ly:{lead_id}")  # forged callback must still be refused
    assert db.get_lead(lead_id) is not None

    ceo.say("/leads").click("رضایی").click("حذف").click("بله")
    assert db.get_lead(lead_id) is None


def test_search_and_stage_filter(tmp_path):
    db, _, engine, m = make_bot(tmp_path)
    sara = Chat(engine, m, "1")
    sara.say("/start")
    uid = db.users()[0].id
    a = db.add_lead("رضایی", "نوین", "09121111111", uid)
    db.add_lead("احمدی", "پارس", "09352222222", uid)
    db.set_stage(a.id, "won", uid)

    sara.say("/search ۰۹۱۲")  # Persian digits match the ASCII phone
    assert "رضایی" in "".join(sara.labels()) and "احمدی" not in "".join(sara.labels())
    sara.click("پاک کردن")
    assert len([x for x in sara.labels() if "احمدی" in x or "رضایی" in x]) == 2
    sara.click("فروش")
    assert "رضایی" in "".join(sara.labels()) and "احمدی" not in "".join(sara.labels())


def test_meeting_book_reschedule_cancel(tmp_path):
    db, cfg, engine, m = make_bot(tmp_path)
    sara = Chat(engine, m, "1")
    sara.say("/start")
    _create_lead(sara)
    lead = db.search_leads()[0][0]

    sara.say("/meeting").click("رضایی").click("فردا").click(fa_digits("10:00"))
    meeting = db.lead_meetings(lead.id)[0]
    tomorrow = (utcnow().astimezone(cfg.timezone) + timedelta(days=1)).date()
    assert meeting.scheduled_at.astimezone(cfg.timezone).date() == tomorrow
    assert meeting.scheduled_at.astimezone(cfg.timezone).hour == 10
    assert db.get_lead(lead.id).status == "meeting"

    sara.say("/meetings").click("رضایی").click("تغییر زمان").click("فردا").click(fa_digits("11:00"))
    assert db.get_meeting(meeting.id).scheduled_at.astimezone(cfg.timezone).hour == 11

    sara.say("/meetings").click("رضایی").click("لغو").click("بله")
    assert db.get_meeting(meeting.id).status == "cancelled"
    assert db.lead_meetings(lead.id) == []


def test_past_time_is_rejected(tmp_path):
    db, cfg, engine, m = make_bot(tmp_path)
    sara = Chat(engine, m, "1")
    sara.say("/start")
    _create_lead(sara)
    lead_id = db.search_leads()[0][0].id
    yesterday_hour = f"mh:n{lead_id}:-1:10"
    sara.press(yesterday_hour)
    assert db.lead_meetings(lead_id) == []


def test_tasks_with_buttons_and_typed_jalali_date(tmp_path):
    db, _, engine, m = make_bot(tmp_path)
    sara = Chat(engine, m, "1")
    sara.say("/start").say("➕ کار جدید").say("ارسال پیشنهاد").click("امروز").click("بالا")
    sara.say("/task").say("تماس پیگیری").say("۱۴۰۹/۰۱/۱۵").click("پایین")  # far future, typed in Jalali
    user = db.users()[0]
    assert db.count_upcoming(user.id, utcnow().date()) == 1

    sara.say("📋 کارهای من")
    assert "ارسال پیشنهاد" in sara.last
    sara.click("ارسال پیشنهاد")
    assert db.open_tasks(user.id, utcnow().date() + timedelta(days=1)) == []
    sara.say("/task").say("x").say("not a date")
    assert "نفهمیدم" in sara.last


def test_invite_code_gate_and_ceo_bypass(tmp_path):
    db, _, engine, m = make_bot(tmp_path, team_code="s3cret", ceo_ids=frozenset({("*", "900")}))
    stranger, ceo = Chat(engine, m, "5"), Chat(engine, m, "900", "مدیر")
    stranger.say("/start")
    assert "کد دعوت" in stranger.last
    stranger.say("📋 کارهای من")
    assert "کد دعوت" in stranger.last or "درست نیست" in stranger.last
    assert not db.users()[0].active
    stranger.say("s3cret")
    assert "سلام" in stranger.last and db.get_user(db.users()[0].id).active
    ceo.say("/start")
    assert "سلام" in ceo.last and "داشبورد" in ceo.last


def test_export_is_scoped_to_the_employee(tmp_path):
    db, _, engine, m = make_bot(tmp_path, ceo_ids=frozenset({("bale", "900")}))
    sara, ali, ceo = Chat(engine, m, "1"), Chat(engine, m, "2", "علی"), Chat(engine, m, "900", "مدیر")
    _create_lead(sara, "رضایی")
    _create_lead(ali, "کریمی")
    sara.say("📤 خروجی").click("CSV")
    _, filename, data = m.documents[-1]
    text = data.decode("utf-8-sig")
    assert filename.endswith(".csv") and "رضایی" in text and "کریمی" not in text
    ceo.say("/export").click("xlsx")
    assert m.documents[-1][1].endswith(".xlsx")


def test_commands_abandon_flow_and_errors_are_contained(tmp_path):
    db, settings, _, m = make_bot(tmp_path)
    router = Router()
    router.command("start")(lambda ctx, arg="": ctx.reply("hi"))

    def boom(ctx, arg=""):
        ctx.goto("stuck")
        raise RuntimeError("bug")

    router.command("boom")(boom)
    engine = Engine(Database(settings.db_path), settings, router)
    user = Chat(engine, m, "1")
    user.say("/boom")  # must not raise
    assert "مشکلی" in user.last
    assert engine.db.get_session("bale", "1")[0] == ""  # a crashed flow never leaves the chat stuck


def test_id_command_works_before_the_invite_gate(tmp_path):
    _, _, engine, m = make_bot(tmp_path, team_code="s3cret")
    stranger = Chat(engine, m, "42")
    stranger.say("/id")
    assert "bale:42" in stranger.last
