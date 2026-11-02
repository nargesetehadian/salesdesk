"""Personal to-do list: priorities, due dates, edit, delete, done/reopen and list views."""

from __future__ import annotations

from datetime import date, timedelta

from ...domain import PRIORITIES, Task
from ...fa import WEEKDAYS, fa_digits, parse_jalali, to_jalali
from .. import texts
from ..engine import Ctx
from ..keyboards import btn, chunk, inline
from ..routes import router
from ..views import tasks_view

VIEW_TITLES = {"pending": "⏳ کارهای باز", "overdue": "⚠️ کارهای عقب‌افتاده", "done": "✔️ کارهای انجام‌شده"}
NAV = [btn("📋 امروز", "tl:today"), btn("⏳ باز", "tl:pending"), btn("⚠️ عقب‌افتاده", "tl:overdue"), btn("✔️ انجام‌شده", "tl:done")]


def _task(ctx: Ctx, raw_id: str) -> Task | None:
    task = ctx.db.get_task(int(raw_id)) if raw_id.isdigit() else None
    if task is None or task.owner_id != ctx.user.id:  # a task is private to its owner
        ctx.answer(texts.NOT_FOUND)
        return None
    return task


def _priority_rows(make_data) -> list:  # noqa: ANN001
    return [[btn(label, make_data(key)) for key, label in texts.PRIORITY.items()]]


def show_card(ctx: Ctx, task_id: int, fresh: bool = False) -> None:
    task = ctx.db.get_task(task_id)
    if task is None:
        return ctx.reply(texts.NOT_FOUND)
    first = [btn("↩️ بازگشایی", f"tu:{task.id}")] if task.done else [btn("✅ انجام شد", f"tc:{task.id}")]
    rows = [first, [btn("✏️ ویرایش", f"tw:{task.id}"), btn("🗑 حذف", f"tx:{task.id}")], [btn("◀️ فهرست", "tl:pending")]]
    (ctx.reply if fresh else ctx.edit)(texts.task_card(task, ctx.today()), inline(rows))


# views ---------------------------------------------------------------------------
@router.command("tasks")
def tasks_cmd(ctx: Ctx, arg: str = "") -> None:
    text, markup = tasks_view(ctx.db, ctx.cfg, ctx.user, ctx.now())
    ctx.reply(text, markup)


@router.callback("td")
def task_done(ctx: Ctx, arg: str) -> None:
    done = arg.isdigit() and ctx.db.complete_task(int(arg), ctx.user.id)
    ctx.answer("انجام شد ✅" if done else texts.NOT_FOUND)
    text, markup = tasks_view(ctx.db, ctx.cfg, ctx.user, ctx.now())
    ctx.edit(text, markup)


@router.callback("tl")
def list_view(ctx: Ctx, arg: str) -> None:
    if arg == "today" or arg not in VIEW_TITLES:
        text, markup = tasks_view(ctx.db, ctx.cfg, ctx.user, ctx.now())
        return ctx.edit(text, markup)
    tasks = ctx.db.list_tasks(ctx.user.id, arg, ctx.today())
    rows = [[btn(texts.task_line(t), f"tv:{t.id}")] for t in tasks] + chunk(NAV, 2)
    ctx.edit(f"{VIEW_TITLES[arg]} ({fa_digits(len(tasks))})" + ("" if tasks else "\n\nموردی وجود ندارد."), inline(rows))


@router.callback("tv")
def card(ctx: Ctx, arg: str) -> None:
    if task := _task(ctx, arg):
        show_card(ctx, task.id)


@router.callback("tc")
def complete(ctx: Ctx, arg: str) -> None:
    if (task := _task(ctx, arg)) and ctx.db.complete_task(task.id, ctx.user.id):
        ctx.answer("انجام شد ✅")
        show_card(ctx, task.id)


@router.callback("tu")
def reopen(ctx: Ctx, arg: str) -> None:
    if (task := _task(ctx, arg)) and ctx.db.reopen_task(task.id, ctx.user.id):
        show_card(ctx, task.id)


# create: title -> date (button or typed Jalali) -> priority --------------------------
@router.command("task")
def task_start(ctx: Ctx, arg: str = "") -> None:
    ctx.goto("task:title")
    ctx.reply(texts.ASK_TASK)


