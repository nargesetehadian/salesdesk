"""Exports are verified by reading the files back with independent parsers (openpyxl / csv)."""

import csv
import io
import re
from datetime import timedelta

from harness import make_settings

from salesdesk import exports
from salesdesk.db import Database
from salesdesk.domain import utcnow

try:
    from openpyxl import load_workbook
except ImportError:  # pragma: no cover
    load_workbook = None


def _seed(tmp_path, n=3, other=True):
    cfg, db = make_settings(tmp_path), Database(make_settings(tmp_path).db_path)
    sara = db.upsert_user("bale", "1", "1", "سارا محمدی", "employee", active=True)
    ali = db.upsert_user("bale", "2", "2", "علی کریمی", "employee", active=True)
    leads = [db.add_lead(f"مشتری {i}", f"شرکت {i}", f"0912{i:07d}", sara.id) for i in range(n)]
    if leads:
        db.add_call(leads[0].id, "یادداشت", sara.id)
        db.add_meeting(leads[0].id, utcnow() + timedelta(days=1), sara.id)
    if other:
        db.add_lead("کریمی", "", "09350000000", ali.id)
    return cfg, db, sara, ali, leads


def test_xlsx_roundtrip_persian_text_types_and_formatting(tmp_path):
    cfg, db, sara, _, _ = _seed(tmp_path)
    name, data = exports.build_export(db, cfg, "xlsx")
    assert re.fullmatch(r"salesdesk-\d{8}\.xlsx", name)
    wb = load_workbook(io.BytesIO(data))
    assert wb.sheetnames == ["مشتریان", "قرارها"]
    ws = wb["مشتریان"]
    headers = [c.value for c in ws[1]]
    assert headers[:5] == ["شناسه", "نام", "شرکت", "تلفن", "مرحله"]
    row = {h: c.value for h, c in zip(headers, ws[2], strict=True)}
    assert row["نام"] == "مشتری 0" and row["شرکت"] == "شرکت 0"
    assert row["تلفن"] == "09120000000" and isinstance(row["تلفن"], str)  # leading zero survives (text cell)
    assert isinstance(row["شناسه"], int) and isinstance(row["تعداد تماس"], int) and row["تعداد تماس"] == 1
    assert re.fullmatch(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}", row["تاریخ ثبت"])  # Jalali, fixed width, sortable
    assert re.fullmatch(r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}", row["قرار بعدی"])
    assert ws.sheet_view.rightToLeft is True and ws.freeze_panes == "A2" and ws.auto_filter.ref == "A1:J5"
    assert all(c.font.b for c in ws[1]) and not ws["B2"].font.b  # bold header only
    assert all(10 <= (ws.column_dimensions[col].width or 0) <= 40 for col in "ABCDEFGHIJ")
    assert wb["قرارها"].max_row == 2  # header + the one booked meeting


def test_xlsx_handles_hostile_and_unusual_text(tmp_path):
    cfg, db, sara, _, _ = _seed(tmp_path, n=0, other=False)
    nasty = ['=SUM(A1)', 'گفت: "سلام"، <b>&</b>', "خط اول\nخط دوم", "نیم‌فاصله و ایموجی 🙂", "  فاصله  ", "کنترل\x00\x07نویسه", "ی" * 40000]
    for text in nasty:
        db.add_lead(text, "", "", sara.id)
    cfg2, data = cfg, exports.build_export(db, cfg, "xlsx")[1]
    ws = load_workbook(io.BytesIO(data))["مشتریان"]
    names = [ws.cell(row=r, column=2).value for r in range(2, ws.max_row + 1)]
    assert names[0] == "=SUM(A1)"  # stored as text by xlsx: it can never execute
    assert ws.cell(row=2, column=2).data_type == "s"
    assert names[1] == 'گفت: "سلام"، <b>&</b>' and names[2] == "خط اول\nخط دوم" and names[3] == "نیم‌فاصله و ایموجی 🙂"
    assert names[4] == "  فاصله  " and names[5] == "کنترلنویسه"  # control characters removed, text kept
    assert len(names[6]) == exports.MAX_CELL and names[6].endswith("…")  # below Excel's 32,767 limit
    assert cfg2 is cfg


def test_csv_roundtrip_encoding_and_injection_safety(tmp_path):
    cfg, db, sara, _, _ = _seed(tmp_path, n=0, other=False)
    for text in ["=cmd|'/c calc'!A1", "@SUM(1)", "ساده، با ویرگول", 'نقل "قول"', "دو\nخطی"]:
        db.add_lead(text, "نوین", "09121234567", sara.id)
    name, raw = exports.build_export(db, cfg, "csv")
    assert name.endswith(".csv") and raw.startswith(b"\xef\xbb\xbf")  # BOM: Excel detects UTF-8
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"))))
    assert rows[0][:4] == ["شناسه", "نام", "شرکت", "تلفن"] and len(rows) == 6
    names = [r[1] for r in rows[1:]]
    assert names[0] == "'=cmd|'/c calc'!A1" and names[1] == "'@SUM(1)"  # neutralised formulas
    assert names[2:] == ["ساده، با ویرگول", 'نقل "قول"', "دو\nخطی"]  # quoting survives a round trip
    assert all(len(r) == len(rows[0]) for r in rows) and rows[1][3] == "09121234567"  # phone kept verbatim


def test_scope_deleted_leads_empty_export_and_volume(tmp_path):
    cfg, db, sara, ali, leads = _seed(tmp_path, n=3)
    db.delete_lead(leads[2].id, sara.id)
    mine = load_workbook(io.BytesIO(exports.build_export(db, cfg, "xlsx", sara.id)[1]))["مشتریان"]
    assert sorted(r[1].value for r in mine.iter_rows(min_row=2)) == ["مشتری 0", "مشتری 1"]  # not ali's, not the deleted one
    assert exports.build_export(Database(tmp_path / "empty.db"), cfg, "xlsx") is None
    for i in range(1500):
        db.add_lead(f"حجیم {i}", "", "", sara.id)
    big = load_workbook(io.BytesIO(exports.build_export(db, cfg, "xlsx")[1]))["مشتریان"]
    assert big.max_row == 1 + 3 + 1500  # header + 3 live leads (one deleted) + 1500
