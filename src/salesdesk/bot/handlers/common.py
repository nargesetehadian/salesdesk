from __future__ import annotations


from .. import keyboards, texts
from ..keyboards import btn, inline
from ..engine import Ctx
from ..routes import router


@router.command("start", "help")
def start(ctx: Ctx, arg: str = "") -> None:
    ctx.reply(texts.welcome(ctx.user.name.split()[0], ctx.user.is_ceo), keyboards.menu(ctx.user.is_ceo))


@router.command("cancel")
def cancel(ctx: Ctx, arg: str = "") -> None:
    ctx.reply(texts.CANCELLED, keyboards.menu(ctx.user.is_ceo))


def pick_lead(ctx: Ctx, prefix: str) -> None:
    """Show the most recent open customers as buttons; `prefix` is the callback to run on a pick."""
    leads, _ = ctx.db.search_leads(open_only=True, limit=8)
    if not leads:
        return ctx.reply(texts.NO_LEADS)
    ctx.reply(texts.PICK_LEAD, inline([[btn(lead.label, f"{prefix}:{lead.id}")] for lead in leads]))
