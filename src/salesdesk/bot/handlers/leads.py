"""Customers: create, list/filter/search, view, edit, delete, log calls, change stage."""

from __future__ import annotations

from ... import crm
from ...db import EDITABLE_LEAD_FIELDS
from ...domain import STAGES
from ...fa import normalize_phone, normalize_text
from .. import texts
from ..engine import Ctx
from ..keyboards import btn, chunk, inline
from ..routes import router
from .common import pick_lead

PAGE_SIZE = 5


def _lead_or_none(ctx: Ctx, raw_id: str):  # noqa: ANN202
    lead = ctx.db.get_lead(int(raw_id)) if raw_id.isdigit() else None
    if lead is None:
        ctx.answer(texts.NOT_FOUND)
    return lead


# creation ------------------------------------------------------------------
@router.command("new")
def new_start(ctx: Ctx, arg: str = "") -> None:
    ctx.goto("new:name")
    ctx.reply(texts.ASK_NAME)


@router.state("new:name")
def new_name(ctx: Ctx, text: str) -> None:
    ctx.goto("new:company", name=normalize_text(text))
    ctx.reply(texts.ASK_COMPANY)


@router.state("new:company")
def new_company(ctx: Ctx, text: str) -> None:
    ctx.goto("new:phone", company="" if text == "-" else normalize_text(text))
    ctx.reply(texts.ASK_PHONE)


@router.state("new:phone")
def new_phone(ctx: Ctx, text: str) -> None:
    phone = "" if text == "-" else normalize_phone(text)
    if text != "-" and not phone:
        return ctx.reply(texts.BAD_PHONE)
    ctx.goto("new:source", phone=phone)
    ctx.reply(texts.PICK_SOURCE, inline(_source_rows(lambda key: f"sr:{key}") + [[btn("رد کردن", "sr:-")]]))


def _source_rows(make_data):  # noqa: ANN001, ANN202
    return chunk([btn(label, make_data(key)) for key, label in texts.SOURCE.items()], 2)


@router.callback("sr")
def new_source(ctx: Ctx, arg: str) -> None:
    if ctx.state != "new:source" or (arg != "-" and arg not in texts.SOURCE):
        return ctx.answer(texts.NOT_FOUND)
    data = ctx.data
    lead = ctx.db.add_lead(data["name"], data["company"], data["phone"], ctx.user.id, "" if arg == "-" else arg)
    ctx.finish()
    show_card(ctx, lead.id, fresh=True)


# list, filter, search ------------------------------------------------------
def show_list(ctx: Ctx, status: str = "", page: int = 0, mine: bool = False, fresh: bool = False) -> None:
    query = ctx.data.get("q", "")
    leads, total = ctx.db.search_leads(
        query=query, status=status or None, owner_id=ctx.user.id if mine else None,
        limit=PAGE_SIZE, offset=page * PAGE_SIZE,
    )  # fmt: skip
    key, flag = status or "all", int(mine)
    rows = [[btn(f"{texts.STAGE[lead.status][:2]} {lead.label}", f"ld:{lead.id}")] for lead in leads]
    filters = [("همه", "all")] + [(texts.STAGE_SHORT[s], s) for s in STAGES]
    rows += chunk([btn(("• " if k == key else "") + label, f"ls:{k}:0:{flag}") for label, k in filters], 4)
    nav = []
    if page > 0:
        nav.append(btn("◀️ قبلی", f"ls:{key}:{page - 1}:{flag}"))
    if (page + 1) * PAGE_SIZE < total:
        nav.append(btn("بعدی ▶️", f"ls:{key}:{page + 1}:{flag}"))
    rows.append(nav)
    rows.append([btn("👥 همه‌ی تیم" if mine else "👤 فقط مال من", f"ls:{key}:0:{int(not mine)}")]
                + ([btn("✖️ پاک کردن جستجو", "lq:")] if query else []))  # fmt: skip
    text = texts.lead_list_title(total, status, query, mine) + ("" if total else "\n\nنتیجه‌ای پیدا نشد.")
    (ctx.reply if fresh else ctx.edit)(text, inline(rows))


@router.command("leads")
def leads_cmd(ctx: Ctx, arg: str = "") -> None:
    show_list(ctx, fresh=True)


@router.callback("ls")
def leads_cb(ctx: Ctx, arg: str) -> None:
    key, page, mine = arg.split(":")
    show_list(ctx, "" if key == "all" else key, int(page), mine == "1")


@router.callback("lq")
def search_clear(ctx: Ctx, arg: str) -> None:
    ctx.data.pop("q", None)
    show_list(ctx)


@router.command("search")
def search_start(ctx: Ctx, arg: str = "") -> None:
    if arg:
        ctx.data["q"] = normalize_text(arg)
        return show_list(ctx, fresh=True)
    ctx.goto("search:q")
    ctx.reply(texts.ASK_QUERY)


@router.state("search:q")
def search_run(ctx: Ctx, text: str) -> None:
    ctx.state, ctx.data = "", {"q": normalize_text(text)}
    show_list(ctx, fresh=True)


