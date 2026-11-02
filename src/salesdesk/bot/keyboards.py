"""Builders for the Bot API keyboard payloads (identical on Telegram and Bale)."""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from . import texts

Button = dict


def btn(text: str, data: str) -> Button:
    return {"text": text, "callback_data": data}


def inline(rows: Iterable[Sequence[Button]]) -> dict:
    return {"inline_keyboard": [list(r) for r in rows if r]}


def chunk(buttons: Sequence[Button], size: int) -> list[Sequence[Button]]:
    return [buttons[i : i + size] for i in range(0, len(buttons), size)]


def menu(is_ceo: bool) -> dict:
    labels = list(texts.MENU)
    rows = chunk([{"text": label} for label in labels if is_ceo or texts.MENU[label] != "dashboard"], 2)
    return {"keyboard": rows, "resize_keyboard": True}
