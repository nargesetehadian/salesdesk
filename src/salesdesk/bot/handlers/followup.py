"""Shared conversation step: "when should we follow up next?".

A flow (contact, meeting outcome, ...) calls `ask(ctx, flow)` and registers a finisher that receives the
chosen date (or None). Keeping this step in one place avoids duplicating the picker in every flow.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

from ... import crm
from ...fa import parse_jalali
from .. import texts
from ..engine import Ctx
from ..keyboards import btn, inline
from ..routes import router

Finisher = Callable[[Ctx, "date | None"], None]
FINISHERS: dict[str, Finisher] = {}


def finisher(flow: str) -> Callable[[Finisher], Finisher]:
    def deco(fn: Finisher) -> Finisher:
        if flow in FINISHERS:
            raise ValueError(f"follow-up finisher for {flow!r} already registered")
        FINISHERS[flow] = fn
        return fn

    return deco


def ask(ctx: Ctx, flow: str) -> None:
    ctx.goto("fu:wait", flow=flow)
    rows = [[btn("بدون پیگیری", "cf:x")], [btn(label, f"cf:{key}") for label, key in texts.FOLLOWUP_BUTTONS], [btn("📅 تاریخ دلخواه", "cf:t")]]
    ctx.reply(texts.ASK_FOLLOWUP, inline(rows))


def _finish(ctx: Ctx, day: date | None) -> None:
    FINISHERS[ctx.data["flow"]](ctx, day)


@router.callback("cf")
def choose(ctx: Ctx, arg: str) -> None:
    if ctx.state not in ("fu:wait", "fu:date") or ctx.data.get("flow") not in FINISHERS:
        return ctx.answer(texts.NOT_FOUND)
    if arg == "t":
        ctx.state = "fu:date"
        return ctx.reply(texts.ASK_FOLLOWUP_DATE)
    _finish(ctx, crm.followup_date(ctx.today(), arg))


@router.state("fu:wait")
@router.state("fu:date")
def typed(ctx: Ctx, text: str) -> None:
    day = parse_jalali(text)
    if day is None or day < ctx.today():
        return ctx.reply(texts.BAD_FOLLOWUP)
    _finish(ctx, day)
