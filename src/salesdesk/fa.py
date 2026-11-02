"""Persian locale helpers: Jalali dates, digits and phone normalisation."""

from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

MONTHS = (
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
)  # fmt: skip
WEEKDAYS = ("دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه")  # Mon=0

_FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_ARABIC_LETTERS = str.maketrans({"ي": "ی", "ك": "ک"})
_CUM_DAYS = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)


def fa_digits(value: object) -> str:
    return str(value).translate(_FA)


def en_digits(value: str) -> str:
    return value.translate(_EN)


def normalize_text(value: str) -> str:
    """Canonical form for storing and searching: ASCII digits, Persian ی/ک, trimmed."""
    return en_digits(value).translate(_ARABIC_LETTERS).strip()


def normalize_phone(raw: str) -> str:
    cleaned = en_digits(raw).replace(" ", "").replace("-", "")
    return cleaned if cleaned.lstrip("+").isdigit() else ""


def to_jalali(gy: int, gm: int, gd: int) -> tuple[int, int, int]:
    gy2 = gy + 1 if gm > 2 else gy
    days = (
        355666 + 365 * gy + (gy2 + 3) // 4 - (gy2 + 99) // 100 + (gy2 + 399) // 400
        + gd + _CUM_DAYS[gm - 1]
    )  # fmt: skip
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        return jy, 1 + days // 31, 1 + days % 31
    return jy, 7 + (days - 186) // 30, 1 + (days - 186) % 30


def format_day(dt: datetime, tz: ZoneInfo) -> str:
    local = dt.astimezone(tz)
    _, jm, jd = to_jalali(local.year, local.month, local.day)
    return f"{WEEKDAYS[local.weekday()]} {fa_digits(jd)} {MONTHS[jm - 1]}"


def format_time(dt: datetime, tz: ZoneInfo) -> str:
    return fa_digits(dt.astimezone(tz).strftime("%H:%M"))


def to_gregorian(jy: int, jm: int, jd: int) -> tuple[int, int, int]:
    jy += 1595
    days = (
        -355668 + 365 * jy + (jy // 33) * 8 + ((jy % 33) + 3) // 4 + jd
        + ((jm - 1) * 31 if jm < 7 else (jm - 7) * 30 + 186)
    )  # fmt: skip
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    leap = (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0
    month_len = (0, 31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
    gm = 0
    while gm < 12 and gd > month_len[gm + 1]:
        gm += 1
        gd -= month_len[gm]
    return gy, gm + 1, gd


def parse_jalali(text: str) -> date | None:
    """Parse '1405/08/10' (Persian or Latin digits, / or - separators) into a date."""
    parts = re.split(r"[/\-.]", en_digits(text.strip()))
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    jy, jm, jd = map(int, parts)
    if not (1300 <= jy <= 1500 and 1 <= jm <= 12 and 1 <= jd <= 31):
        return None
    try:
        return date(*to_gregorian(jy, jm, jd))
    except ValueError:
        return None


def format_date(d: date) -> str:
    _, jm, jd = to_jalali(d.year, d.month, d.day)
    return f"{WEEKDAYS[d.weekday()]} {fa_digits(jd)} {MONTHS[jm - 1]}"


def format_datetime(dt: datetime, tz: ZoneInfo) -> str:
    return f"{format_day(dt, tz)}، {format_time(dt, tz)}"


def format_stamp(dt: datetime, tz: ZoneInfo) -> str:
    """Compact Jalali timestamp for spreadsheets: 1405/08/07 10:30."""
    local = dt.astimezone(tz)
    jy, jm, jd = to_jalali(local.year, local.month, local.day)
    return f"{jy}/{jm:02d}/{jd:02d} {local:%H:%M}"
