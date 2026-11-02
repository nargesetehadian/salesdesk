"""Contact (call) logging: outcome, notes, optional duration and next follow-up. Records are append-only."""

from __future__ import annotations

from datetime import date

from ...crm import DURATION_CHOICES_MIN
from ...domain import CONTACT_OUTCOMES, EFFECTIVE_OUTCOMES
from ...fa import en_digits
from .. import texts
from ..engine import Ctx
from ..keyboards import btn, chunk, inline
from ..routes import router
from . import followup
from .common import pick_lead


@router.command("call")
def call_cmd(ctx: Ctx, arg: str = "") -> None:
    pick_lead(ctx, "lc")


@router.callback("lc")
def pick_outcome(ctx: Ctx, arg: str) -> None:
    lead = ctx.db.get_lead(int(arg)) if arg.isdigit() else None
    if lead is None:
        return ctx.answer(texts.NOT_FOUND)
    buttons = [btn(texts.OUTCOME[o], f"co:{lead.id}:{o}") for o in CONTACT_OUTCOMES]
    ctx.edit(f"📞 {lead.label}\n{texts.PICK_OUTCOME}", inline(chunk(buttons, 2) + [[btn("◀️ بازگشت", f"ld:{lead.id}")]]))


@router.callback("co")
def outcome_chosen(ctx: Ctx, arg: str) -> None:
    raw_id, _, outcome = arg.partition(":")
    if not raw_id.isdigit() or outcome not in CONTACT_OUTCOMES or ctx.db.get_lead(int(raw_id)) is None:
        return ctx.answer(texts.NOT_FOUND)
    ctx.goto("call:note", lead_id=int(raw_id), outcome=outcome)
    ctx.reply(f"{texts.OUTCOME[outcome]} ✔️\n{texts.ASK_NOTE_OPT}")


@router.state("call:note")
def note_entered(ctx: Ctx, text: str) -> None:
    ctx.data["note"] = "" if text == "-" else text
    if ctx.data["outcome"] in EFFECTIVE_OUTCOMES:  # duration only makes sense for a real conversation
        ctx.state = "call:duration"
        buttons = [btn("رد کردن", "cd:0")] + [btn(f"{m} دقیقه".replace(str(m), texts.fa_digits(m)), f"cd:{m}") for m in DURATION_CHOICES_MIN]
        return ctx.reply(texts.ASK_DURATION, inline(chunk(buttons, 3)))
    followup.ask(ctx, "contact")


def _duration_done(ctx: Ctx, minutes: int) -> None:
    ctx.data["duration_sec"] = minutes * 60 if minutes else None
    followup.ask(ctx, "contact")


@router.callback("cd")
def duration_button(ctx: Ctx, arg: str) -> None:
    if ctx.state == "call:duration" and arg.isdigit():
        return _duration_done(ctx, int(arg))
    ctx.answer(texts.NOT_FOUND)


@router.state("call:duration")
def duration_typed(ctx: Ctx, text: str) -> None:
    value = en_digits(text.strip())
    if not value.isdigit() or int(value) > 600:
        return ctx.reply(texts.BAD_DURATION)
    _duration_done(ctx, int(value))


@followup.finisher("contact")
def save_contact(ctx: Ctx, day: date | None) -> None:
    data = ctx.data
    lead = ctx.db.get_lead(data["lead_id"])
    if lead is None:
        ctx.finish()
        return ctx.reply(texts.NOT_FOUND)
    ctx.db.add_contact(lead.id, ctx.user.id, data["outcome"], data.get("note", ""), data.get("duration_sec"), day)
    ctx.finish()
    ctx.reply(texts.contact_saved(lead, data["outcome"], day), inline([[btn("👤 پرونده‌ی مشتری", f"ld:{lead.id}")]]))