# card ----------------------------------------------------------------------
def show_card(ctx: Ctx, lead_id: int, fresh: bool = False) -> None:
    profile = crm.build_profile(ctx.db, lead_id, ctx.now())
    if profile is None:
        return ctx.reply(texts.NOT_FOUND)
    lead = profile.lead
    rows = [
        [btn("📞 ثبت تماس", f"lc:{lead.id}"), btn("📅 قرار جدید", f"lm:{lead.id}")],
        [btn("📜 تاریخچه", f"lh:{lead.id}:t"), btn("📝 یادداشت‌ها", f"ln:{lead.id}")],
    ]
    if ctx.can_manage(lead.owner_id):
        rows.append([btn("✏️ ویرایش", f"le:{lead.id}"), btn("🔄 مرحله", f"lg:{lead.id}"), btn("🗑 حذف", f"lx:{lead.id}")])
    rows += [[btn(texts.meeting_label(m, ctx.cfg.timezone), f"mg:{m.id}")] for m in profile.upcoming_meetings[:3]]
    rows.append([btn("◀️ فهرست", "ls:all:0:0")])
    (ctx.reply if fresh else ctx.edit)(texts.profile_card(profile, ctx.now(), ctx.cfg.timezone), inline(rows))


@router.callback("ld")
def card_cb(ctx: Ctx, arg: str) -> None:
    if lead := _lead_or_none(ctx, arg):
        show_card(ctx, lead.id)


# edit ----------------------------------------------------------------------
@router.callback("le")
def edit_pick_field(ctx: Ctx, arg: str) -> None:
    if (lead := _lead_or_none(ctx, arg)) and ctx.can_manage(lead.owner_id):
        rows = chunk([btn(label, f"lf:{lead.id}:{field}") for field, label in texts.FIELD.items()], 2) + [[btn("◀️ بازگشت", f"ld:{lead.id}")]]
        return ctx.edit(texts.PICK_FIELD, inline(rows))
    ctx.answer(texts.NO_ACCESS)


@router.callback("lf")
def edit_ask_value(ctx: Ctx, arg: str) -> None:
    raw_id, _, field = arg.partition(":")
    lead = _lead_or_none(ctx, raw_id)
    if not lead or field not in EDITABLE_LEAD_FIELDS or not ctx.can_manage(lead.owner_id):
        return ctx.answer(texts.NO_ACCESS)
    if field == "source":
        rows = _source_rows(lambda key: f"ss:{lead.id}:{key}") + [[btn("◀️ بازگشت", f"ld:{lead.id}")]]
        return ctx.edit(texts.PICK_SOURCE, inline(rows))
    ctx.goto("edit:value", lead_id=lead.id, field=field)
    ctx.reply(f"مقدار جدید «{texts.FIELD[field]}» را بفرست (برای خالی کردن، - بفرست):")


@router.state("edit:value")
def edit_save(ctx: Ctx, text: str) -> None:
    lead, field = ctx.db.get_lead(ctx.data["lead_id"]), ctx.data["field"]
    if lead is None or not ctx.can_manage(lead.owner_id):
        ctx.finish()
        return ctx.reply(texts.NO_ACCESS)
    value = "" if text == "-" and field != "name" else (normalize_phone(text) if field == "phone" else normalize_text(text))
    if (field == "phone" and text != "-" and not value) or (field == "name" and not value):
        return ctx.reply(texts.BAD_PHONE if field == "phone" else texts.ASK_NAME)
    ctx.db.update_lead(lead.id, **{field: value})
    ctx.finish()
    show_card(ctx, lead.id, fresh=True)


@router.callback("ss")
def source_set(ctx: Ctx, arg: str) -> None:
    raw_id, _, key = arg.partition(":")
    lead = _lead_or_none(ctx, raw_id)
    if lead and key in texts.SOURCE and ctx.can_manage(lead.owner_id):
        ctx.db.update_lead(lead.id, source=key)
        return show_card(ctx, lead.id)
    ctx.answer(texts.NO_ACCESS)


# delete (soft) -------------------------------------------------------------
@router.callback("lx")
def delete_confirm(ctx: Ctx, arg: str) -> None:
    lead = _lead_or_none(ctx, arg)
    if lead and ctx.can_manage(lead.owner_id):
        rows = [[btn("🗑 بله، حذف شود", f"ly:{lead.id}"), btn("انصراف", f"ld:{lead.id}")]]
        return ctx.edit(f"«{lead.label}» و قرارهای پیش‌رویش حذف شود؟", inline(rows))
    ctx.answer(texts.NO_ACCESS)


@router.callback("ly")
def delete_do(ctx: Ctx, arg: str) -> None:
    lead = _lead_or_none(ctx, arg)
    if lead and ctx.can_manage(lead.owner_id):
        ctx.db.delete_lead(lead.id, ctx.user.id)
        return ctx.edit(f"🗑 «{lead.label}» حذف شد.", inline([[btn("◀️ فهرست", "ls:all:0:0")]]))
    ctx.answer(texts.NO_ACCESS)


# stage ---------------------------------------------------------------------
@router.command("stage")
def stage_cmd(ctx: Ctx, arg: str = "") -> None:
    pick_lead(ctx, "lg")


@router.callback("lg")
def stage_pick(ctx: Ctx, arg: str) -> None:
    lead = _lead_or_none(ctx, arg)
    if lead and ctx.can_manage(lead.owner_id):
        rows = chunk([btn(texts.STAGE[s], f"sg:{lead.id}:{s}") for s in STAGES], 2) + [[btn("◀️ بازگشت", f"ld:{lead.id}")]]
        return ctx.edit(f"{lead.label}\n{texts.PICK_STAGE}", inline(rows))
    ctx.answer(texts.NO_ACCESS)


@router.callback("sg")
def stage_set(ctx: Ctx, arg: str) -> None:
    raw_id, _, stage = arg.partition(":")
    lead = _lead_or_none(ctx, raw_id)
    if lead and stage in STAGES and ctx.can_manage(lead.owner_id):
        ctx.db.set_stage(lead.id, stage, ctx.user.id)
        return show_card(ctx, lead.id)
    ctx.answer(texts.NO_ACCESS)
