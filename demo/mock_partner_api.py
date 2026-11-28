"""Tiny local stand-in for a partner's REST API, used by the IntegrateHub demo.

    python demo/mock_partner_api.py            # serves on http://127.0.0.1:8765

GET /contacts  -> demo/data/partner_api_contacts.json  (same shape as a paginated partner API)
GET /flaky     -> HTTP 503, so you can show a degraded connector and its error log
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent / "data" / "partner_api_contacts.json"
DEFAULT_PORT = 8765


class PartnerAPIHandler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        if self.path.startswith("/contacts"):
            self._send(200, json.loads(DATA_FILE.read_text(encoding="utf-8")))
        elif self.path.startswith("/flaky"):
            self._send(503, {"error": "partner API temporarily unavailable"})
        else:
            self._send(404, {"error": "not found"})

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("[mock partner api] " + fmt % args + "\n")


def start_in_background(port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), PartnerAPIHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    print(f"Mock partner API on http://127.0.0.1:{port}  (GET /contacts, GET /flaky). Ctrl+C to stop.")
    ThreadingHTTPServer(("127.0.0.1", port), PartnerAPIHandler).serve_forever()
