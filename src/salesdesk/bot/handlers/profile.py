"""Customer profile pages: full activity history, contact and meeting history, and notes (add / edit / delete)."""

from __future__ import annotations

from .. import texts
from ..engine import Ctx
from ..keyboards import btn, inline
from ..routes import router

HISTORY_LIMIT = 15


def _lead(ctx: Ctx, raw_id: str):  # noqa: ANN202
    lead = ctx.db.get_lead(int(raw_id)) if raw_id.isdigit() else None
    if lead is None:
        ctx.answer(texts.NOT_FOUND)
    return lead


# history ---------------------------------------------------------------------
@router.callback("lh")
def history(ctx: Ctx, arg: str) -> None:
    raw_id, _, kind = arg.partition(":")
    if not (lead := _lead(ctx, raw_id)):
        return
    tz = ctx.cfg.timezone
    if kind == "c":
        text = texts.contacts_page(lead, ctx.db.lead_contacts(lead.id, HISTORY_LIMIT), tz)
    elif kind == "m":
        text = texts.meetings_page(lead, ctx.db.lead_meeting_history(lead.id, HISTORY_LIMIT), tz)
    else:
        text = texts.timeline_page(lead, ctx.db.lead_timeline(lead.id, HISTORY_LIMIT), tz)
    tabs = [btn("📜 همه", f"lh:{lead.id}:t"), btn("📞 تماس‌ها", f"lh:{lead.id}:c"), btn("📅 قرارها", f"lh:{lead.id}:m")]
    ctx.edit(text, inline([tabs, [btn("📝 یادداشت‌ها", f"ln:{lead.id}"), btn("◀️ پرونده", f"ld:{lead.id}")]]))


# notes -----------------------------------------------------------------------
def show_notes(ctx: Ctx, lead_id: int, fresh: bool = False) -> None:
    lead, notes = ctx.db.get_lead(lead_id), ctx.db.lead_notes(lead_id)
    if lead is None:
        return ctx.reply(texts.NOT_FOUND)
    rows = [[btn(f"{texts.stamp(n.created_at, ctx.cfg.timezone)} — {texts._short(n.body, 30)}", f"nv:{n.id}")] for n in notes]
    rows.append([btn("➕ یادداشت جدید", f"na:{lead_id}"), btn("◀️ پرونده", f"ld:{lead_id}")])
    title = f"📝 یادداشت‌های {lead.name} ({texts.fa_digits(len(notes))})" + ("" if notes else f"\n\n{texts.NO_NOTES}")
    (ctx.reply if fresh else ctx.edit)(title, inline(rows))


@router.callback("ln")
def notes_list(ctx: Ctx, arg: str) -> None:
    if lead := _lead(ctx, arg):
        show_notes(ctx, lead.id)


@router.callback("na")
def note_new(ctx: Ctx, arg: str) -> None:
    if lead := _lead(ctx, arg):
        ctx.goto("note:new", lead_id=lead.id)
        ctx.reply(f"{texts.ASK_NOTE_BODY}\n({lead.label})")


@router.state("note:new")
def note_save(ctx: Ctx, text: str) -> None:
    lead_id = ctx.data["lead_id"]
    if ctx.db.get_lead(lead_id) is None:
        ctx.finish()
        return ctx.reply(texts.NOT_FOUND)
    ctx.db.add_note(lead_id, ctx.user.id, text)
    ctx.finish()
    show_notes(ctx, lead_id, fresh=True)


def _note(ctx: Ctx, raw_id: str):  # noqa: ANN202
    note = ctx.db.get_note(int(raw_id)) if raw_id.isdigit() else None
    if note is None:
        ctx.answer(texts.NOT_FOUND)
    return note


@router.callback("nv")
def note_view(ctx: Ctx, arg: str) -> None:
    if note := _note(ctx, arg):
        rows = [[btn("✏️ ویرایش", f"ne:{note.id}"), btn("🗑 حذف", f"nx:{note.id}")], [btn("◀️ یادداشت‌ها", f"ln:{note.lead_id}")]]
        ctx.edit(texts.note_card(note, ctx.cfg.timezone), inline(rows))


@router.callback("ne")
def note_edit_ask(ctx: Ctx, arg: str) -> None:
    if note := _note(ctx, arg):
        ctx.goto("note:edit", note_id=note.id)
        ctx.reply("متن جدید یادداشت را بنویس:")


@router.state("note:edit")
def note_edit_save(ctx: Ctx, text: str) -> None:
    note_id = ctx.data["note_id"]
    note = ctx.db.get_note(note_id)
    ctx.finish()
    if note is None or not ctx.db.edit_note(note_id, text):
        return ctx.reply(texts.NOT_FOUND)
    show_notes(ctx, note.lead_id, fresh=True)


@router.callback("nx")
def note_delete_confirm(ctx: Ctx, arg: str) -> None:
    if note := _note(ctx, arg):
        ctx.edit("این یادداشت حذف شود؟", inline([[btn("🗑 بله، حذف شود", f"ny:{note.id}"), btn("انصراف", f"nv:{note.id}")]]))


@router.callback("ny")
def note_delete(ctx: Ctx, arg: str) -> None:
    if note := _note(ctx, arg):
        ctx.db.delete_note(note.id)
        show_notes(ctx, note.lead_id)
