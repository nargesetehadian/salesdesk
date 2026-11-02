"""All user-facing copy (plain text, so it renders identically on every platform)."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from ..crm import CustomerProfile
from ..domain import Contact, Lead, Meeting, Note, Task, TimelineItem, parse_dt
from ..fa import fa_digits, format_date, format_datetime, format_stamp, format_time

STAGE = {
    "new": "🆕 جدید", "contacted": "📞 تماس گرفته‌شده", "meeting": "📅 قرار ملاقات",
    "proposal": "📄 پیشنهاد", "won": "✅ فروش موفق", "lost": "❌ از دست رفته",
}  # fmt: skip
STAGE_SHORT = {"new": "جدید", "contacted": "تماس", "meeting": "قرار", "proposal": "پیشنهاد", "won": "فروش", "lost": "ازدست‌رفته"}

MENU = {
    "👥 مشتریان": "leads", "🔎 جستجو": "search", "👤 مشتری جدید": "new", "📞 ثبت تماس": "call",
    "📅 قرارها": "meetings", "➕ کار جدید": "task", "📋 کارهای من": "tasks", "📤 خروجی": "export",
    "📊 داشبورد": "dashboard",
}  # fmt: skip
COMMANDS = [
    ("leads", "فهرست مشتریان"), ("search", "جستجوی مشتری"), ("new", "ثبت مشتری جدید"), ("call", "ثبت تماس"),
    ("meetings", "قرارها"), ("meeting", "تعیین قرار"), ("tasks", "کارهای امروز"), ("task", "افزودن کار"),
    ("stage", "تغییر مرحله"), ("today", "قرارهای امروز"), ("stale", "پیگیری‌نشده‌ها"), ("export", "خروجی اکسل"),
]  # fmt: skip

ASK_CODE = "🔒 این ربات مخصوص تیم است. کد دعوت را بفرست."
BAD_CODE = "کد درست نیست. دوباره امتحان کن."
CANCELLED = "انصراف داده شد."
HINT = "برای شروع از منو استفاده کن یا /start بزن."
ERROR = "مشکلی پیش آمد. دوباره تلاش کن. اگر تکرار شد، به مدیر سیستم خبر بده."
NO_ACCESS = "دسترسی این کار را نداری. فقط مسئول مشتری یا مدیرعامل می‌تواند."
NOT_FOUND = "این مورد پیدا نشد (شاید حذف شده)."
PAST = "این زمان گذشته است."
ASK_NAME = "نام مشتری چیست؟"
ASK_COMPANY = "نام شرکت؟ (برای رد کردن، - بفرست)"
ASK_PHONE = "شماره تماس؟ (برای رد کردن، - بفرست)"
BAD_PHONE = "شماره معتبر نیست. فقط رقم بفرست، مثل ۰۹۱۲۱۲۳۴۵۶۷"
ASK_NOTE = "نتیجه‌ی تماس را در یک پیام کوتاه بنویس:"
ASK_QUERY = "نام، شرکت یا شماره‌ی مشتری را بنویس:"
ASK_TASK = "عنوان کار چیست؟"
ASK_TASK_DATE = "برای چه روزی؟ یکی را انتخاب کن یا تاریخ شمسی را بنویس، مثل ۱۴۰۵/۰۸/۱۰"
BAD_DATE = "تاریخ را نفهمیدم. این‌طور بنویس: ۱۴۰۵/۰۸/۱۰"
NO_LEADS = "مشتری فعالی پیدا نشد. با «👤 مشتری جدید» شروع کن."
PICK_LEAD = "کدام مشتری؟ (مشتری دیگر را با 🔎 جستجو پیدا کن)"
PICK_DAY = "چه روزی؟"
PICK_HOUR = "ساعت چند؟"
PICK_STAGE = "مرحله‌ی جدید؟"
PICK_FIELD = "کدام بخش را ویرایش کنیم؟"
FIELD = {"name": "نام", "company": "شرکت", "phone": "شماره", "source": "منبع"}
EXPORT_PICK = "خروجی در چه قالبی باشد؟"
NO_EXPORT = "داده‌ای برای خروجی وجود ندارد."


def welcome(name: str, is_ceo: bool) -> str:
    extra = "\n📊 داشبورد مدیریتی را از منو یا /dashboard باز کن." if is_ceo else ""
    return f"سلام {name} 👋\nمن دستیار ثبت فروش هستم؛ از منوی پایین استفاده کن.{extra}"


def lead_card(lead: Lead, now: datetime) -> str:
    days = (now - lead.last_contact_at).days
    last = "امروز" if days <= 0 else f"{fa_digits(days)} روز پیش"
    return (
        f"👤 {lead.name}\n🏢 {lead.company or '—'}\n📱 {lead.phone or '—'}\n📍 {STAGE[lead.status]}\n"
        f"🧑‍💼 مسئول: {lead.owner_name or '—'}\n🕒 آخرین تماس: {last}"
    )


def lead_list_title(total: int, status: str, query: str, mine: bool) -> str:
    parts = [f"👥 {fa_digits(total)} مشتری"]
    if status:
        parts.append(f"مرحله: {STAGE_SHORT[status]}")
    if query:
        parts.append(f"«{query}»")
    if mine:
        parts.append("فقط مال من")
    return " — ".join(parts)


def meeting_label(m: Meeting, tz: ZoneInfo) -> str:
    return f"📅 {format_datetime(m.scheduled_at, tz)} — {m.lead_name}"


def meeting_card(m: Meeting, owner: str, tz: ZoneInfo) -> str:
    company = f"\n🏢 {m.company}" if m.company else ""
    notes = f"\n📝 {m.notes}" if m.notes else ""
    return f"📅 قرار ملاقات\n👤 {m.lead_name}{company}\n🕒 {format_datetime(m.scheduled_at, tz)}\n🧑‍💼 مسئول: {owner or '—'}\n📌 وضعیت: {MEETING_STATUS[m.status]}{notes}"


def meeting_saved(m: Meeting, tz: ZoneInfo, verb: str = "ثبت شد") -> str:
    return f"✅ قرار با {m.lead_name} {verb}:\n{format_datetime(m.scheduled_at, tz)}\n⏰ یادآوری خودکار برایت می‌فرستم."


def reminder(m: Meeting, minutes: int, tz: ZoneInfo) -> str:
    company = f" ({m.company})" if m.company else ""
    return f"⏰ یادآوری قرار\n{m.lead_name}{company}\nساعت {format_time(m.scheduled_at, tz)} — حدود {fa_digits(minutes)} دقیقه دیگر"


def today(meetings: list[Meeting], tz: ZoneInfo) -> str:
    if not meetings:
        return "امروز قراری نداری ☕️"
    lines = [f"• {format_time(m.scheduled_at, tz)}  {m.lead_name}" + (f" ({m.company})" if m.company else "") for m in meetings]
    return f"📅 قرارهای امروز ({fa_digits(len(meetings))})\n\n" + "\n".join(lines)


def stale(leads: list[Lead], days: int, now: datetime) -> str:
    if not leads:
        return f"همه‌ی مشتری‌ها در {fa_digits(days)} روز اخیر پیگیری شده‌اند 👌"
    lines = [f"• {lead.label} — {fa_digits((now - lead.last_contact_at).days)} روز" for lead in leads[:15]]
    more = f"\n… و {fa_digits(len(leads) - 15)} مورد دیگر" if len(leads) > 15 else ""
    return f"⏰ {fa_digits(len(leads))} مشتری بیش از {fa_digits(days)} روز بدون پیگیری\n\n" + "\n".join(lines) + more


def task_saved(task: Task) -> str:
    return f"📝 کار ثبت شد: {task.title}\nموعد: {format_date(task.due_date)}"


def tasks(due_today: list[Task], overdue: list[Task], upcoming: int, today_: date) -> str:
    lines = [f"📋 کارهای امروز — {format_date(today_)}", ""]
    lines += [f"• {PRIORITY_DOT[t.priority]} {t.title}" for t in due_today] or ["کاری برای امروز نداری 🎉"]
    if overdue:
        lines += ["", f"⚠️ عقب‌افتاده ({fa_digits(len(overdue))})"]
        lines += [f"• {PRIORITY_DOT[t.priority]} {t.title} — {fa_digits((today_ - t.due_date).days)} روز" for t in overdue]
    if upcoming:
        lines += ["", f"🗓 در روزهای آینده {fa_digits(upcoming)} کار داری."]
    return "\n".join(lines)


def ceo_digest(meetings: int, stale_n: int, overdue: int) -> str:
    return (
        f"☀️ صبح بخیر\n\n📅 قرار امروز تیم: {fa_digits(meetings)}\n"
        f"⏰ مشتری بدون پیگیری: {fa_digits(stale_n)}\n⚠️ کار عقب‌افتاده‌ی تیم: {fa_digits(overdue)}"
    )


# --- P1: profile, history, contacts, notes, meeting outcomes, task priorities --------------
OUTCOME = {
    "answered": "پاسخ داد", "no_answer": "پاسخ نداد", "wrong_number": "شماره اشتباه",
    "callback": "درخواست تماس مجدد", "interested": "علاقه‌مند", "not_interested": "عدم علاقه",
}  # fmt: skip
SOURCE = {
    "referral": "معرفی", "website": "وب‌سایت", "inbound_call": "تماس ورودی",
    "exhibition": "نمایشگاه", "social": "شبکه‌های اجتماعی", "other": "سایر",
}  # fmt: skip
MEETING_STATUS = {"scheduled": "برنامه‌ریزی‌شده", "done": "برگزار شد", "cancelled": "لغو شد", "no_show": "انجام نشد"}
PRIORITY = {"high": "🔴 بالا", "medium": "🟡 متوسط", "low": "🟢 پایین"}
PRIORITY_DOT = {"high": "🔴", "medium": "🟡", "low": "🟢"}
PICK_OUTCOME = "نتیجه‌ی تماس چه بود؟"
PICK_SOURCE = "مشتری از کجا با ما آشنا شد؟"
ASK_NOTE_OPT = "توضیحات را بنویس (برای رد کردن، - بفرست):"
ASK_DURATION = "مدت تماس؟ (دکمه بزن یا تعداد دقیقه را بنویس)"
BAD_DURATION = "فقط تعداد دقیقه را بنویس، مثل ۵"
ASK_FOLLOWUP = "پیگیری بعدی چه زمانی باشد؟"
ASK_FOLLOWUP_DATE = "تاریخ شمسی پیگیری را بنویس، مثل ۱۴۰۵/۰۸/۱۰"
BAD_FOLLOWUP = "تاریخ را نفهمیدم یا گذشته است. این‌طور بنویس: ۱۴۰۵/۰۸/۱۰"
ASK_NOTE_BODY = "متن یادداشت را بنویس:"
ASK_MEETING_NOTE = "نتیجه‌ی جلسه را بنویس (برای رد کردن، - بفرست):"
PICK_PRIORITY = "اولویت کار؟"
NO_NOTES = "هنوز یادداشتی ثبت نشده است."
FOLLOWUP_BUTTONS = [("فردا", "1"), ("۳ روز دیگر", "3"), ("هفته‌ی بعد", "7")]


def stamp(dt: datetime, tz: ZoneInfo) -> str:
    return fa_digits(format_stamp(dt, tz))


def _short(text: str, n: int = 70) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def timeline_line(item: TimelineItem, tz: ZoneInfo) -> str:
    who = f" — {item.user_name}" if item.user_name else ""
    if item.kind == "contact":
        follow = f" · پیگیری {format_date(date.fromisoformat(item.c))}" if item.c else ""
        body = f"📞 تماس ({OUTCOME[item.a]})" + (f": {_short(item.b)}" if item.b else "") + follow
    elif item.kind == "meeting":
        body = f"📅 قرار {stamp(parse_dt(item.c), tz)} — {MEETING_STATUS[item.a]}" + (f": {_short(item.b)}" if item.b else "")
    elif item.kind == "note":
        body = f"📝 {_short(item.b)}"
    else:  # stage
        body = "🆕 ثبت مشتری" if not item.a else f"🔄 از «{STAGE_SHORT[item.a]}» به «{STAGE_SHORT[item.b]}»"
    return f"{stamp(item.at, tz)}  {body}{who}"


def profile_card(profile: CustomerProfile, now: datetime, tz: ZoneInfo) -> str:
    lead, counts = profile.lead, profile.counts
    idle = (now - profile.last_activity).days
    follow = "—"
    if lead.next_followup:
        late = lead.next_followup < now.astimezone(tz).date()
        follow = format_date(lead.next_followup) + (" ⚠️ گذشته" if late else "")
    lines = [
        f"👤 {lead.name}", f"🏢 {lead.company or '—'}", f"📱 {lead.phone or '—'}", f"📍 {STAGE[lead.status]}",
        f"🎯 منبع: {SOURCE.get(lead.source, '—')}", f"🧑‍💼 مسئول: {lead.owner_name or '—'}", f"🔔 پیگیری بعدی: {follow}",
        f"🕒 آخرین فعالیت: {'امروز' if idle <= 0 else fa_digits(idle) + ' روز پیش'}",
        f"📊 {fa_digits(counts['contacts'])} تماس · {fa_digits(counts['meetings'])} قرار · {fa_digits(counts['notes'])} یادداشت",
    ]  # fmt: skip
    if profile.recent_activity:
        lines += ["", "🧾 آخرین فعالیت‌ها:"] + [timeline_line(i, tz) for i in profile.recent_activity[:3]]
    return "\n".join(lines)


def timeline_page(lead: Lead, items: list[TimelineItem], tz: ZoneInfo) -> str:
    body = "\n\n".join(timeline_line(i, tz) for i in items) or "فعالیتی ثبت نشده است."
    return f"📜 تاریخچه‌ی {lead.name} ({fa_digits(len(items))} مورد، جدیدترین اول)\n\n{body}"


def contacts_page(lead: Lead, contacts: list[Contact], tz: ZoneInfo) -> str:
    if not contacts:
        return f"📞 هنوز تماسی با {lead.name} ثبت نشده است."
    blocks = []
    for c in contacts:
        lines = [f"▫️ {stamp(c.created_at, tz)}" + (f" — {c.owner_name}" if c.owner_name else "")]
        detail = f"نتیجه: {OUTCOME[c.outcome]}" + (f" · مدت: {fa_digits(round(c.duration_sec / 60))} دقیقه" if c.duration_sec else "")
        lines.append(f"   {detail}")
        if c.note:
            lines.append(f"   توضیح: {_short(c.note, 120)}")
        if c.next_followup:
            lines.append(f"   🔔 پیگیری: {format_date(c.next_followup)}")
        blocks.append("\n".join(lines))
    return f"📞 تماس‌های {lead.name} ({fa_digits(len(contacts))})\n\n" + "\n\n".join(blocks)


def meetings_page(lead: Lead, meetings: list[Meeting], tz: ZoneInfo) -> str:
    if not meetings:
        return f"📅 هنوز قراری با {lead.name} ثبت نشده است."
    blocks = [
        f"▫️ {stamp(m.scheduled_at, tz)} — {MEETING_STATUS[m.status]}" + (f"\n   توضیح: {_short(m.notes, 120)}" if m.notes else "")
        for m in meetings
    ]
    return f"📅 قرارهای {lead.name} ({fa_digits(len(meetings))})\n\n" + "\n\n".join(blocks)


def note_card(note: Note, tz: ZoneInfo) -> str:
    edited = f" · ویرایش {stamp(note.updated_at, tz)}" if note.updated_at else ""
    return f"📝 یادداشت\n\n{note.body}\n\n🧑‍💼 {note.author_name or '—'} · {stamp(note.created_at, tz)}{edited}"


def contact_saved(lead: Lead, outcome: str, follow: date | None) -> str:
    extra = f"\n🔔 پیگیری بعدی: {format_date(follow)}" if follow else ""
    return f"📞 تماس با {lead.name} ثبت شد.\nنتیجه: {OUTCOME[outcome]}{extra}"


def task_line(t: Task) -> str:
    mark = "✅" if t.done else PRIORITY_DOT[t.priority]
    return f"{mark} {t.title[:40]} — {format_date(t.due_date)}"


def task_card(t: Task, today_: date) -> str:
    late = " ⚠️ عقب‌افتاده" if not t.done and t.due_date < today_ else ""
    return f"📋 {t.title}\n\n⏰ موعد: {format_date(t.due_date)}{late}\n🎚 اولویت: {PRIORITY[t.priority]}\n📌 وضعیت: {'انجام شد ✅' if t.done else 'باز'}"
