"""Reports for chat: today's meetings, stale leads, exports and the manager dashboard link."""

from __future__ import annotations

from ... import exports
from .. import texts
from ..engine import Ctx
from ..keyboards import btn, inline
from ..routes import router
from ..views import today_meetings_text


@router.command("today")
def today_cmd(ctx: Ctx, arg: str = "") -> None:
    ctx.reply(today_meetings_text(ctx.db, ctx.cfg, ctx.user, ctx.now()))


@router.command("stale")
def stale_cmd(ctx: Ctx, arg: str = "") -> None:
    now = ctx.now()
    ctx.reply(texts.stale(ctx.db.stale_leads(ctx.cfg.stale_days, now), ctx.cfg.stale_days, now))


@router.command("export")
def export_cmd(ctx: Ctx, arg: str = "") -> None:
    ctx.reply(texts.EXPORT_PICK, inline([[btn("📗 اکسل (xlsx)", "ex:xlsx"), btn("📄 CSV", "ex:csv")]]))


@router.callback("ex")
def export_do(ctx: Ctx, arg: str) -> None:
    if arg not in exports.FORMATS:
        return ctx.answer(texts.NOT_FOUND)
    ctx.answer("در حال ساخت فایل…")
    owner = None if ctx.user.is_ceo else ctx.user.id  # employees export their own customers only
    export = exports.build_export(ctx.db, ctx.cfg, arg, owner)
    if export is None:
        return ctx.reply(texts.NO_EXPORT)
    filename, data = export
    ctx.messenger.send_document(ctx.update.chat_id, filename, data, "📤 خروجی مشتریان و قرارها")


@router.command("dashboard")
def dashboard_cmd(ctx: Ctx, arg: str = "") -> None:
    if not ctx.user.is_ceo:
        return ctx.reply(texts.NO_ACCESS)
    base = ctx.cfg.public_url or f"http://{ctx.cfg.web_host}:{ctx.cfg.web_port}"
    url = f"{base}/?key={ctx.cfg.dashboard_key}" if ctx.cfg.dashboard_key else base
    if url.startswith("https://"):  # Mini App button opens it inside the messenger
        ctx.reply("📊 داشبورد مدیریتی", {"inline_keyboard": [[{"text": "باز کردن داشبورد", "web_app": {"url": url}}]]})
    else:
        ctx.reply(f"📊 داشبورد مدیریتی:\n{url}")
