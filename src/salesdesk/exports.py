"""Spreadsheet exports. The xlsx writer is dependency-free (OOXML via zipfile) and RTL-aware."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from .bot.texts import STAGE_SHORT
from .config import Settings
from .db import Database
from .domain import parse_dt, utcnow
from .fa import format_stamp

FORMATS = ("xlsx", "csv")
MAX_CELL = 32000  # Excel hard limit is 32,767 characters per cell
MIME = {"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "csv": "text/csv; charset=utf-8"}
Sheet = tuple[list[str], list[list[Any]]]
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_STYLES = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><styleSheet xmlns="{_NS}">'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'
)


def _col(index: int) -> str:
    name = ""
    index += 1
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def _cell(ref: str, value: Any, style: int = 0) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c r="{ref}" s="{style}"><v>{value}</v></c>'
    text = _ILLEGAL_XML.sub("", "" if value is None else str(value))
    text = escape(text if len(text) <= MAX_CELL else text[: MAX_CELL - 1] + "…")
    return f'<c r="{ref}" s="{style}" t="inlineStr"><is><t xml:space="preserve">{text}</t></is></c>'


def _sheet_xml(headers: list[str], rows: list[list[Any]]) -> str:
    widths = [max([len(str(h))] + [len(str(r[i])) for r in rows[:200]]) for i, h in enumerate(headers)]
    cols = "".join(f'<col min="{i + 1}" max="{i + 1}" width="{min(max(w + 3, 10), 40)}" customWidth="1"/>' for i, w in enumerate(widths))
    body = ['<row r="1">' + "".join(_cell(f"{_col(i)}1", h, 1) for i, h in enumerate(headers)) + "</row>"]
    for n, row in enumerate(rows, start=2):
        body.append(f'<row r="{n}">' + "".join(_cell(f"{_col(i)}{n}", v) for i, v in enumerate(row)) + "</row>")
    return (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="{_NS}">'
        '<sheetViews><sheetView rightToLeft="1" workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
        'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
        f"<cols>{cols}</cols><sheetData>{''.join(body)}</sheetData>"
        f'<autoFilter ref="A1:{_col(len(headers) - 1)}{len(rows) + 1}"/></worksheet>'
    )


def to_xlsx(sheets: dict[str, Sheet]) -> bytes:
    n = len(sheets)
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, n + 1)
    )
    workbook = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="{_NS}" xmlns:r="{_REL}"><sheets>'
        + "".join(f'<sheet name={quoteattr(name[:31])} sheetId="{i}" r:id="rId{i}"/>' for i, name in enumerate(sheets, 1))
        + "</sheets></workbook>"
    )
    rels = "".join(
        f'<Relationship Id="rId{i}" Type="{_REL}/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, n + 1)
    ) + f'<Relationship Id="rId{n + 1}" Type="{_REL}/styles" Target="styles.xml"/>'
    pkg = "http://schemas.openxmlformats.org/package/2006/relationships"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
            '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
            f"{overrides}</Types>")  # fmt: skip
        z.writestr("_rels/.rels", f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{pkg}"><Relationship Id="rId1" Type="{_REL}/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", f'<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{pkg}">{rels}</Relationships>')
        z.writestr("xl/styles.xml", _STYLES)
        for i, (headers, rows) in enumerate(sheets.values(), 1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(headers, rows))
    return buffer.getvalue()


def _csv_safe(value: Any) -> Any:
    """Neutralise spreadsheet formula injection (cells starting with = @ or a non-numeric +/-)."""
    if isinstance(value, str) and value[:1] in ("=", "@", "+", "-") and not value[1:].replace(" ", "").isdigit():
        return "'" + value
    return value


def to_csv(headers: list[str], rows: list[list[Any]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(headers)
    writer.writerows([[_csv_safe(v) for v in row] for row in rows])
    return out.getvalue().encode("utf-8-sig")  # BOM so Excel opens Persian text correctly


def build_export(db: Database, cfg: Settings, fmt: str, owner_id: int | None = None) -> tuple[str, bytes] | None:
    """Leads (and meetings for xlsx). `owner_id` restricts the export to one employee's customers."""
    tz = cfg.timezone
    stamp = lambda v: format_stamp(parse_dt(v), tz) if v else ""  # noqa: E731
    leads = db.export_leads(owner_id)
    if not leads:
        return None
    lead_sheet: Sheet = (
        ["شناسه", "نام", "شرکت", "تلفن", "مرحله", "مسئول", "تاریخ ثبت", "آخرین تماس", "تعداد تماس", "قرار بعدی"],
        [[r["id"], r["name"], r["company"], r["phone"], STAGE_SHORT[r["status"]], r["owner_name"] or "",
          stamp(r["created_at"]), stamp(r["last_contact_at"]), r["calls"], stamp(r["next_meeting"])] for r in leads],
    )  # fmt: skip
    name = f"salesdesk-{utcnow().astimezone(tz):%Y%m%d}"
    if fmt == "csv":
        return f"{name}.csv", to_csv(*lead_sheet)
    status_fa = {"scheduled": "برنامه‌ریزی‌شده", "cancelled": "لغو شده", "done": "انجام شده"}
    meeting_sheet: Sheet = (
        ["شناسه", "مشتری", "شرکت", "مسئول", "زمان", "وضعیت"],
        [[r["id"], r["lead_name"], r["company"], r["owner_name"] or "", stamp(r["scheduled_at"]), status_fa.get(r["status"], r["status"])]
         for r in db.export_meetings(owner_id)],
    )  # fmt: skip
    return f"{name}.xlsx", to_xlsx({"مشتریان": lead_sheet, "قرارها": meeting_sheet})


