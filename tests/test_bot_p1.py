"""P1 conversations: profile, source, contact outcomes, follow-up, history and notes."""

from datetime import timedelta

from harness import Chat, make_bot

from salesdesk.domain import utcnow
from salesdesk.fa import fa_digits, to_jalali


def _setup(tmp_path, **kw):
    db, cfg, engine, m = make_bot(tmp_path, **kw)
    sara = Chat(engine, m, "1")
    sara.say("/start")
    return db, cfg, sara


def _new(chat, source="رد کردن", name="رضایی"):
    chat.say("👤 مشتری جدید").say(name).say("نوین‌تک").say("۰۹۱۲۱۲۳۴۵۶۷").click(source)


def _tomorrow(cfg):
    return utcnow().astimezone(cfg.timezone).date() + timedelta(days=1)


def _jalali(day):
    return "/".join(f"{p:02d}" if i else str(p) for i, p in enumerate(to_jalali(day.year, day.month, day.day)))


def test_new_customer_flow_records_the_source_and_shows_a_full_profile(tmp_path):
    db, cfg, sara = _setup(tmp_path)
    _new(sara, "وب‌سایت")
    lead = db.search_leads()[0][0]
    assert lead.source == "website" and lead.owner_name == "سارا محمدی"
    card = sara.last
    for expected in ("رضایی", "نوین‌تک", "09121234567", "منبع: وب‌سایت", "مسئول: سارا محمدی", "پیگیری بعدی: —", "آخرین فعالیت: امروز", "۰ تماس"):
        assert expected in card, expected
    assert "ثبت مشتری" in card  # the stage history already has its first entry
    assert {"📞 ثبت تماس", "📜 تاریخچه", "📝 یادداشت‌ها", "✏️ ویرایش"} <= set(sara.labels())


def test_source_is_optional_and_editable_from_the_profile(tmp_path):
    db, _, sara = _setup(tmp_path)
    _new(sara)  # skipped
    lead = db.search_leads()[0][0]
    assert lead.source == "" and "منبع: —" in sara.last
    sara.click("ویرایش").click("منبع").click("نمایشگاه")
    assert db.get_lead(lead.id).source == "exhibition" and "منبع: نمایشگاه" in sara.last


def test_contact_flow_stores_outcome_note_duration_and_followup_and_never_overwrites(tmp_path):
    db, cfg, sara = _setup(tmp_path)
    _new(sara)
    lead = db.search_leads()[0][0]
    sara.say("📞 ثبت تماس").click("رضایی").click("علاقه‌مند").say("خواهان پیشنهاد قیمت است")
    assert "مدت تماس" in sara.last
    sara.click("۵ دقیقه").click("فردا")
    assert "ثبت شد" in sara.last and "نتیجه: علاقه‌مند" in sara.last
    first = db.lead_contacts(lead.id)[0]
    assert (first.outcome, first.note, first.duration_sec, first.next_followup) == ("interested", "خواهان پیشنهاد قیمت است", 300, _tomorrow(cfg))
    assert db.get_lead(lead.id).status == "contacted" and db.get_lead(lead.id).next_followup == _tomorrow(cfg)

    sara.say("/call").click("رضایی").click("پاسخ نداد").say("-").click("بدون پیگیری")
    contacts = db.lead_contacts(lead.id)
    assert [c.outcome for c in contacts] == ["no_answer", "interested"] and contacts[1].note == "خواهان پیشنهاد قیمت است"  # earlier record untouched
    assert db.get_lead(lead.id).next_followup == _tomorrow(cfg)  # an unanswered attempt keeps the promise


def test_unreached_customer_stays_new_and_skips_the_duration_step(tmp_path):
    db, _, sara = _setup(tmp_path)
    _new(sara)
    lead = db.search_leads()[0][0]
    sara.say("/call").click("رضایی").click("شماره اشتباه").say("-")
    assert "مدت تماس" not in sara.last and "پیگیری بعدی" in sara.last  # straight to the follow-up question
    sara.click("بدون پیگیری")
    assert db.get_lead(lead.id).status == "new" and db.lead_contacts(lead.id)[0].outcome == "wrong_number"


