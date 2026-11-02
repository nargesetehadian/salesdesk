"""Meetings: book, reschedule, cancel. Day/hour pickers are stateless (target encoded in callbacks)."""

from __future__ import annotations

from datetime import timedelta

from ...domain import Meeting, local_midnight
from . import followup
from ...fa import WEEKDAYS, fa_digits, to_jalali
from .. import texts
from ..engine import Ctx
from ..keyboards import btn, chunk, inline
from ..routes import router

HOURS = range(9, 18)


def _meeting(ctx: Ctx, raw_id: str, *, any_status: bool = False) -> Meeting | None:
    meeting = ctx.db.get_meeting(int(raw_id)) if raw_id.isdigit() else None
    if meeting is None or (meeting.status != "scheduled" and not any_status):
        ctx.answer(texts.NOT_FOUND)
        return None
    return meeting


def _days_keyboard(ctx: Ctx, target: str) -> dict:
    today, buttons = ctx.today(), []
    for offset in range(6):
        day = today + timedelta(days=offset)
        label = ("امروز", "فردا")[offset] if offset < 2 else f"{WEEKDAYS[day.weekday()]} {fa_digits(to_jalali(day.year, day.month, day.day)[2])}"
        buttons.append(btn(label, f"md:{target}:{offset}"))
    return inline(chunk(buttons, 2))


# book ------------------------------------------------------------------------
@router.command("meeting")
def meeting_cmd(ctx: Ctx, arg: str = "") -> None:
    leads, _ = ctx.db.search_leads(open_only=True, limit=8)
    if not leads:
        return ctx.reply(texts.NO_LEADS)
    ctx.reply(texts.PICK_LEAD, inline([[btn(lead.label, f"lm:{lead.id}")] for lead in leads]))


@router.callback("lm")
def book_pick_day(ctx: Ctx, arg: str) -> None:
    if arg.isdigit() and ctx.db.get_lead(int(arg)):
        return ctx.edit(texts.PICK_DAY, _days_keyboard(ctx, f"n{arg}"))
    ctx.answer(texts.NOT_FOUND)


@router.callback("md")
def pick_hour(ctx: Ctx, arg: str) -> None:
    target, _, offset = arg.partition(":")
    buttons = [btn(fa_digits(f"{h}:00"), f"mh:{target}:{offset}:{h}") for h in HOURS]
    ctx.edit(texts.PICK_HOUR, inline(chunk(buttons, 3)))


@router.callback("mh")
def apply_time(ctx: Ctx, arg: str) -> None:
    target, offset, hour = arg.split(":")
    when = local_midnight(ctx.now(), ctx.cfg.timezone) + timedelta(days=int(offset))
    when = when.replace(hour=int(hour))
    if when <= ctx.now():
        return ctx.answer(texts.PAST)
    kind, ref = target[0], target[1:]
    if kind == "n":
        lead = ctx.db.get_lead(int(ref))
        if lead is None:
            return ctx.answer(texts.NOT_FOUND)
        meeting_id = ctx.db.add_meeting(lead.id, when, ctx.user.id)
        verb = "ثبت شد"
    else:
        meeting = _meeting(ctx, ref)
        if meeting is None:
            return
        if not ctx.can_manage(meeting.owner_id):
            return ctx.answer(texts.NO_ACCESS)
        ctx.db.reschedule_meeting(meeting.id, when, ctx.user.id)
        meeting_id, verb = meeting.id, "به‌روز شد"
    saved = ctx.db.get_meeting(meeting_id)
    ctx.edit(texts.meeting_saved(saved, ctx.cfg.timezone, verb), inline([[btn("📅 مشاهده‌ی قرار", f"mg:{meeting_id}")]]))


# view, reschedule, cancel ------------------------------------------------------
@router.command("meetings")
def list_cmd(ctx: Ctx, arg: str = "") -> None:
    now, owner = ctx.now(), (None if ctx.user.is_ceo else ctx.user.id)
    start = local_midnight(now, ctx.cfg.timezone)
    pending = ctx.db.meetings_pending_outcome(now, owner)
    upcoming = [m for m in ctx.db.meetings_between(start, start + timedelta(days=14), owner) if m.scheduled_at >= now][:10]
    if not pending and not upcoming:
        return ctx.reply("قرار پیش‌رویی ثبت نشده است.")
    rows = [[btn("⏳ " + texts.meeting_label(m, ctx.cfg.timezone), f"mg:{m.id}")] for m in pending]
    rows += [[btn(texts.meeting_label(m, ctx.cfg.timezone), f"mg:{m.id}")] for m in upcoming]
    head = f"📅 قرارها — پیش‌رو: {fa_digits(len(upcoming))}" + (f" · ⏳ منتظر ثبت نتیجه: {fa_digits(len(pending))}" if pending else "")
    ctx.reply(head, inline(rows))


