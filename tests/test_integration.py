import io
import json
import threading
import urllib.error
import urllib.request
import zipfile
from xml.etree import ElementTree

from harness import make_settings

from salesdesk import exports
from salesdesk.db import Database
from salesdesk.platforms import parse_update
from salesdesk.seed import seed
from salesdesk.web import DashboardServer


def test_xlsx_is_a_valid_rtl_workbook_with_two_sheets():
    data = exports.to_xlsx({"مشتریان": (["نام", "تلفن"], [["رضایی", "09121234567"], ["a&b <c>", 5]]), "قرارها": (["x"], [])})
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.testzip() is None
        for name in z.namelist():
            ElementTree.fromstring(z.read(name))  # every part is well-formed XML
        sheet = z.read("xl/worksheets/sheet1.xml").decode()
    assert 'rightToLeft="1"' in sheet and "09121234567" in sheet and "a&amp;b &lt;c&gt;" in sheet
    try:
        from openpyxl import load_workbook
    except ImportError:
        return
    ws = load_workbook(io.BytesIO(data))["مشتریان"]
    assert [c.value for c in ws[3]] == ["a&b <c>", 5] and ws["B2"].value == "09121234567"


def test_csv_has_bom_and_blocks_formula_injection():
    raw = exports.to_csv(["a", "b"], [["=HYPERLINK(\"x\")", "+98 912"], ["-5", "@cmd"]])
    assert raw.startswith(b"\xef\xbb\xbf")
    lines = raw.decode("utf-8-sig").splitlines()
    assert lines[1].startswith("\"'=") and "+98 912" in lines[1] and "'@cmd" in lines[2]


def test_parse_update_ignores_groups_and_normalises_callbacks():
    private = {"message": {"message_id": 5, "text": "سلام", "chat": {"id": 7, "type": "private"}, "from": {"id": 7, "first_name": "سارا"}}}
    group = {"message": {"text": "hi", "chat": {"id": -1, "type": "group"}, "from": {"id": 7}}}
    click = {"callback_query": {"id": "c1", "data": "ld:3", "from": {"id": 7, "first_name": "سارا"}, "message": {"message_id": 9, "chat": {"id": 7}}}}
    assert parse_update("bale", private).text == "سلام" and parse_update("bale", group) is None
    cb = parse_update("bale", click)
    assert (cb.callback_data, cb.message_id, cb.chat_id) == ("ld:3", 9, "7")
    assert parse_update("bale", {"message": {"chat": {"id": 1}, "photo": []}}) is None


def test_web_health_auth_api_and_export_download(tmp_path):
    cfg = make_settings(tmp_path, web_port=0, dashboard_key="k" * 16)
    db = Database(cfg.db_path)
    seed(db, cfg)
    server = DashboardServer(db, cfg)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        assert json.load(urllib.request.urlopen(f"{base}/healthz")) == {"ok": True}  # probe needs no key
        try:
            urllib.request.urlopen(f"{base}/api/summary")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
        summary = json.load(urllib.request.urlopen(f"{base}/api/summary?range=7d&key={'k' * 16}"))
        assert summary["performance"]["range"] == "7d" and len(summary["performance"]["team"]) == 2
        page = urllib.request.urlopen(f"{base}/?key={'k' * 16}")
        assert "frame-ancestors" in page.headers["Content-Security-Policy"] and "X-Frame-Options" not in page.headers
        xlsx = urllib.request.urlopen(f"{base}/export/leads.xlsx?key={'k' * 16}")
        assert zipfile.is_zipfile(io.BytesIO(xlsx.read()))
    finally:
        server.shutdown()


def test_poller_skips_a_malformed_update_and_still_handles_the_rest(tmp_path):
    from harness import FakeMessenger, make_bot

    from salesdesk.bot.engine import Poller

    _, _, engine, _ = make_bot(tmp_path)
    stop = threading.Event()

    class OneBatch(FakeMessenger):
        def __init__(self):
            super().__init__()
            self.offsets = []

        def get_updates(self, offset, timeout):
            self.offsets.append(offset)
            if len(self.offsets) > 1:
                stop.set()
                return []
            return [
                {"update_id": 10, "message": {"text": "x", "chat": {"type": "private"}}},  # no chat id: parser raises KeyError
                {"nothing": "useful"},  # no update_id at all
                {"update_id": 11, "message": {"text": "/id", "chat": {"id": 5, "type": "private"}, "from": {"id": 5}}},
            ]

    messenger = OneBatch()
    Poller(messenger, engine, stop).run()
    assert [e[2] for e in messenger.events] == ["شناسه‌ی تو: bale:5"]  # the good update was handled
    assert messenger.offsets == [None, 12]  # and the poison update was acknowledged, not replayed


def test_web_internal_errors_return_500_and_the_server_keeps_serving(tmp_path, monkeypatch=None):
    import salesdesk.web as web

    cfg = make_settings(tmp_path, web_port=0)
    server = DashboardServer(Database(cfg.db_path), cfg)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    original = web.build_summary
    web.build_summary = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("report bug"))
    try:
        try:
            urllib.request.urlopen(f"{base}/api/summary")
        except urllib.error.HTTPError as exc:
            assert exc.code == 500 and json.loads(exc.read()) == {"error": "internal error"}
        else:
            raise AssertionError("expected a 500")
        assert json.load(urllib.request.urlopen(f"{base}/healthz")) == {"ok": True}  # still alive
    finally:
        web.build_summary = original
        server.shutdown()