@router.state("task:title")
def task_title(ctx: Ctx, text: str) -> None:
    today, buttons = ctx.today(), []
    for offset in range(6):
        day = today + timedelta(days=offset)
        label = ("امروز", "فردا")[offset] if offset < 2 else f"{WEEKDAYS[day.weekday()]} {fa_digits(to_jalali(day.year, day.month, day.day)[2])}"
        buttons.append(btn(label, f"tk:{offset}"))
    ctx.goto("task:date", title=text[:200])
    ctx.reply(texts.ASK_TASK_DATE, inline(chunk(buttons, 3)))


def _ask_priority(ctx: Ctx, due: date) -> None:
    ctx.goto("task:priority", due=due.isoformat())
    ctx.reply(texts.PICK_PRIORITY, inline(_priority_rows(lambda key: f"tq:{key}")))


@router.callback("tk")
def task_date_button(ctx: Ctx, arg: str) -> None:
    if ctx.state == "task:date" and arg.isdigit():
        return _ask_priority(ctx, ctx.today() + timedelta(days=int(arg)))
    ctx.answer(texts.NOT_FOUND)


@router.state("task:date")
def task_date_text(ctx: Ctx, text: str) -> None:
    due = parse_jalali(text)
    if due is None:
        return ctx.reply(texts.BAD_DATE)
    _ask_priority(ctx, due)


@router.callback("tq")
def task_priority_chosen(ctx: Ctx, arg: str) -> None:
    if ctx.state != "task:priority" or arg not in PRIORITIES:
        return ctx.answer(texts.NOT_FOUND)
    task = ctx.db.add_task(ctx.user.id, ctx.data["title"], date.fromisoformat(ctx.data["due"]), arg)
    ctx.finish()
    ctx.reply(texts.task_saved(task) + f"\nاولویت: {texts.PRIORITY[task.priority]}", inline([[btn("📋 مشاهده", f"tv:{task.id}")]]))


# edit and delete ------------------------------------------------------------------
@router.callback("tw")
def edit_menu(ctx: Ctx, arg: str) -> None:
    if task := _task(ctx, arg):
        rows = [[btn("عنوان", f"tf:{task.id}:title"), btn("موعد", f"tf:{task.id}:date"), btn("اولویت", f"tf:{task.id}:priority")], [btn("◀️ بازگشت", f"tv:{task.id}")]]
        ctx.edit(f"✏️ {task.title}\nکدام بخش را ویرایش کنیم؟", inline(rows))


@router.callback("tf")
def edit_field(ctx: Ctx, arg: str) -> None:
    raw_id, _, field = arg.partition(":")
    if not (task := _task(ctx, raw_id)) or field not in ("title", "date", "priority"):
        return
    if field == "priority":
        return ctx.edit(texts.PICK_PRIORITY, inline(_priority_rows(lambda key: f"tp:{task.id}:{key}")))
    ctx.goto("task:edit", task_id=task.id, field=field)
    ctx.reply("عنوان جدید را بنویس:" if field == "title" else texts.ASK_TASK_DATE)


@router.state("task:edit")
def edit_save(ctx: Ctx, text: str) -> None:
    task_id, field = ctx.data["task_id"], ctx.data["field"]
    if field == "date":
        due = parse_jalali(text)
        if due is None:
            return ctx.reply(texts.BAD_DATE)
        changes = {"due_date": due}
    else:
        changes = {"title": text[:200]}
    ok = ctx.db.update_task(task_id, ctx.user.id, **changes)
    ctx.finish()
    show_card(ctx, task_id, fresh=True) if ok else ctx.reply(texts.NOT_FOUND)


@router.callback("tp")
def edit_priority(ctx: Ctx, arg: str) -> None:
    raw_id, _, priority = arg.partition(":")
    if (task := _task(ctx, raw_id)) and priority in PRIORITIES and ctx.db.update_task(task.id, ctx.user.id, priority=priority):
        show_card(ctx, task.id)


@router.callback("tx")
def delete_confirm(ctx: Ctx, arg: str) -> None:
    if task := _task(ctx, arg):
        ctx.edit(f"«{task.title}» حذف شود؟", inline([[btn("🗑 بله، حذف شود", f"tz:{task.id}"), btn("انصراف", f"tv:{task.id}")]]))


@router.callback("tz")
def delete_do(ctx: Ctx, arg: str) -> None:
    if (task := _task(ctx, arg)) and ctx.db.delete_task(task.id, ctx.user.id):
        ctx.edit(f"🗑 «{task.title}» حذف شد.", inline([[btn("◀️ فهرست", "tl:pending")]]))