@router.callback("mg")
def card(ctx: Ctx, arg: str) -> None:
    meeting = _meeting(ctx, arg, any_status=True)
    if meeting is None:
        return
    owner = ctx.db.get_user(meeting.owner_id) if meeting.owner_id else None
    rows = [[btn("👤 مشتری", f"ld:{meeting.lead_id}")]]
    if meeting.status == "scheduled" and ctx.can_manage(meeting.owner_id):
        rows.insert(0, [btn("✅ برگزار شد", f"mo:{meeting.id}:done"), btn("🚫 انجام نشد", f"mo:{meeting.id}:no_show")])
        rows.insert(1, [btn("🔁 تغییر زمان", f"mr:{meeting.id}"), btn("❌ لغو", f"mx:{meeting.id}")])
    ctx.edit(texts.meeting_card(meeting, owner.name if owner else "", ctx.cfg.timezone), inline(rows))


# outcome: held / did not take place, with a note and an optional next follow-up ----------
@router.callback("mo")
def outcome_start(ctx: Ctx, arg: str) -> None:
    raw_id, _, status = arg.partition(":")
    meeting = _meeting(ctx, raw_id)
    if meeting is None or status not in ("done", "no_show"):
        return
    if not ctx.can_manage(meeting.owner_id):
        return ctx.answer(texts.NO_ACCESS)
    ctx.goto("meet:note", meeting_id=meeting.id, status=status)
    ctx.reply(f"{texts.MEETING_STATUS[status]} ✔️ ({meeting.lead_name})\n{texts.ASK_MEETING_NOTE}")


@router.state("meet:note")
def outcome_note(ctx: Ctx, text: str) -> None:
    ctx.data["note"] = "" if text == "-" else text
    followup.ask(ctx, "meeting")


@followup.finisher("meeting")
def outcome_save(ctx: Ctx, day) -> None:  # noqa: ANN001
    data = ctx.data
    meeting = ctx.db.get_meeting(data["meeting_id"])
    saved = meeting is not None and ctx.db.complete_meeting(meeting.id, data["status"], data.get("note", ""), ctx.user.id, day)
    ctx.finish()
    if not saved:
        return ctx.reply(texts.NOT_FOUND)
    follow = f"\n🔔 پیگیری بعدی: {texts.format_date(day)}" if day else ""
    ctx.reply(f"📅 قرار با {meeting.lead_name} ثبت شد: {texts.MEETING_STATUS[data['status']]}{follow}", inline([[btn("👤 پرونده‌ی مشتری", f"ld:{meeting.lead_id}")]]))


@router.callback("mr")
def reschedule(ctx: Ctx, arg: str) -> None:
    meeting = _meeting(ctx, arg)
    if meeting and ctx.can_manage(meeting.owner_id):
        return ctx.edit(texts.PICK_DAY, _days_keyboard(ctx, f"r{meeting.id}"))
    ctx.answer(texts.NO_ACCESS)


@router.callback("mx")
def cancel_confirm(ctx: Ctx, arg: str) -> None:
    meeting = _meeting(ctx, arg)
    if meeting and ctx.can_manage(meeting.owner_id):
        rows = [[btn("❌ بله، لغو شود", f"mz:{meeting.id}"), btn("انصراف", f"mg:{meeting.id}")]]
        return ctx.edit(f"قرار با {meeting.lead_name} لغو شود؟", inline(rows))
    ctx.answer(texts.NO_ACCESS)


@router.callback("mz")
def cancel_do(ctx: Ctx, arg: str) -> None:
    meeting = _meeting(ctx, arg)
    if meeting and ctx.can_manage(meeting.owner_id):
        ctx.db.cancel_meeting(meeting.id, ctx.user.id)
        return ctx.edit(f"❌ قرار با {meeting.lead_name} لغو شد.")
    ctx.answer(texts.NO_ACCESS)
