"""Zero-dependency HTTP server: dashboard page, JSON API, exports and a health probe.

Intended to sit behind a TLS-terminating reverse proxy when exposed to the internet.
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import exports
from .analytics import build_summary
from .config import Settings
from .db import Database

log = logging.getLogger(__name__)
INDEX = Path(__file__).resolve().parents[2] / "dashboard" / "index.html"
# Lets the dashboard open as a Mini App inside Bale (and its web client) and Telegram.
CSP = "frame-ancestors 'self' https://web.bale.ai https://*.bale.ai https://web.telegram.org"


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, db: Database, settings: Settings) -> None:
        super().__init__((settings.web_host, settings.web_port), _Handler)
        self.db, self.settings = db, settings


class _Handler(BaseHTTPRequestHandler):
    server: DashboardServer

    def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
        try:
            self._route()
        except (BrokenPipeError, ConnectionResetError):
            log.debug("Client disconnected")
        except Exception:
            log.exception("Unhandled error serving %s", urlparse(self.path).path)
            try:
                self._send(500, b'{"error": "internal error"}', "application/json")
            except OSError:
                pass

    def _route(self) -> None:
        url, srv = urlparse(self.path), self.server
        query = parse_qs(url.query)
        if url.path == "/healthz":
            return self._json({"ok": srv.db.health()})
        key = srv.settings.dashboard_key
        if key and not hmac.compare_digest(query.get("key", [""])[0], key):
            return self._send(401, b"Access key required", "text/plain")
        if url.path == "/api/summary":
            self._json(build_summary(srv.db, srv.settings, range_key=query.get("range", ["30d"])[0]))
        elif url.path in ("/export/leads.xlsx", "/export/leads.csv"):
            fmt = url.path.rsplit(".", 1)[1]
            export = exports.build_export(srv.db, srv.settings, fmt)
            if export is None:
                return self._send(404, b"Nothing to export", "text/plain")
            filename, data = export
            self._send(200, data, exports.MIME[fmt], {"Content-Disposition": f'attachment; filename="{filename}"'})
        elif url.path in ("/", "/index.html"):
            self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        else:
            self._send(404, b"Not found", "text/plain")

    def _json(self, payload: object) -> None:
        self._send(200, json.dumps(payload).encode(), "application/json")

    def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in {
            "Content-Type": content_type, "Content-Length": str(len(body)), "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff", "Content-Security-Policy": CSP, **(headers or {}),
        }.items():  # fmt: skip
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:  # access logs would leak the key in the URL
        pass


def start_in_background(db: Database, settings: Settings) -> DashboardServer:
    server = DashboardServer(db, settings)
    threading.Thread(target=server.serve_forever, name="dashboard", daemon=True).start()
    log.info("Dashboard listening on http://%s:%s", settings.web_host, settings.web_port)
    return server


def main(argv: list[str] | None = None) -> None:
    """Dashboard only (no bot tokens needed). `--demo` serves the demo database."""
    import argparse

    parser = argparse.ArgumentParser(prog="python -m salesdesk.web")
    parser.add_argument("--demo", action="store_true", help="serve DEMO_DB_PATH instead of the real database")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    settings = Settings.from_env(need_platform=False)
    path = settings.demo_db_path if args.demo else settings.db_path
    if args.demo and not path.exists():
        raise SystemExit(f"{path} does not exist. Run `python -m salesdesk.seed` first.")
    server = DashboardServer(Database(path), settings)
    log.info("Dashboard (%s) at http://%s:%s", "demo" if args.demo else "live", settings.web_host, settings.web_port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()