def test_typed_followup_date_and_typed_duration_are_validated(tmp_path):
    db, cfg, sara = _setup(tmp_path)
    _new(sara)
    lead = db.search_leads()[0][0]
    sara.say("/call").click("رضایی").click("درخواست تماس مجدد").say("بعداً زنگ بزن")
    sara.say("abc")
    assert "فقط تعداد دقیقه" in sara.last
    sara.say("۷")  # typed minutes instead of a button
    sara.click("تاریخ دلخواه")
    sara.say("1300/01/01")
    assert "نفهمیدم" in sara.last and db.lead_contacts(lead.id) == []  # past date refused, nothing saved yet
    target = utcnow().astimezone(cfg.timezone).date() + timedelta(days=10)
    sara.say(fa_digits(_jalali(target)))
    saved = db.lead_contacts(lead.id)[0]
    assert (saved.outcome, saved.duration_sec, saved.next_followup) == ("callback", 420, target)


def test_cancel_abandons_a_contact_and_stale_buttons_do_nothing(tmp_path):
    db, _, sara = _setup(tmp_path)
    _new(sara)
    lead = db.search_leads()[0][0]
    sara.say("/call").click("رضایی").click("پاسخ داد").say("-").click("۵ دقیقه")
    assert "پیگیری بعدی" in sara.last
    stale_followup = sara.mine[-1][3]["inline_keyboard"][1][0]["callback_data"]
    sara.say("/cancel")
    sara.press(stale_followup)  # the old follow-up button, pressed after cancelling
    assert db.lead_contacts(lead.id) == []
    sara.press(f"co:{lead.id}:answered")  # valid button, then deleting the customer mid-flow
    sara.say("-")
    db.delete_lead(lead.id, 1)
    sara.press(stale_followup)
    assert db.lead_contacts(lead.id) == []  # no crash, nothing recorded


def test_history_pages_show_contacts_meetings_notes_and_stage_changes_in_order(tmp_path):
    db, cfg, sara = _setup(tmp_path)
    _new(sara, "معرفی")
    lead = db.search_leads()[0][0]
    sara.say("/call").click("رضایی").click("علاقه‌مند").say("توضیح تماس").click("۱۰ دقیقه").click("۳ روز")
    sara.say("/meeting").click("رضایی").click("فردا").click(fa_digits("10:00"))
    sara.say("/stage").click("رضایی").click("پیشنهاد")
    sara.press(f"na:{lead.id}")
    sara.say("مشتری قیمت رقیب را دارد")
    sara.press(f"lh:{lead.id}:t")
    page = sara.last
    assert page.index("ثبت مشتری") > page.index("تماس (علاقه‌مند)") > page.index("از «جدید» به «تماس»")  # newest first, creation last
    assert page.index("از «قرار» به «پیشنهاد»") < page.index("از «تماس» به «قرار»") < page.index("از «جدید» به «تماس»")
    assert page.index("مشتری قیمت رقیب را دارد") < page.index("از «قرار» به «پیشنهاد»")  # the note was added last
    for expected in ("توضیح تماس", "پیگیری", "قرار", "برنامه‌ریزی‌شده", "از «جدید» به «تماس»", "از «تماس» به «قرار»", "سارا محمدی"):
        assert expected in page, expected
    sara.press(f"lh:{lead.id}:c")
    assert "نتیجه: علاقه‌مند · مدت: ۱۰ دقیقه" in sara.last and "توضیح: توضیح تماس" in sara.last and "🔔 پیگیری:" in sara.last
    sara.press(f"lh:{lead.id}:m")
    assert "برنامه‌ریزی‌شده" in sara.last


def test_notes_can_be_added_edited_and_deleted_from_the_chat(tmp_path):
    db, _, sara = _setup(tmp_path)
    _new(sara)
    lead = db.search_leads()[0][0]
    sara.click("یادداشت‌ها")
    assert "هنوز یادداشتی" in sara.last
    sara.click("یادداشت جدید").say("اولین یادداشت")
    assert "(۱)" in sara.last and [n.body for n in db.lead_notes(lead.id)] == ["اولین یادداشت"]
    sara.click("اولین یادداشت").click("ویرایش").say("یادداشت اصلاح‌شده")
    assert [n.body for n in db.lead_notes(lead.id)] == ["یادداشت اصلاح‌شده"] and db.lead_notes(lead.id)[0].updated_at
    sara.click("اصلاح‌شده")
    assert "ویرایش" in sara.last and "سارا محمدی" in sara.last
    sara.click("حذف").click("بله")
    assert db.lead_notes(lead.id) == [] and "هنوز یادداشتی" in sara.last
    sara.press(f"lh:{lead.id}:t")
    assert "یادداشت اصلاح‌شده" not in sara.last  # deleted notes leave the timeline too
